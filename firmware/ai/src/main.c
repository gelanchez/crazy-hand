/**
 * @file main.c
 * @brief Main GAP8 code.
 */

/* INCLUDE */
#include "bsp/bsp.h"
#include "pmsis.h"
#include "utils.h"

#ifdef JPEG_ENCODING
#include "bsp/buffer.h"
#include "gaplib/jpeg_encoder.h"
#endif

#include "cpx.h"
#include "FreeRTOS.h"
#include "task.h"
#include <string.h>

#define AIDECK_LED_PIN 2
#define CAPTURE_DONE_BIT (1 << 0)

// Warmup frames discarded on startup: 1 to discard the first misaligned frame
// (sensor resumes at unknown position after setupCamera CMD_STOP), plus
// additional frames for AEG to settle.
#define WARMUP_FRAMES 4

// Row-split detection: minimum mean-absolute-brightness-jump between adjacent
// rows to be considered a VSYNC split (not scene content). Observed splits
// have Δ 23–128; scene gradients measured ≤ 15.
#define SPLIT_THRESHOLD 20
// Isolation window: a split candidate is rejected if any row within this many
// rows also exceeds 50% of the peak. VSYNC splits are single-row; scene edges
// span multiple adjacent rows (e.g., row102 Δ=122 AND row106 Δ=111 = object edge).
#define SPLIT_ISOLATION_ROWS 6

/* GLOBAL VARIABLES */
static EventGroupHandle_t g_eventGroup;
static struct pi_device g_camera;

// DMA capture buffer (L2 SRAM). Per-frame CMD_STOP/CMD_START means only one
// buffer is needed: sensor is in standby while we process and transmit.
PI_L2 unsigned char *g_dma_buf;

// Scratch buffer: memcpy destination after DMA completes. g_dma_buf is then
// free to serve as tmp workspace for apply_row_correction if needed.
PI_L2 unsigned char *g_scratch;

static volatile int g_skip_rem = WARMUP_FRAMES;

static CPXPacket_t g_txPacket;
static CPXPacket_t g_rxPacket;
static bool g_wifiClientConnected = false;
static pi_task_t g_task;

#ifdef JPEG_ENCODING
static pi_buffer_t g_jpeg_buffer;
static jpeg_encoder_t g_jpeg_encoder;
static pi_buffer_t    g_jpeg_header;
static pi_buffer_t    g_jpeg_footer;
static pi_buffer_t    g_jpeg_data;
static uint32_t       g_jpeg_header_size;
static uint32_t       g_jpeg_footer_size;
#endif

// Search all row boundaries for the largest mean-absolute-brightness jump that
// is also an isolated peak (no neighbor within SPLIT_ISOLATION_ROWS exceeds
// 50% of the peak). VSYNC splits produce a single-row discontinuity; scene
// edges span multiple adjacent rows and fail the isolation test.
// Returns split row index, or -1 if no qualifying boundary found.
static uint32_t row_grad(const unsigned char *buf, int r) {
    const unsigned char *a = buf + r * IMG_WIDTH;
    const unsigned char *b = buf + (r + 1) * IMG_WIDTH;
    uint32_t d = 0;
    for (int c = 0; c < IMG_WIDTH; c++) {
        int v = (int)b[c] - (int)a[c];
        d += v < 0 ? -v : v;
    }
    return d / IMG_WIDTH;
}

static int detect_split_row(const unsigned char *buf) {
    // Pass 1: find the maximum gradient row
    int split = -1;
    uint32_t max_diff = SPLIT_THRESHOLD;
    for (int r = 0; r < IMG_HEIGHT - 1; r++) {
        uint32_t d = row_grad(buf, r);
        if (d > max_diff) { max_diff = d; split = r; }
    }
    if (split < 0) return -1;

    // Pass 2: isolation check — recompute only the ±SPLIT_ISOLATION_ROWS neighbors
    int lo = split - SPLIT_ISOLATION_ROWS; if (lo < 0) lo = 0;
    int hi = split + SPLIT_ISOLATION_ROWS; if (hi >= IMG_HEIGHT - 1) hi = IMG_HEIGHT - 2;
    for (int r = lo; r <= hi; r++) {
        if (r == split) continue;
        if (row_grad(buf, r) > max_diff / 2) return -1;
    }

    return split;
}

// Rotate g_scratch by (split+1) rows so the image starts at row 0.
// tmp must be IMG_SIZE bytes and not overlap g_scratch; pass g_dma_buf
// (safe: DMA is stopped, sensor in standby while we process).
static void apply_row_correction(int split, unsigned char *tmp) {
    uint32_t bottom = (uint32_t)(split + 1) * IMG_WIDTH;
    uint32_t top    = IMG_SIZE - bottom;
    memcpy(tmp,        g_scratch + bottom, top);
    memcpy(tmp + top,  g_scratch,          bottom);
    memcpy(g_scratch,  tmp,                IMG_SIZE);
}

static void image_capture_done_cb(void *arg) {
    (void)arg;
    xEventGroupSetBits(g_eventGroup, CAPTURE_DONE_BIT);
}

