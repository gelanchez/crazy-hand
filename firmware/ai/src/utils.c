/**
 * @file utils.c
 * @brief Utils functions.
 */

#include "utils.h"
#include "cpx.h" // CPX communications
// #include "bsp/camera.h"
#include "bsp/camera/himax.h"

/**
 * @brief Creates an image header packet for transmission over CPX.
 *
 * This function initializes a CPX packet with an image header, filling in the
 * necessary metadata such as image dimensions, format, and size. The header is
 * then ready to be sent as part of the image transmission process.
 *
 * @param packet A pointer to the CPXPacket_t structure where the image header
 * will be stored. This packet's data field is populated with the image header
 * information.
 * @param imgSize The size of the image in bytes. This value is used to set the
 * size field in the image header.
 * @param imgFormat The format of the image (e.g., raw, JPEG). This value is
 * used to set the format field in the image header.
 *
 * The function sets the following fields in the image header:
 * - `magic`: A fixed value (0xBC) to identify the packet as an image header.
 * - `width` and `height`: Predefined dimensions (`IMG_WIDTH` and `IMG_HEIGHT`).
 * - `depth`: Set to 1, indicating a single channel (e.g., grayscale).
 * - `format`: Set according to the `imgFormat` parameter.
 * - `size`: Set according to the `imgSize` parameter.
 *
 * @note The function assumes that the width, height, and depth are predefined
 * constants.
 */
void createImageHeaderPacket(CPXPacket_t *packet, uint32_t imgSize,
                             ImageFormat_t imgFormat) {
    img_header_t *imgHeader = (img_header_t *)packet->data;
    imgHeader->magic = 0xBC;
    imgHeader->width = IMG_WIDTH;
    imgHeader->height = IMG_HEIGHT;
    imgHeader->depth = 1;
    imgHeader->format = imgFormat;
    imgHeader->size = imgSize;
    packet->dataLength = sizeof(img_header_t); // As per CPX package
}

/**
 * @brief Opens and configures a Himax camera device.
 *
 * This function opens and configures a Himax camera device. It initializes the
 * camera settings, sets the image orientation, and performs additional
 * configuration based on compile-time directives.
 *
 * @param device Pointer to the device structure representing the Himax camera.
 *
 * @return 0 on success, -1 on failure.
 */
int setup_camera(struct pi_device *device) {
    cpxPrintToConsole(LOG_TO_CRTP, "Opening Himax camera\n");

    // Initialize camera configuration
    struct pi_himax_conf camera_conf;
    pi_himax_conf_init(&camera_conf);
#if defined(QVGA_MODE)
    camera_conf.format = PI_CAMERA_QVGA; // QVGA
#endif

    // Open camera device
    pi_open_from_conf(device, &camera_conf);
    if (pi_camera_open(device)) {
        cpxPrintToConsole(LOG_TO_CRTP, "Failed to open camera\n");
        return -1;
    }

    // Start camera operation
    pi_camera_control(device, PI_CAMERA_CMD_START, 0);

    uint8_t set_value;
    uint8_t reg_value;

    // Rotate camera orientation
    set_value = 3;
    pi_camera_reg_set(device, HIMAX_IMG_ORIENTATION, &set_value);
    pi_time_wait_us(1000000);
    // vTaskDelay(M2T(1000))
    pi_camera_reg_get(device, HIMAX_IMG_ORIENTATION, &reg_value);
    if (set_value != reg_value) {
        cpxPrintToConsole(LOG_TO_CRTP, "Failed to rotate camera image\n");
        return -1;
    }
    cpxPrintToConsole(LOG_TO_CRTP, "Image orientation %d\n", reg_value);

    // Leave PIXEL_SHIFT_EN at driver default (0x01). Setting it to 0 introduces
    // 2 invalid bright columns at the right edge of every row (cols 322-323).
    // The driver default avoids these invalid columns.
    pi_camera_reg_get(device, HIMAX_VSYNC_HSYNC_PIXEL_SHIFT_EN, &reg_value);
    cpxPrintToConsole(LOG_TO_CRTP, "PIXEL_SHIFT_EN %d\n", reg_value);

    // QVGA_MODE
#ifdef QVGA_MODE
    set_value = 1;
    pi_camera_reg_set(device, HIMAX_QVGA_WIN_EN, &set_value);
    pi_camera_reg_get(device, HIMAX_QVGA_WIN_EN, &reg_value);
    cpxPrintToConsole(LOG_TO_CRTP, "QVGA window enabled %d\n", reg_value);
#endif

    // Stop camera operation and initialize auto exposure gain
    pi_camera_control(device, PI_CAMERA_CMD_STOP, 0);
    pi_camera_control(device, PI_CAMERA_CMD_AEG_INIT, 0);

    return 0;
}

/**
 * @brief Sends a large buffer of data in chunks over the CPX communication
 * protocol.
 *
 * This function is designed to send a buffer of data that may be larger than
 * the packet size allowed by the CPX protocol. It divides the buffer into
 * smaller chunks and sends each chunk sequentially using the
 * `cpxSendPacketBlocking` function. The function handles the logic for
 * splitting the buffer into appropriately sized packets and continues sending
 * until the entire buffer is transmitted.
 *
 * @param packet A pointer to the CPXPacket_t structure used for sending each
 * chunk of data. The data field of this structure is filled with chunks of the
 * buffer, and its length is set accordingly.
 * @param buffer A pointer to the buffer containing the data to be sent. This
 * buffer may be larger than the maximum packet size.
 * @param bufferSize The total size of the buffer in bytes.
 *
 * @note The function handles the case where the buffer size is not an exact
 * multiple of the packet size, ensuring that the final packet contains the
 * remaining data.
 */
