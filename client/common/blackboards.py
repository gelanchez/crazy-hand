"""Typed blackboard field definitions for the iceoryx2 /config service.

Each entry in CONFIG maps a parameter name to a BlackboardField that carries
its integer key, ctypes type, and default value.
"""

import ctypes
from dataclasses import dataclass

from client.common.constants import (
    CLAHE_ENABLED,
    FAST_SPEED_FACTOR,
    GESTURE_DEBOUNCE_MS,
    GESTURE_HYSTERESIS_MS,
    GESTURE_THRESHOLD,
    MAX_ALTITUDE,
    MIN_ALTITUDE,
    SPEED_FACTOR,
    TRACKING_ALT_SCALE,
    TRACKING_DISTANCE,
    TRACKING_DISTANCE_SCALE,
    TRACKING_HAND_SPAN_AT_1M,
    TRACKING_MAX_SPEED,
    TRACKING_SPEED_SCALE,
    YAW_RATE,
    YAW_RATE_FAST,
)


@dataclass
class BlackboardField:
    """Descriptor for a single blackboard entry: its numeric key, ctypes type, and default value."""

    key: object
    value_type: object
    default: object


def _bb(key, c_type, default):
    """Build a BlackboardField, wrapping key and default in their respective ctypes."""
    return BlackboardField(
        key=ctypes.c_uint64(key),
        value_type=c_type,
        default=c_type(default),
    )


_ENTRIES = [
    # (name,                        c_type,          default)
    ("save_images",                 ctypes.c_bool,   False),
    ("process_images",              ctypes.c_bool,   False),
    # Flight control
    ("speed_factor",                ctypes.c_float,  SPEED_FACTOR),
    ("fast_speed_factor",           ctypes.c_float,  FAST_SPEED_FACTOR),
    ("yaw_rate",                    ctypes.c_float,  YAW_RATE),
    ("yaw_rate_fast",               ctypes.c_float,  YAW_RATE_FAST),
    ("max_altitude",                ctypes.c_float,  MAX_ALTITUDE),
    ("min_altitude",                ctypes.c_float,  MIN_ALTITUDE),
    # Gesture
    ("gesture_threshold",           ctypes.c_float,  GESTURE_THRESHOLD),
    ("gesture_debounce_ms",         ctypes.c_int32,  GESTURE_DEBOUNCE_MS),
    ("gesture_hysteresis_ms",       ctypes.c_int32,  GESTURE_HYSTERESIS_MS),
    ("clahe_enabled",               ctypes.c_bool,   CLAHE_ENABLED),
    # Tracking
    ("tracking_max_speed",          ctypes.c_float,  TRACKING_MAX_SPEED),
    ("tracking_speed_scale",        ctypes.c_float,  TRACKING_SPEED_SCALE),
    ("tracking_alt_scale",          ctypes.c_float,  TRACKING_ALT_SCALE),
    ("tracking_distance",           ctypes.c_float,  TRACKING_DISTANCE),
    ("tracking_distance_scale",     ctypes.c_float,  TRACKING_DISTANCE_SCALE),
    ("tracking_hand_span_at_1m",    ctypes.c_float,  TRACKING_HAND_SPAN_AT_1M),
]

CONFIG = {name: _bb(key, c_type, default) for key, (name, c_type, default) in enumerate(_ENTRIES)}
