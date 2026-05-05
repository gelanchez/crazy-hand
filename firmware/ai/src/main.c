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
#endif

static void image_capture_done_cb(void *arg) {
    xEventGroupSetBits(g_eventGroup, CAPTURE_DONE_BIT);
}

void led_task(void *parameters) {
    (void)parameters; // Inform the compiler parameters are not used to avoid
                      // warnings
    char *taskname = pcTaskGetName(NULL);
    cpxPrintToConsole(LOG_TO_CRTP, "Task %s created\n", taskname);

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
    cpxPrintToConsole(LOG_TO_CRTP, "Task %s created\n", taskname);

    // Open and configure the Himax HM01B0 camera
    struct pi_device camera;
    if (setup_camera(&camera)) {
        cpxPrintToConsole(LOG_TO_CRTP, "Failed to open and configure camera\n");
        return;
    }

    // Reserve buffer spaces for image
    g_buff_img = pmsis_l2_malloc(IMG_SIZE);
    if (g_buff_img == NULL) {
        cpxPrintToConsole(LOG_TO_CRTP, "Failed to allocate g_buff_img\n");
        return;
    }

    cpxPrintToConsole(LOG_TO_CRTP, "Initialized image buffer\n");

#ifdef JPEG_ENCODING
    // Can we move the JPEG encoding to the cluster?
    jpeg_encoder_t jpeg_encoder;
    struct jpeg_encoder_conf encoder_conf;
    jpeg_encoder_conf_init(&encoder_conf);
    encoder_conf.width = IMG_WIDTH;
    encoder_conf.height = IMG_HEIGHT;
    encoder_conf.flags = 0; // Grayscale

    if (jpeg_encoder_open(&jpeg_encoder, &encoder_conf)) {
        cpxPrintToConsole(LOG_TO_CRTP, "Failed initialize JPEG encoder\n");
        return;
    }

    pi_buffer_t header;
    uint32_t headerSize;
    pi_buffer_t footer;
    uint32_t footerSize;
    pi_buffer_t jpeg_data;
    uint32_t jpegSize;

    // TODO Do it manually without additional functions
    pi_buffer_init(&g_jpeg_buffer, PI_BUFFER_TYPE_L2, g_buff_img);
    pi_buffer_set_format(&g_jpeg_buffer, IMG_WIDTH, IMG_HEIGHT, 1,
                         PI_BUFFER_FORMAT_GRAY);

    header.size = 1024;
    header.data = pmsis_l2_malloc(1024);

    footer.size = 10;
    footer.data = pmsis_l2_malloc(10);

    // This must fit the full encoded JPEG
    jpeg_data.size = 1024 * 15;
    jpeg_data.data = pmsis_l2_malloc(1024 * 15);

    if (header.data == 0 || footer.data == 0 || jpeg_data.data == 0) {
        cpxPrintToConsole(LOG_TO_CRTP,
                          "Could not allocate memory for JPEG image\n");
        return;
    }

    jpeg_encoder_header(&jpeg_encoder, &header, &headerSize);
    jpeg_encoder_footer(&jpeg_encoder, &footer, &footerSize);
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
        pi_camera_capture_async(
            &camera, g_buff_img, IMG_SIZE,
            pi_task_callback(&g_task, image_capture_done_cb, NULL));
        pi_camera_control(&camera, PI_CAMERA_CMD_START, 0);
        xEventGroupWaitBits(g_eventGroup, CAPTURE_DONE_BIT, pdTRUE, pdFALSE,
                            (TickType_t)portMAX_DELAY);
        pi_camera_control(&camera, PI_CAMERA_CMD_STOP, 0);
        captureTime = xTaskGetTickCount() - start;

        if (g_wifiClientConnected == 1) {
            // PROCESS
            start = xTaskGetTickCount();
            processTime = xTaskGetTickCount() - start;

            start = xTaskGetTickCount();
#if defined(JPEG_ENCODING)
            // JPEG ENCODE
            jpeg_encoder_process(&jpeg_encoder, &g_jpeg_buffer, &jpeg_data,
                                 &jpegSize);
            encodeTime = xTaskGetTickCount() - start;

            // TRANSFER
            start = xTaskGetTickCount();
            imgSize = headerSize + jpegSize + footerSize;
            transferJpegImage(&g_txPacket, imgSize, jpeg_data.data, jpegSize,
                              header.data, headerSize, footer.data, footerSize);
#elif defined(RAW_ENCODING)
            // TRANSFER
            transferRawImage(&g_txPacket, imgSize, g_buff_img);
#endif
            transferTime = xTaskGetTickCount() - start;

            // TODO Process and send CPX data to STM
            // cpxInitRoute(CPX_T_GAP8, CPX_T_STM32, CPX_F_APP,
            // &g_txPacket.route); //
            // TODO We need a different CPX packet here
            start = xTaskGetTickCount();
            // g_txPacket.data[0] = 0;
            // g_txPacket.dataLength = 1;
            // cpxSendPacketBlocking(&g_txPacket);
            cpxTime = xTaskGetTickCount() - start;

            cpxPrintToConsole(
                LOG_TO_CRTP,
                "cap = %d ms, proc = %d ms, enc = %d ms (%d B), xfer = "
                "%d ms, CPX = %d ms\n",
                captureTime, processTime, encodeTime, imgSize, transferTime,
                cpxTime);
        } else {
            vTaskDelay(10);
        }
    }
}

