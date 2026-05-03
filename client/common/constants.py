from enum import Enum
from pathlib import Path

# Connection Constants
CRAZYFLIE_IP = "192.168.4.1"
CRAZYFLIE_PORT = 5000
CRAZYFLIE_URI = f"tcp://{CRAZYFLIE_IP}:{CRAZYFLIE_PORT}"

# iceoryx2 services
IMAGE_SERVICE = "image"
TELEMETRY_SERVICE = "telemetry"
COMMAND_SERVICE = "command"

# iceoryx2 events
class EventId(Enum):
    # 0 is reserved for dead notifiers
    IMAGE_READY_EVENT = 1
    TELEMETRY_READY_EVENT = 2
    COMMAND_READY_EVENT = 3

IOX2_CONFIG = Path(__file__).parent / "iceoryx2.toml"

# Image Configuration
IMAGE_WIDTH = 324
IMAGE_HEIGHT = 244
IMAGE_SIZE = IMAGE_WIDTH * IMAGE_HEIGHT

# Flight control
SPEED_FACTOR = 0.3   # m/s for vx/vy
DEFAULT_HEIGHT = 0.3  # metres for initial take-off

