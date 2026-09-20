/**
 * @file utils.h
 * @brief Utils functions.
 */

#ifndef __UTILS_H__
#define __UTILS_H__

#include "cpx.h" // CPX communications
#include "pmsis.h"
#include <stdint.h>

// QVGA format = 324 x 244 (extra 4 is from padding)
#ifdef QVGA_MODE
#define IMG_WIDTH 324
#define IMG_HEIGHT 244
#endif

// QQVGA = 164 x 124 (extra 4 is from padding)
#ifdef QQVGA_MODE
#define IMG_WIDTH 164
#define IMG_HEIGHT 124
#endif

// Custom 324 x 324 (extra 4 is from padding)
#if !defined(QVGA_MODE) && !defined(QQVGA_MODE)
#define IMG_WIDTH 324
#define IMG_HEIGHT 324
#endif

#define IMG_SIZE (IMG_WIDTH * IMG_HEIGHT)

// Camera himax.h not included when compiling for FreeRTOS
// gap_sdk/rtos/pmsis/bsp/include/bsp/camera/himax.h
#define HIMAX_IMG_ORIENTATION 0x0101
#define HIMAX_QVGA_WIN_EN 0x3010
#define HIMAX_VSYNC_HSYNC_PIXEL_SHIFT_EN 0x1012
#define HIMAX_AE_CTRL 0x2100

typedef struct {
    uint8_t magic;
    uint16_t width;
    uint16_t height;
    uint8_t depth;
    uint8_t format;
    uint32_t size;
} __attribute__((packed)) ImageHeader_t;

typedef enum {
    RAW_FORMAT = 0,
    JPEG_FORMAT = 1
} __attribute__((packed)) ImageFormat_t;

typedef enum {
    WIFI_CTRL_SET_SSID = 0x10,
    WIFI_CTRL_SET_KEY = 0x11,

    WIFI_CTRL_WIFI_CONNECT = 0x20,

    WIFI_CTRL_STATUS_WIFI_CONNECTED =
        0x31, // CF connected to access point (I think)
    WIFI_CTRL_STATUS_CLIENT_CONNECTED =
        0x32, // Client connected to AI-deck via WiFi
} __attribute__((packed)) WiFiCTRLType_t;

typedef struct {
    WiFiCTRLType_t cmd;
    uint8_t data[50];
} __attribute__((packed)) WiFiCTRLPacket_t;

void createImageHeaderPacket(CPXPacket_t *packet, uint32_t img_size,
                             ImageFormat_t img_format);

int setupCamera(struct pi_device *device);

void sendBufferViaCPX(CPXPacket_t *packet, uint8_t *buffer,
                      uint32_t buffer_size);

void setupWiFi(CPXPacket_t *tx_packet);

void transferJpegImage(CPXPacket_t *tx_packet, uint32_t img_size,
                       uint8_t *jpeg_data, uint32_t jpeg_size,
                       uint8_t *header_data, uint32_t header_size,
                       uint8_t *footer_data, uint32_t footer_size);

void transferRawImage(CPXPacket_t *tx_packet, uint32_t img_size,
                      uint8_t *buff_img);

#endif