void led_task(void *parameters) {
    (void)parameters;
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Task %s created\n", pcTaskGetName(NULL));

    pi_device_t led_gpio_dev;
    pi_gpio_pin_configure(&led_gpio_dev, AIDECK_LED_PIN, PI_GPIO_OUTPUT);
    const TickType_t xDelay = 500 / portTICK_PERIOD_MS;

    while (true) {
        pi_gpio_pin_write(&led_gpio_dev, AIDECK_LED_PIN, 1);
        vTaskDelay(xDelay);
        pi_gpio_pin_write(&led_gpio_dev, AIDECK_LED_PIN, 0);
        vTaskDelay(xDelay);
    }
}

void camera_task(void *parameters) {
    (void)parameters;
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Task %s created\n", pcTaskGetName(NULL));

    // Allow voltage/clock to stabilise before touching the camera peripheral
    vTaskDelay(pdMS_TO_TICKS(2000));

    if (setupCamera(&g_camera)) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Failed to open and configure camera\n");
        return;
    }

    g_dma_buf = pmsis_l2_malloc(IMG_SIZE);
    g_scratch  = pmsis_l2_malloc(IMG_SIZE);
    if (!g_dma_buf || !g_scratch) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Failed to allocate image buffers\n");
        return;
    }
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Initialized image buffers (2 x %u bytes)\n",
                      (unsigned)IMG_SIZE);

#ifdef JPEG_ENCODING
    pi_buffer_init(&g_jpeg_buffer, PI_BUFFER_TYPE_L2, g_scratch);
    pi_buffer_set_format(&g_jpeg_buffer, IMG_WIDTH, IMG_HEIGHT, 1,
                         PI_BUFFER_FORMAT_GRAY);
    uint32_t jpegSize;
#endif

    uint32_t imgSize = IMG_SIZE;
    uint32_t start = 0;
    uint32_t captureTime = 0;
    uint32_t transferTime = 0;
    uint32_t frame_ok = 0;
    uint32_t frame_tx = 0;

    cpxInitRoute(CPX_T_GAP8, CPX_T_WIFI_HOST, CPX_F_APP, &g_txPacket.route);

    g_skip_rem = WARMUP_FRAMES;
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Starting per-frame capture, %d warmup frames\n",
                      WARMUP_FRAMES);

    while (true) {
        if (!g_wifiClientConnected) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        // Per-frame capture: arm DMA, CMD_START, wait, CMD_STOP.
        // CMD_START triggers a fresh VSYNC so DMA always starts at row 0.
        pi_camera_capture_async(&g_camera, g_dma_buf, IMG_SIZE,
            pi_task_callback(&g_task, image_capture_done_cb, NULL));
        pi_camera_control(&g_camera, PI_CAMERA_CMD_START, 0);

        start = xTaskGetTickCount();
        EventBits_t bits = xEventGroupWaitBits(
            g_eventGroup, CAPTURE_DONE_BIT, pdTRUE, pdFALSE, pdMS_TO_TICKS(5000));
        captureTime = xTaskGetTickCount() - start;

        pi_camera_control(&g_camera, PI_CAMERA_CMD_STOP, 0);
        // HM01B0 needs ≥30ms standby before CMD_START produces valid VSYNC.
        // Empirically: 35ms gap worked reliably in prior sessions (from xfer time
        // alone); 30ms explicit covers both warmup and streaming.
        vTaskDelay(pdMS_TO_TICKS(30));

        if (!(bits & CAPTURE_DONE_BIT)) {
            cpxPrintToConsole(LOG_TO_CRTP,
                "[WARNING] DMA timeout (cap=%ums, ok=%u tx=%u) -- reinit\n",
                captureTime, frame_ok, frame_tx);
            if (setupCamera(&g_camera)) {
                cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Camera reinit failed\n");
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            g_skip_rem = WARMUP_FRAMES;
            continue;
        }

        if (g_skip_rem > 0) {
            g_skip_rem--;
            cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Warmup %d/%d cap=%ums\n",
                              WARMUP_FRAMES - g_skip_rem, WARMUP_FRAMES, captureTime);
            if (g_skip_rem == 0)
                cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Warmup done, streaming\n");
            continue;
        }

        memcpy(g_scratch, g_dma_buf, IMG_SIZE);

#ifdef DEBUG_SPLIT_DETECT
        // Fallback: detect and correct row shift. Should never trigger with
        // per-frame CMD_STOP/CMD_START (split always -1); retained for regression testing.
        int split = detect_split_row(g_scratch);
        if (split >= 0) {
            apply_row_correction(split, g_dma_buf);
            cpxPrintToConsole(LOG_TO_CRTP, "[INFO] row-correct: split=%d\n", split);
        }
#endif

        frame_ok++;

        uint32_t txstart = xTaskGetTickCount();
#if defined(JPEG_ENCODING)
        jpeg_encoder_process(&g_jpeg_encoder, &g_jpeg_buffer, &g_jpeg_data,
                             &jpegSize);
        txstart = xTaskGetTickCount();
        imgSize = g_jpeg_header_size + jpegSize + g_jpeg_footer_size;
        transferJpegImage(&g_txPacket, imgSize, g_jpeg_data.data, jpegSize,
                          g_jpeg_header.data, g_jpeg_header_size,
                          g_jpeg_footer.data, g_jpeg_footer_size);
#elif defined(RAW_ENCODING)
        transferRawImage(&g_txPacket, imgSize, g_scratch);
#endif
        transferTime = xTaskGetTickCount() - txstart;
        frame_tx++;
        if (frame_tx % 100 == 0)
            cpxPrintToConsole(LOG_TO_CRTP,
                "[INFO] frame #%u cap=%ums xfer=%ums\n",
                frame_tx, captureTime, transferTime);
    }
}

