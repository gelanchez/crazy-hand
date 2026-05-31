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
    key: object
    value_type: object
    default: object


def blackboard_bool(key, default=False):
    return BlackboardField(
        key=ctypes.c_uint64(key),
        value_type=ctypes.c_bool,
        default=ctypes.c_bool(default),
    )


def blackboard_float(key, default=0.0):
    return BlackboardField(
        key=ctypes.c_uint64(key),
        value_type=ctypes.c_float,
        default=ctypes.c_float(default),
    )


def blackboard_int(key, default=0):
    return BlackboardField(
        key=ctypes.c_uint64(key),
        value_type=ctypes.c_int32,
        default=ctypes.c_int32(default),
    )


_ENTRIES = [
    ("save_images",             blackboard_bool,  {"default": False}),
    ("process_images",          blackboard_bool,  {"default": False}),
    # Flight control
    ("speed_factor",            blackboard_float, {"default": SPEED_FACTOR}),
    ("fast_speed_factor",       blackboard_float, {"default": FAST_SPEED_FACTOR}),
    ("yaw_rate",                blackboard_float, {"default": YAW_RATE}),
    ("yaw_rate_fast",           blackboard_float, {"default": YAW_RATE_FAST}),
    ("max_altitude",            blackboard_float, {"default": MAX_ALTITUDE}),
    ("min_altitude",            blackboard_float, {"default": MIN_ALTITUDE}),
    # Gesture
    ("gesture_threshold",       blackboard_float, {"default": GESTURE_THRESHOLD}),
    ("gesture_debounce_ms",     blackboard_int,   {"default": GESTURE_DEBOUNCE_MS}),
    ("gesture_hysteresis_ms",   blackboard_int,   {"default": GESTURE_HYSTERESIS_MS}),
    ("clahe_enabled",           blackboard_bool,  {"default": CLAHE_ENABLED}),
    # Tracking
    ("tracking_max_speed",       blackboard_float, {"default": TRACKING_MAX_SPEED}),
    ("tracking_speed_scale",    blackboard_float, {"default": TRACKING_SPEED_SCALE}),
    ("tracking_alt_scale",      blackboard_float, {"default": TRACKING_ALT_SCALE}),
    ("tracking_distance",         blackboard_float, {"default": TRACKING_DISTANCE}),
    ("tracking_distance_scale",   blackboard_float, {"default": TRACKING_DISTANCE_SCALE}),
    ("tracking_hand_span_at_1m",  blackboard_float, {"default": TRACKING_HAND_SPAN_AT_1M}),
]

CONFIG = {name: factory(key, **kwargs) for key, (name, factory, kwargs) in enumerate(_ENTRIES)}
