"""ctypes struct definitions used as shared-memory payloads for iceoryx2 IPC between the drone client nodes."""

import ctypes

from client.common.constants import IMAGE_SIZE, AppStatus, FlightCommand, ImageFormat


class ActionData(ctypes.Structure):
    """Shared-memory payload carrying the current flight command and derived motion setpoints issued to the drone."""

    _fields_ = [
        ("active", ctypes.c_bool),
        ("command", ctypes.c_uint8),  # FlightCommand
        ("state", ctypes.c_uint8),  # FlightState
        ("source", ctypes.c_uint8),  # ActionSource
        ("vx", ctypes.c_float),
        ("vy", ctypes.c_float),
        ("yawrate", ctypes.c_float),
        ("zdistance", ctypes.c_float),
        ("ema_x", ctypes.c_float),           # EMA-filtered hand x (0.0 when not tracking)
        ("ema_y", ctypes.c_float),           # EMA-filtered hand y (0.0 when not tracking)
        ("estimated_distance", ctypes.c_float),  # m from hand (0.0 when not tracking)
        ("thrust", ctypes.c_uint16),         # raw thrust for MOTOR_TEST (0–65535; 0 otherwise)
    ]

    def __str__(self) -> str:
        cmd = FlightCommand(self.command).name
        return (
            f"ActionData(active={self.active}, cmd={cmd}, vx={self.vx:.2f}, "
            f"vy={self.vy:.2f}, yaw={self.yawrate:.1f}, z={self.zdistance:.2f})"
        )


class CommandData(ctypes.Structure):
    """Shared-memory payload representing a single raw keyboard event (key code, pressed state, and shift modifier)."""

    _fields_ = [
        ("key", ctypes.c_uint8),
        ("is_pressed", ctypes.c_bool),
        ("shift", ctypes.c_bool),
    ]

    def __str__(self) -> str:
        return f"CommandData(key={self.key}, is_pressed={self.is_pressed}, shift={self.shift})"


class ImageData(ctypes.Structure):
    """Shared-memory payload carrying a raw or JPEG-encoded camera frame together with its sequence id and timestamp."""

    _fields_ = [
        ("id", ctypes.c_uint64),
        ("timestamp", ctypes.c_uint64),
        ("format", ctypes.c_uint8),  # ImageFormat: 0=RAW, 1=JPEG
        ("pixels", ctypes.c_ubyte * IMAGE_SIZE),
    ]

    def __str__(self) -> str:
        fmt = ImageFormat(self.format).name if self.format in ImageFormat._value2member_map_ else self.format
        return f"ImageData(id={self.id}, timestamp={self.timestamp}, format={fmt})"


GESTURE_NAME_SIZE = 32


class PerceptionData(ctypes.Structure):
    """Shared-memory payload produced by the perception node, containing hand-tracking results and the annotated frame."""

    _fields_ = [
        ("id", ctypes.c_uint64),
        ("timestamp", ctypes.c_uint64),
        ("format", ctypes.c_uint8),  # ImageFormat: propagated from ImageData
        ("hand_detected", ctypes.c_bool),
        ("hand_x", ctypes.c_uint16),
        ("hand_y", ctypes.c_uint16),
        ("gesture_name", ctypes.c_char * GESTURE_NAME_SIZE),
        ("gesture_confidence", ctypes.c_float),
        ("hand_span", ctypes.c_float),   # normalised wrist-to-middle-fingertip span (0 = not detected)
        ("processed_pixels", ctypes.c_ubyte * (IMAGE_SIZE * 3)),
    ]

    def __str__(self) -> str:
        if self.hand_detected:
            return f"PerceptionData(gesture='{self.gesture_name.decode('utf-8')}', confidence={self.gesture_confidence:.2f})"
        return "PerceptionData(no hand detected)"


class TelemetryData(ctypes.Structure):
    """Shared-memory payload streaming drone telemetry: app status, Kalman state estimate, motor outputs, and battery voltage."""

    _fields_ = [
        ("status", ctypes.c_uint8),
        ("fps", ctypes.c_float),
        # State estimate (Kalman filter)
        ("x", ctypes.c_float),  # position m
        ("y", ctypes.c_float),
        ("z", ctypes.c_float),
        ("vx", ctypes.c_float),  # velocity m/s
        ("vy", ctypes.c_float),
        ("vz", ctypes.c_float),
        ("roll", ctypes.c_float),  # attitude deg
        ("pitch", ctypes.c_float),
        ("yaw", ctypes.c_float),
        # Motor (0–100 %)
        ("m1", ctypes.c_uint16),
        ("m2", ctypes.c_uint16),
        ("m3", ctypes.c_uint16),
        ("m4", ctypes.c_uint16),
        # Battery
        ("vbat", ctypes.c_float),
    ]

    def __str__(self) -> str:
        try:
            status_str = AppStatus(self.status).name
        except ValueError:
            status_str = "UNKNOWN"
        return (
            f"TelemetryData(status={status_str}, fps={self.fps:.1f}, "
            f"pos=({self.x:.2f},{self.y:.2f},{self.z:.2f}), "
            f"att=({self.roll:.1f},{self.pitch:.1f},{self.yaw:.1f}), "
            f"motors=({self.m1},{self.m2},{self.m3},{self.m4}), "
            f"vbat={self.vbat:.2f}V)"
        )