void rx_task(void *parameters) {
    (void)parameters;
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Task %s created\n", pcTaskGetName(NULL));

    while (true) {
        cpxReceivePacketBlocking(CPX_F_WIFI_CTRL, &g_rxPacket);
        WiFiCTRLPacket_t *wifiCtrl = (WiFiCTRLPacket_t *)g_rxPacket.data;

        switch (wifiCtrl->cmd) {
        case WIFI_CTRL_STATUS_WIFI_CONNECTED:
            cpxPrintToConsole(LOG_TO_CRTP, "[INFO] WiFi connected (%u.%u.%u.%u)\n",
                              wifiCtrl->data[0], wifiCtrl->data[1],
                              wifiCtrl->data[2], wifiCtrl->data[3]);
            break;
        case WIFI_CTRL_STATUS_CLIENT_CONNECTED:
            if (wifiCtrl->data[0] == 1) {
                vTaskDelay(pdMS_TO_TICKS(2000));
                g_wifiClientConnected = true;
                cpxPrintToConsole(LOG_TO_CRTP, "[INFO] WiFi client connected (streaming enabled)\n");
            } else {
                g_wifiClientConnected = false;
                cpxPrintToConsole(LOG_TO_CRTP, "[INFO] WiFi client disconnected\n");
            }
            break;
        default:
            break;
        }
    }
}

void run(void) {
    cpxInit();
    cpxEnableFunction(CPX_F_APP);
    cpxEnableFunction(CPX_F_WIFI_CTRL);
    cpxPrintToConsole(LOG_TO_CRTP, "\n[INFO] *** %s ***\n", APP_NAME);

    setupWiFi(&g_txPacket);

    g_eventGroup = xEventGroupCreate();
    if (g_eventGroup == NULL) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Failed to create event group\n");
        pmsis_exit(-1);
    }

    BaseType_t xTask;

#ifdef JPEG_ENCODING
    {
        struct jpeg_encoder_conf enc_conf;
        jpeg_encoder_conf_init(&enc_conf);
        enc_conf.width  = IMG_WIDTH;
        enc_conf.height = IMG_HEIGHT;
        enc_conf.flags  = 0;

        if (jpeg_encoder_open(&g_jpeg_encoder, &enc_conf)) {
            cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Failed to open JPEG encoder\n");
            pmsis_exit(-1);
        }

        g_jpeg_header.size = 1024;
        g_jpeg_header.data = pmsis_l2_malloc(1024);
        g_jpeg_footer.size = 10;
        g_jpeg_footer.data = pmsis_l2_malloc(10);
        g_jpeg_data.size   = 1024 * 15;
        g_jpeg_data.data   = pmsis_l2_malloc(1024 * 15);

        if (!g_jpeg_header.data || !g_jpeg_footer.data || !g_jpeg_data.data) {
            cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] JPEG buffer allocation failed\n");
            pmsis_exit(-1);
        }

        jpeg_encoder_header(&g_jpeg_encoder, &g_jpeg_header, &g_jpeg_header_size);
        jpeg_encoder_footer(&g_jpeg_encoder, &g_jpeg_footer, &g_jpeg_footer_size);
        cpxPrintToConsole(LOG_TO_CRTP, "[INFO] JPEG encoder ready (FC-only)\n");
    }
#endif

    xTask = xTaskCreate(led_task, "LED_TASK", configMINIMAL_STACK_SIZE * 2,
                        NULL, tskIDLE_PRIORITY + 1, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] LED_TASK did not start!\n");
        pmsis_exit(-1);
    }

    xTask = xTaskCreate(camera_task, "CAMERA_TASK", configMINIMAL_STACK_SIZE * 8,
                        NULL, tskIDLE_PRIORITY + 2, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] CAMERA_TASK did not start!\n");
        pmsis_exit(-1);
    }

    xTask = xTaskCreate(rx_task, "RX_TASK", configMINIMAL_STACK_SIZE * 2, NULL,
                        tskIDLE_PRIORITY + 1, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] RX_TASK did not start!\n");
        pmsis_exit(-1);
    }

    while (true) {
        pi_yield();
    }

    pmsis_exit(0);
}

int main(void) {
    pi_bsp_init();
    pi_freq_set(PI_FREQ_DOMAIN_FC, 250000000);
    __pi_pmu_voltage_set(PI_PMU_DOMAIN_FC, 1200);
    return pmsis_kickoff((void *)run);
}
