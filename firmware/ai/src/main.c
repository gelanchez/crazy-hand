/**
 * @file main.c
 * @brief Main GAP8 code.
 */

/* INCLUDE */
// Drivers
#include "bsp/bsp.h"
#include "pmsis.h"

// Own libraries
#include "utils.h"

// JPEG
#ifdef JPEG_ENCODING
#include "bsp/buffer.h"
#include "gaplib/jpeg_encoder.h"
#endif

// CPX communications
#include "cpx.h"

// FreeRTOS
#include "FreeRTOS.h"
#include "task.h"

// LED
#define AIDECK_LED_PIN 2

// Event capture bit
#define CAPTURE_DONE_BIT (1 << 0)

/* GLOBAL VARIABLES */
static EventGroupHandle_t g_eventGroup;
PI_L2 unsigned char *g_buff_img;
static CPXPacket_t g_txPacket;
static CPXPacket_t g_rxPacket;
static bool g_wifiConnected = false;
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

static void image_capture_done_cb(void *arg) {
    xEventGroupSetBits(g_eventGroup, CAPTURE_DONE_BIT);
}

void led_task(void *parameters) {
    (void)parameters; // Inform the compiler parameters are not used to avoid
                      // warnings
    char *taskname = pcTaskGetName(NULL);
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Task %s created\n", taskname);

    // Initialize the LED pin
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
    (void)parameters; // Inform the compiler parameters are not used to avoid
                      // warnings
    char *taskname = pcTaskGetName(NULL);
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Task %s created\n", taskname);

    // Open and configure the Himax HM01B0 camera
    struct pi_device camera;
    if (setup_camera(&camera)) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Failed to open and configure camera\n");
        return;
    }

    // Reserve buffer spaces for image
    g_buff_img = pmsis_l2_malloc(IMG_SIZE);
    if (g_buff_img == NULL) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Failed to allocate g_buff_img\n");
        return;
    }

    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Initialized image buffer\n");

#ifdef JPEG_ENCODING
    // Link capture buffer — encoder and JPEG buffers are initialised in run() before tasks start
    pi_buffer_init(&g_jpeg_buffer, PI_BUFFER_TYPE_L2, g_buff_img);
    pi_buffer_set_format(&g_jpeg_buffer, IMG_WIDTH, IMG_HEIGHT, 1,
                         PI_BUFFER_FORMAT_GRAY);
    uint32_t jpegSize;
#endif

    uint32_t imgSize = IMG_SIZE;

    // Performance measuring variables
    uint32_t start = 0;
    uint32_t captureTime = 0;
    uint32_t processTime = 0;
    uint32_t encodeTime = 0;
    uint32_t transferTime = 0;
    uint32_t cpxTime = 0;

    // We're reusing the same packet, so initialize the route once
    cpxInitRoute(CPX_T_GAP8, CPX_T_WIFI_HOST, CPX_F_APP, &g_txPacket.route);

    while (true) {
        // CAPTURE
        start = xTaskGetTickCount();
        pi_camera_control(&camera, PI_CAMERA_CMD_START, 0);
        pi_camera_capture_async(
            &camera, g_buff_img, IMG_SIZE,
            pi_task_callback(&g_task, image_capture_done_cb, NULL));
        // 5000ms safety timeout — protects against permanent DMA hang.
        // IMPORTANT: do NOT add xEventGroupClearBits here or warmup STOP/START
        // cycles — those desync VSYNC and corrupt every frame (162-byte row offset).
        EventBits_t captureBits = xEventGroupWaitBits(
            g_eventGroup, CAPTURE_DONE_BIT, pdTRUE, pdFALSE,
            pdMS_TO_TICKS(5000));
        pi_camera_control(&camera, PI_CAMERA_CMD_STOP, 0);
        captureTime = xTaskGetTickCount() - start;

        if (!(captureBits & CAPTURE_DONE_BIT)) {
            cpxPrintToConsole(LOG_TO_CRTP,
                "[WARNING] DMA timeout — reinitializing camera\n");
            if (setup_camera(&camera)) {
                cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Camera reinit failed\n");
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            continue;
        }

        if (g_wifiClientConnected) {
            // PROCESS
            start = xTaskGetTickCount();
            processTime = xTaskGetTickCount() - start;

            start = xTaskGetTickCount();
#if defined(JPEG_ENCODING)
            // JPEG ENCODE
            jpeg_encoder_process(&g_jpeg_encoder, &g_jpeg_buffer, &g_jpeg_data,
                                 &jpegSize);
            encodeTime = xTaskGetTickCount() - start;

            // TRANSFER
            start = xTaskGetTickCount();
            imgSize = g_jpeg_header_size + jpegSize + g_jpeg_footer_size;
            transferJpegImage(&g_txPacket, imgSize, g_jpeg_data.data, jpegSize,
                              g_jpeg_header.data, g_jpeg_header_size,
                              g_jpeg_footer.data, g_jpeg_footer_size);
#elif defined(RAW_ENCODING)
            // TRANSFER
            transferRawImage(&g_txPacket, imgSize, g_buff_img);
#endif
            transferTime = xTaskGetTickCount() - start;

            start = xTaskGetTickCount();
            cpxTime = xTaskGetTickCount() - start;

#ifdef DEBUG
            cpxPrintToConsole(
                LOG_TO_CRTP,
                "[DEBUG] cap=%dms proc=%dms enc=%dms(%dB) xfer=%dms cpx=%dms\n",
                captureTime, processTime, encodeTime, imgSize, transferTime,
                cpxTime);
#endif
        } else {
            vTaskDelay(10);
        }
    }
}