void sendBufferViaCPX(CPXPacket_t *packet, uint8_t *buffer,
                      uint32_t bufferSize) {
    uint32_t offset = 0;
    uint32_t size = 0;
    do {
        size = sizeof(packet->data);
        if (offset + size > bufferSize) {
            size = bufferSize - offset;
        }
        memcpy(packet->data, &buffer[offset], size);
        packet->dataLength = size;
        cpxSendPacketBlocking(packet);
        offset += size;
    } while (size == sizeof(packet->data));
}

/**
 * @brief Configures and initiates a WiFi Access Point (AP) connection via CPX.
 *
 * This function sets up the WiFi access point by configuring the necessary CPX
 * routing, setting the SSID, and initiating the WiFi connection process. The
 * function sends the appropriate control commands to the WiFi module through
 * the CPX protocol, ensuring that the WiFi AP is correctly configured and
 * connected.
 *
 * @param txPacket A pointer to the CPXPacket_t structure used for sending WiFi
 * control packets. This structure is filled with the necessary command data and
 * sent through the CPX protocol.
 *
 * The function performs the following steps:
 * - Initializes the routing for WiFi control packets.
 * - Sets the SSID using the predefined `APP_NAME`.
 * - Sends the command to connect to the WiFi network.
 *
 * @note The function currently includes a placeholder (`TODO`) for setting the
 * WiFi key, which should be implemented based on the security requirements of
 * the application.
 */
void setupWiFi(CPXPacket_t *txPacket) {
    cpxPrintToConsole(LOG_TO_CRTP, "Setting up WiFi AP\n");

    // Set up the routing for the WiFi CTRL packets
    cpxInitRoute(CPX_T_GAP8, CPX_T_ESP32, CPX_F_WIFI_CTRL, &txPacket->route);
    WiFiCTRLPacket_t *wifiCtrl =
        (WiFiCTRLPacket_t *)
            txPacket->data; // pointers to the same memory location

    // SSID
    wifiCtrl->cmd = WIFI_CTRL_SET_SSID;
    const char ssid[] = APP_NAME;
    memcpy(wifiCtrl->data, ssid, sizeof(ssid));
    txPacket->dataLength = sizeof(ssid);
    cpxSendPacketBlocking(txPacket);

    // TODO WiFi key

    // Connect
    wifiCtrl->cmd = WIFI_CTRL_WIFI_CONNECT;
    wifiCtrl->data[0] = 0x01;
    txPacket->dataLength = 2;
    cpxSendPacketBlocking(txPacket);
}

/**
 * @brief Transfers a JPEG image over CPX by sending the header, image data, and
 * footer sequentially.
 *
 * This function constructs and sends the image in three parts:
 * 1. The image header.
 * 2. The image data (JPEG format).
 * 3. The image footer.
 *
 * @param txPacket Pointer to the CPX packet structure used for transmission.
 * @param imgSize Total size of the image, including the header, image data, and
 * footer.
 * @param jpegData Pointer to the JPEG image data to be transferred.
 * @param jpegSize Size of the JPEG image data in bytes.
 * @param headerData Pointer to the header data that precedes the JPEG image.
 * @param headerSize Size of the header data in bytes.
 * @param footerData Pointer to the footer data that follows the JPEG image.
 * @param footerSize Size of the footer data in bytes.
 */
void transferJpegImage(CPXPacket_t *txPacket, uint32_t imgSize,
                       uint8_t *jpegData, uint32_t jpegSize,
                       uint8_t *headerData, uint32_t headerSize,
                       uint8_t *footerData, uint32_t footerSize) {
    // Send information about the image
    createImageHeaderPacket(txPacket, imgSize, JPEG_FORMAT);
    cpxSendPacketBlocking(txPacket);

    // Send header
    memcpy(txPacket->data, headerData, headerSize);
    txPacket->dataLength = headerSize;
    cpxSendPacketBlocking(txPacket);

    // Send image data
    sendBufferViaCPX(txPacket, jpegData, jpegSize);

    // Send footer
    memcpy(txPacket->data, footerData, footerSize);
    txPacket->dataLength = footerSize;
    cpxSendPacketBlocking(txPacket);
}

/**
 * @brief Transfers a raw image over CPX by sending a header packet followed by
 * the image data.
 *
 * This function sends a raw image buffer through the CPX communication
 * protocol. It first creates and sends a header packet that contains
 * information about the image size. Then, it sends the actual image data from
 * the provided buffer.
 *
 * @param txPacket A pointer to the CPXPacket_t structure used for sending the
 * packets.
 * @param imgSize The size of the image data in bytes.
 * @param buffer A pointer to the raw image data buffer.
 */
void transferRawImage(CPXPacket_t *txPacket, uint32_t imgSize,
                      uint8_t *buff_img) {
    // Send information about the image
    createImageHeaderPacket(txPacket, imgSize, RAW_FORMAT);
    cpxSendPacketBlocking(txPacket);

    // Send the provided buffer
    sendBufferViaCPX(txPacket, buff_img, imgSize);
}