void rx_task(void *parameters) {
    (void)parameters; // Inform the compiler parameters are not used to avoid
                      // warnings
    char *taskname = pcTaskGetName(NULL);
    cpxPrintToConsole(LOG_TO_CRTP, "Task %s created\n", taskname);

    while (true) {
        // TODO client alive, multiple clients?
        cpxReceivePacketBlocking(CPX_F_WIFI_CTRL, &g_rxPacket);

        WiFiCTRLPacket_t *wifiCtrl =
            (WiFiCTRLPacket_t *)
                g_rxPacket.data; // Pointers to the same memory location

        switch (wifiCtrl->cmd) {
        case WIFI_CTRL_STATUS_WIFI_CONNECTED: // Not used in access point (I
                                              // think)
            cpxPrintToConsole(LOG_TO_CRTP, "WiFi connected (%u.%u.%u.%u)\n",
                              wifiCtrl->data[0], wifiCtrl->data[1],
                              wifiCtrl->data[2], wifiCtrl->data[3]);
            g_wifiConnected = true;
            break;
        case WIFI_CTRL_STATUS_CLIENT_CONNECTED:
            cpxPrintToConsole(LOG_TO_CRTP,
                              "WiFi client connection status: %u\n",
                              wifiCtrl->data[0]);
            g_wifiClientConnected = true;
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
    cpxPrintToConsole(LOG_TO_CRTP, "\n*** %s ***\n", APP_NAME);

    // Setup WiFi access point
    setupWiFi(&g_txPacket);

    // Event group
    g_eventGroup = xEventGroupCreate();
    if (g_eventGroup == NULL) {
        cpxPrintToConsole(LOG_TO_CRTP, "Failed to create event group, "
                                       "insuficient FreeRTOS heap available\n");
        pmsis_exit(-1);
    }

    BaseType_t xTask;

    // LED_TASK
    xTask = xTaskCreate(led_task, "LED_TASK", configMINIMAL_STACK_SIZE * 2,
                        NULL, tskIDLE_PRIORITY + 1, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "LED_TASK did not start!\n");
        pmsis_exit(-1);
    }

    // CAMERA_TASK
    xTask =
        xTaskCreate(camera_task, "CAMERA_TASK", configMINIMAL_STACK_SIZE * 4,
                    NULL, tskIDLE_PRIORITY + 2, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "CAMERA_TASK did not start!\n");
        pmsis_exit(-1);
    }

    // RX_TASK
    xTask = xTaskCreate(rx_task, "RX_TASK", configMINIMAL_STACK_SIZE * 2, NULL,
                        tskIDLE_PRIORITY + 1, NULL);
    if (xTask != pdPASS) {
        cpxPrintToConsole(LOG_TO_CRTP, "RX_TASK did not start!\n");
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
