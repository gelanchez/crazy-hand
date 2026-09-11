ifndef GAP_SDK_HOME
  $(error Source sourceme in gap_sdk first)
endif

APP             = crazyflie-control
APP_CFLAGS 		+= -DAPP_NAME=\"CRAZY-HAND\"
# io			= uart
# io			= host

# Operating system
# PMSIS_OS 		= pulp_os
PMSIS_OS		= freertos

# App sources
APP_SRCS		+= ./src/main.c
APP_SRCS		+= ./src/utils.c
APP_SRCS		+= ./lib/cpx/src/com.c ./lib/cpx/src/cpx.c

# App includes
APP_INC	        += ./include
APP_INC	        += $(GAP_LIB_PATH)/include
APP_INC			+= ./lib/cpx/inc

# Compiler flags
APP_CFLAGS      += -O3 -g -Wall -Wextra -Iinclude

# Debug flag support
ifdef DEBUG
APP_CFLAGS      += -DDEBUG
endif

# FreeRTOS for lib timers for CPX
APP_CFLAGS 		+= -DconfigUSE_TIMERS=1 -DINCLUDE_xTimerPendFunctionCall=1

#! Choose VGA mode here
# Configure camera
APP_CFLAGS 		+= -DQVGA_MODE # QVGA format (324x244)
# APP_CFLAGS 		+= -DQQVGA_MODE # @ QQVGA format (164x124)

# Enable fallback VSYNC split-row detection and correction (normally never
# triggers with per-frame CMD_STOP/CMD_START; enable for regression testing).
# APP_CFLAGS      += -DDEBUG_SPLIT_DETECT

#! Choose encoding here
# Image encoding
APP_CFLAGS 		+= -DRAW_ENCODING
# APP_CFLAGS		+= -DJPEG_ENCODING
CONFIG_GAP_LIB_JPEG = 1

#! Choose JPEG quality (only applies when JPEG_ENCODING is set)
# Default is quality 50 (no flag). Valid options: JPEG_Q_0/10/20/50/90/95/100
# Lower = smaller files, higher FPS; higher = better image quality, lower FPS
# APP_CFLAGS		+= -DJPEG_Q_50

# Linker flags
APP_LDFLAGS     =

# Custom linker
APP_LINK_SCRIPT =

# Model
# include model.mk


$(info APP_SRCS... $(APP_SRCS))
$(info APP_CFLAGS... $(APP_CFLAGS))
RUNNER_CONFIG = $(CURDIR)/config.ini
include $(RULES_DIR)/pmsis_rules.mk