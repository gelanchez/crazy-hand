import ctypes
from dataclasses import dataclass


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


_ENTRIES = [
    ("save_images", blackboard_bool, {"default": False}),
    ("process_images", blackboard_bool, {"default": False}),
]

CONFIG = {name: factory(key, **kwargs) for key, (name, factory, kwargs) in enumerate(_ENTRIES)}
