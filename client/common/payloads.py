

import ctypes

from common.constants import IMAGE_SIZE


class ImageData(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64),
        ("timestamp", ctypes.c_uint64),
        ("pixels", ctypes.c_ubyte * IMAGE_SIZE),
    ]

    def __str__(self) -> str:
        return f"ImageData(id={self.id}, timestamp={self.timestamp}, size={len(self.pixels)})"


class CommandData(ctypes.Structure):
    _fields_ = [
        ("vx",        ctypes.c_float),
        ("vy",        ctypes.c_float),
        ("yawrate",   ctypes.c_float),
        ("zdistance", ctypes.c_float),
        ("active",    ctypes.c_uint8),
        ("_pad",      ctypes.c_uint8 * 3),
    ]

    def __str__(self) -> str:
        return (
            f"CommandData(active={self.active}, vx={self.vx:.2f}, vy={self.vy:.2f}, "
            f"yaw={self.yawrate:.1f}, z={self.zdistance:.2f})"
        )

