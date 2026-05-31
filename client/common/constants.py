import logging
from enum import IntEnum, StrEnum, auto, unique
from pathlib import Path

# Logging — per-logger level overrides (file_level, console_level).
# Keys match the `name` passed to setup_logging(). Unlisted loggers use caller default.
LOG_LEVELS: dict[str, tuple[int, int]] = {
    "main": (logging.INFO, logging.INFO),
    "database": (logging.INFO, logging.INFO),
    "control_node": (logging.DEBUG, logging.DEBUG),
    "vision_node": (logging.DEBUG, logging.DEBUG),
    "wifi_node": (logging.DEBUG, logging.INFO),
    "gui_node": (logging.DEBUG, logging.DEBUG),
    "gui_command": (logging.DEBUG, logging.DEBUG),
    "logger_node": (logging.INFO, logging.INFO),
}

# Paths
IOX2_CONFIG = Path(__file__).parent / "iceoryx2.toml"
QUESTDB_SCRIPT = Path.home() / "apps/questdb-9.3.5-rt-linux-x86-64/bin/questdb.sh"
QUESTDB_CONF = "tcp::addr=127.0.0.1:9009;"

# Network
CRAZYFLIE_IP = "192.168.4.1"
CRAZYFLIE_PORT = 5000
CRAZYFLIE_URI = f"tcp://{CRAZYFLIE_IP}:{CRAZYFLIE_PORT}"


# IPC
@unique
class ServiceName(StrEnum):
    ACTION = "/action"
    COMMAND = "/command"
    IMAGE = "/image"
    PERCEPTION = "/perception"
    TELEMETRY = "/telemetry"


@unique
class EventId(IntEnum):
    # 0 is reserved for dead notifiers
    ACTION_READY = 1
    COMMAND_READY = auto()
    IMAGE_READY = auto()
    PERCEPTION_READY = auto()
    TELEMETRY_READY = auto()


# Image
IMAGE_WIDTH = 324
IMAGE_HEIGHT = 244
IMAGE_SIZE = IMAGE_WIDTH * IMAGE_HEIGHT


@unique
class ImageFormat(IntEnum):
    RAW = 0
    JPEG = 1


# Flight
@unique
class AppStatus(IntEnum):
    DISCONNECTED = 0
    CONNECTED = auto()
    SIMULATING = auto()
    RECONNECTING = auto()


@unique
class FlightState(IntEnum):
    IDLE = 0
    AIRBORNE = auto()
    TRACKING = auto()
    LANDING = auto()
    MOTOR_TESTING = auto()


@unique
class FlightCommand(IntEnum):
    NONE = 0
    TAKEOFF = auto()
    LAND = auto()
    EMERGENCY_STOP = auto()
    TOGGLE_TRACKING = auto()
    MOTOR_TEST = auto()


@unique
class ActionSource(IntEnum):
    KEYBOARD = 0
    GESTURE = auto()
    TRACKING = auto()


# GUI
IMAGE_SCALING_FACTOR = 2  # 1 for original, 2 for upscaled


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
    M = auto()


# Flight control
SPEED_FACTOR = 0.3  # m/s vx/vy normal
FAST_SPEED_FACTOR = 0.6  # m/s vx/vy with Shift
DEFAULT_HEIGHT = 0.3  # m initial take-off altitude
ALTITUDE_STEP = 0.1  # m per W/S press
ALTITUDE_STEP_FAST = 0.2  # m per Shift+W/S press
YAW_RATE = 70.0  # deg/s normal yaw
YAW_RATE_FAST = 200.0  # deg/s Shift yaw
LAND_RATE = 0.2  # m/s controlled descent rate
LAND_CUTOFF = 0.05  # m — cut motors below this height
MAX_ALTITUDE = 2.0  # m ceiling for manual altitude control
MIN_ALTITUDE = 0.1  # m floor for manual altitude control
MOTOR_TEST_THRUST = 12000  # raw thrust for ground motor test (~18% — spins visibly, won't lift)
MOTOR_TEST_DURATION = 0.5  # seconds to run motor test

# Vision / gesture recognition
GESTURE_MIN_CONFIDENCE = 0.3  # MediaPipe hand detection / presence / tracking
GESTURE_THRESHOLD = 0.5  # minimum confidence to accept a gesture
GESTURE_DEBOUNCE_MS = 300  # stable time required before confirming gesture
GESTURE_HYSTERESIS_MS = 200  # cooldown to prevent rapid gesture switching
CLAHE_ENABLED = True
CLAHE_CLIP_LIMIT = 2.0  # max contrast amplification per tile (higher = more aggressive)
CLAHE_TILE_GRID_SIZE = (8, 8)  # grid of tiles for adaptive histogram equalisation

# Tracking
TRACKING_EMA_ALPHA = 0.3  # EMA smoothing factor for hand position (0=frozen, 1=raw)
TRACKING_LOSS_FRAMES = 5  # consecutive no-detection frames before hand considered lost
TRACKING_DEADZONE_PX = 20  # pixel radius around frame centre to ignore (no velocity)
TRACKING_MAX_SPEED = 0.15  # m/s lateral/forward cap during tracking (separate from manual SPEED_FACTOR)
TRACKING_SPEED_SCALE = 0.008  # m/s per pixel of lateral error (tune to taste)
TRACKING_ALT_SCALE = 0.0005  # m per pixel of vertical error per frame (tune to taste)
TRACKING_DISTANCE = 1.5  # m target distance from hand (0.0 = disabled)
TRACKING_DISTANCE_SCALE = 0.5  # m/s per metre of distance error
TRACKING_HAND_SPAN_AT_1M = 0.25  # calibration: normalised wrist-to-fingertip span at 1 m
