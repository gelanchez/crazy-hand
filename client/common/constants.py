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
    PERCEPTION = "/perception"
    TELEMETRY = "/telemetry"


# iceoryx2 events
@unique
class EventId(IntEnum):
    # 0 is reserved for dead notifiers
    ACTION_READY = 1
    COMMAND_READY = auto()
    IMAGE_READY = auto()
    PERCEPTION_READY = auto()
    TELEMETRY_READY = auto()


# Image Configuration
IMAGE_WIDTH = 324
IMAGE_HEIGHT = 244
IMAGE_SIZE = IMAGE_WIDTH * IMAGE_HEIGHT


@unique
class AppStatus(IntEnum):
    DISCONNECTED = 0
    CONNECTED = auto()
    SIMULATING = auto()


# GUI
IMAGE_SCALING_FACTOR = 2  # 1 for original, 2 for upscaled

# Flight control
SPEED_FACTOR = 0.3       # m/s vx/vy normal
FAST_SPEED_FACTOR = 0.6  # m/s vx/vy with Shift — untested, tune as needed
DEFAULT_HEIGHT = 0.3     # m initial take-off altitude
ALTITUDE_STEP = 0.1      # m per W/S press
ALTITUDE_STEP_FAST = 0.2 # m per Shift+W/S press — untested
YAW_RATE = 70.0          # deg/s normal yaw — untested, tune as needed
YAW_RATE_FAST = 200.0    # deg/s Shift yaw — untested


@unique
class KeyCode(IntEnum):
    NONE = 0
    SPACE = auto()
    ESC = auto()
    WINDOW_CLOSED = auto()
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    W = auto()
    A = auto()
    S = auto()
    D = auto()
    T = auto()
    C = auto()


@unique
class FlightState(IntEnum):
    IDLE = 0
    AIRBORNE = auto()
    TRACKING = auto()


@unique
class FlightCommand(IntEnum):
    NONE = 0
    TAKEOFF = auto()
    LAND = auto()
    EMERGENCY_STOP = auto()
    TOGGLE_TRACKING = auto()
