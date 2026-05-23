import ctypes

from client.common.constants import IMAGE_SIZE, AppStatus, FlightCommand


class ActionData(ctypes.Structure):
    _fields_ = [
        ("active", ctypes.c_bool),
        ("command", ctypes.c_uint8),  # FlightCommand
        ("state", ctypes.c_uint8),  # FlightState
        ("source", ctypes.c_uint8),  # ActionSource
        ("vx", ctypes.c_float),
        ("vy", ctypes.c_float),
        ("yawrate", ctypes.c_float),
        ("zdistance", ctypes.c_float),
        ("ema_x", ctypes.c_float),  # EMA-filtered hand x (0.0 when not tracking)
        ("ema_y", ctypes.c_float),  # EMA-filtered hand y (0.0 when not tracking)
    ]

    def __str__(self) -> str:
        cmd = FlightCommand(self.command).name
        return (
            f"ActionData(active={self.active}, cmd={cmd}, vx={self.vx:.2f}, "
            f"vy={self.vy:.2f}, yaw={self.yawrate:.1f}, z={self.zdistance:.2f})"
        )


class CommandData(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint8),
        ("is_pressed", ctypes.c_bool),
        ("shift", ctypes.c_bool),
    ]

    def __str__(self) -> str:
        return f"CommandData(key={self.key}, is_pressed={self.is_pressed}, shift={self.shift})"


class ImageData(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64),
        ("timestamp", ctypes.c_uint64),
        ("pixels", ctypes.c_ubyte * IMAGE_SIZE),
    ]

    def __str__(self) -> str:
        return f"ImageData(id={self.id}, timestamp={self.timestamp}, size={len(self.pixels)})"


class PerceptionData(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64),
        ("timestamp", ctypes.c_uint64),
        ("hand_detected", ctypes.c_bool),
        ("hand_x", ctypes.c_uint16),
        ("hand_y", ctypes.c_uint16),
        ("gesture_name", ctypes.c_char * 32),
        ("gesture_confidence", ctypes.c_float),
        ("processed_pixels", ctypes.c_ubyte * (IMAGE_SIZE * 3)),
    ]

    def __str__(self) -> str:
        if self.hand_detected:
            return f"PerceptionData(gesture='{self.gesture_name.decode('utf-8')}', confidence={self.gesture_confidence:.2f})"
        return "PerceptionData(no hand detected)"


class TelemetryData(ctypes.Structure):
    _fields_ = [
        ("status", ctypes.c_uint8),
        ("fps", ctypes.c_float),
    ]

    def __str__(self) -> str:
        try:
            status_str = AppStatus(self.status).name
        except ValueError:
            status_str = "UNKNOWN"
        return f"TelemetryData(status={status_str}, fps={self.fps:.1f})"
