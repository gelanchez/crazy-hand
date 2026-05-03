/**
 * @file app.c
 * @brief App layer that communicates with the GAP8 on the AI deck and the
 * client via radio.
 */

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "app.h"
#include "app_channel.h"

#include "cpx.h"
#include "cpx_internal_router.h"

#include "FreeRTOS.h"
#include "task.h"

#define DEBUG_MODULE "app" // Used in debug.h
#include "debug.h"

enum Status // Enums are static by default in C
{
  STATUS_ZERO,
  STATUS_ONE,
  STATUS_TWO,
  STATUS_MAX
};

struct appPacketTX {
  enum Status stat;
} __attribute__((packed));

// Callback that is called when a CPX packet arrives
static void cpxPacketCallback(const CPXPacket_t *cpxRx) {
  DEBUG_PRINT("Got packet from GAP8 (%u)\n", cpxRx->data[0]);
}

void appMain() {
  DEBUG_PRINT("Starting app\n");

  // Register a callback for CPX packets.
  // Packets sent to destination=CPX_T_STM32 and function=CPX_F_APP will arrive
  // here
  cpxRegisterAppMessageHandler(cpxPacketCallback);

  // Radio packet
  struct appPacketTX txRadioPacket;
  txRadioPacket.stat = STATUS_ONE;

  // For communication from GAP8 to STM, see:
  // https://github.com/bitcraze/crazyflie-firmware/blob/master/examples/app_stm_gap8_cpx/src/stm_gap8_cpx.c
  // Needs implementing the GAP8 side.

  while (true) {
    vTaskDelay(M2T(2000));

    // Radio packets
    appchannelSendDataPacket(&txRadioPacket,
                             sizeof(txRadioPacket)); // Not block
    DEBUG_PRINT("Send packet %d\n", txRadioPacket.stat);
    txRadioPacket.stat =
        (txRadioPacket.stat + 1) % STATUS_MAX; // Iterate through status
  }
}