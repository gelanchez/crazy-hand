#!/usr/bin/env bash
#
# flash.sh - Over-the-air (OTA) flashing tool for the AI-deck firmware.
#
# Usage: ./flash.sh [image_path] [target] [uri]
#
# Default values follow README instructions:
#   Image:  BUILD/GAP8_V2/GCC_RISCV_FREERTOS/flash.img
#   Target: deck-bcAI:gap8-fw
#   URI:    radio://0/80/2M/E7E7E7E7E7
#
# Required: cfloader (from crazyflie-clients-python) must be in PATH.
#
set -e

# Default parameters as defined in README
DEFAULT_IMAGE="BUILD/GAP8_V2/GCC_RISCV_FREERTOS/flash.img"
DEFAULT_TARGET="deck-bcAI:gap8-fw"
DEFAULT_URI="radio://0/80/2M/E7E7E7E7E7"

# Use provided arguments or defaults
IMAGE="${1:-$DEFAULT_IMAGE}"
TARGET="${2:-$DEFAULT_TARGET}"
URI="${3:-$DEFAULT_URI}"

echo "Flashing AI-deck..."
echo "  Image:  $IMAGE"
echo "  Target: $TARGET"
echo "  URI:    $URI"

cfloader flash "$IMAGE" "$TARGET" -w "$URI"
