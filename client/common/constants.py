from enum import IntEnum, StrEnum, auto, unique
from pathlib import Path

IOX2_CONFIG = Path(__file__).parent / "iceoryx2.toml"

# Connection Constants
CRAZYFLIE_IP = "192.168.4.1"
CRAZYFLIE_PORT = 5000
CRAZYFLIE_URI = f"tcp://{CRAZYFLIE_IP}:{CRAZYFLIE_PORT}"

# iceoryx2 service names
@unique
class ServiceName(StrEnum):
    ACTION = "/action"
    COMMAND = "/command"
    IMAGE = "/image"
    PROCESSED = "/processed"
    TELEMETRY = "/telemetry"

# iceoryx2 events
@unique
class EventId(IntEnum):
    # 0 is reserved for dead notifiers
    ACTION_READY = auto()
    COMMAND_READY = auto()
    IMAGE_READY = auto()
    PROCESSED_READY = auto()
    TELEMETRY_READY = auto()

# Image Configuration
IMAGE_WIDTH = 324
IMAGE_HEIGHT = 244
IMAGE_SIZE = IMAGE_WIDTH * IMAGE_HEIGHT

@unique
class AppStatus(IntEnum):
    DISCONNECTED = 0
    CONNECTED = 1
    SIMULATING = 2
    INITIALIZING = 3
    WAITING = 4

# GUI
IMAGE_SCALING_FACTOR = 2 # 1 for original, 2 for upscaled

# Flight control
SPEED_FACTOR = 0.3   # m/s for vx/vy
DEFAULT_HEIGHT = 0.3  # metres for initial take-off

@unique
class KeyCode(IntEnum):
    NONE = 0
    W = auto()
    S = auto()
    A = auto()
    D = auto()
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    Z = auto()
    X = auto()
    SPACE = auto()
    ESC = auto()
    WINDOW_CLOSED = auto()