void rx_task(void *parameters) {
    (void)parameters; // Inform the compiler parameters are not used to avoid
                      // warnings
    char *taskname = pcTaskGetName(NULL);
    cpxPrintToConsole(LOG_TO_CRTP, "[INFO] Task %s created\n", taskname);

    while (true) {
        // TODO client alive, multiple clients?
        cpxReceivePacketBlocking(CPX_F_WIFI_CTRL, &g_rxPacket);

        WiFiCTRLPacket_t *wifiCtrl =
            (WiFiCTRLPacket_t *)
                g_rxPacket.data; // Pointers to the same memory location

        switch (wifiCtrl->cmd) {
        case WIFI_CTRL_STATUS_WIFI_CONNECTED:
            cpxPrintToConsole(LOG_TO_CRTP, "[INFO] WiFi connected (%u.%u.%u.%u)\n",
                              wifiCtrl->data[0], wifiCtrl->data[1],
                              wifiCtrl->data[2], wifiCtrl->data[3]);
            g_wifiConnected = true;
            break;
        case WIFI_CTRL_STATUS_CLIENT_CONNECTED:
            g_wifiClientConnected = (wifiCtrl->data[0] == 1);
            cpxPrintToConsole(LOG_TO_CRTP, "[INFO] WiFi client %s\n",
                              g_wifiClientConnected ? "connected" : "disconnected");
            break;
        default:
            break;
        }
    }
}

void run(void) {
    // Initialize CPX communications
    cpxInit();
    cpxEnableFunction(CPX_F_APP);
    cpxEnableFunction(CPX_F_WIFI_CTRL);
    cpxPrintToConsole(LOG_TO_CRTP, "\n[INFO] *** %s ***\n", APP_NAME);

    // Setup WiFi access point
    setupWiFi(&g_txPacket);

    // Event group
    g_eventGroup = xEventGroupCreate();
    if (g_eventGroup == NULL) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] Failed to create event group, "
                                       "insufficient FreeRTOS heap available\n");
        pmsis_exit(-1);
    }

    BaseType_t xTask;

#ifdef JPEG_ENCODING
    {
        struct jpeg_encoder_conf enc_conf;
        jpeg_encoder_conf_init(&enc_conf);
        enc_conf.width  = IMG_WIDTH;
        enc_conf.height = IMG_HEIGHT;
        enc_conf.flags  = 0; // FC-only; pi_cluster_open broken in FreeRTOS context (all SDK attempts failed)

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

    // LED_TASK
    xTask = xTaskCreate(led_task, "LED_TASK", configMINIMAL_STACK_SIZE * 2,
                        NULL, tskIDLE_PRIORITY + 1, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] LED_TASK did not start!\n");
        pmsis_exit(-1);
    }

    // CAMERA_TASK
    xTask =
        xTaskCreate(camera_task, "CAMERA_TASK", configMINIMAL_STACK_SIZE * 8,
                    NULL, tskIDLE_PRIORITY + 2, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "[ERROR] CAMERA_TASK did not start!\n");
        pmsis_exit(-1);
    }

    // RX_TASK
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

// Program entry
int main(void) {
    // Initialize pads according to configuration ai_deck.sh
    pi_bsp_init();

    // Set FC controller to max frequency
    pi_freq_set(PI_FREQ_DOMAIN_FC, 250000000);
    // pi_pmu_voltage_set(PI_PMU_DOMAIN_FC, 1200);
    // __pi_pmu_voltage_set(PI_PMU_DOMAIN_FC, 1200);

    return pmsis_kickoff((void *)run);
}
