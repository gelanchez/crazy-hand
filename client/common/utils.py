import atexit
import logging
import queue
import shutil
import sys
import time
from collections import deque
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("data/logs")

_LEVEL_COLORS = {
    "DEBUG": "\033[94m",  # Blue
    "INFO": "\033[92m",  # Green
    "WARNING": "\033[93m",  # Yellow
    "ERROR": "\033[91m",  # Red
    "CRITICAL": "\033[95m",  # Magenta
}

_RESET = "\033[0m"

_FMT = "%(asctime)s.%(msecs)03.0f %(levelname)-7s [%(name)s:%(lineno)d] %(message)s"

_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _ColorFormatter(logging.Formatter):
    def format(self, record):
        color = _LEVEL_COLORS.get(record.levelname, "")
        record = logging.makeLogRecord(record.__dict__)
        record.levelname = f"{color}{record.levelname:<7}{_RESET}"
        return super().format(record)


def setup_logging(
    name: str,
    level: int = logging.INFO,
    console_level: int = logging.INFO,
    enable_file_logging: bool = True,
) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    plain_formatter = logging.Formatter(_FMT, _DATEFMT)

    # Console handler — limited to console_level to suppress high-frequency DEBUG
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    use_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if use_color:
        console_handler.setFormatter(_ColorFormatter(_FMT, _DATEFMT))
    else:
        console_handler.setFormatter(plain_formatter)
    logger.addHandler(console_handler)

    # Async file handler — background thread writes to disk, zero latency on callers
    if enable_file_logging:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / f"{name}.log",
            maxBytes=3_000_000,  # 3 MB
            backupCount=1,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(plain_formatter)

        log_queue: queue.Queue = queue.Queue()  # unbounded; background thread drains it
        queue_handler = QueueHandler(log_queue)
        queue_handler.setLevel(logging.NOTSET)  # pass all; file_handler does filtering
        logger.addHandler(queue_handler)

        listener = QueueListener(log_queue, file_handler, respect_handler_level=True)
        listener.start()
        atexit.register(listener.stop)

    return logger


class FPSCounter:
    def __init__(self, window_size: int = 30, timeout: float = 1.0):
        self.window_size = window_size
        self.timeout = timeout
        self.last_time = None
        self.frame_intervals = deque(maxlen=window_size)
        self._fps = 0.0

    def update(self) -> float:
        now = time.perf_counter()
        if self.last_time is not None:
            dt = now - self.last_time

            if dt > self.timeout:
                self.frame_intervals.clear()  # stall — discard history and skip this interval
            elif dt > 0:
                self.frame_intervals.append(dt)
                average_dt = sum(self.frame_intervals) / len(self.frame_intervals)
                self._fps = 1.0 / average_dt

        self.last_time = now
        return self._fps

    def reset(self):
        self.last_time = None
        self.frame_intervals.clear()
        self._fps = 0.0

    @property
    def fps(self):
        if (
            self.last_time is None
            or time.perf_counter() - self.last_time > self.timeout
        ):
            return 0.0
        return self._fps


def cleanup_iceoryx2():
    """Cleans stale iceoryx2 shared memory and temp files.
    Safe to run at startup when no nodes are running.
    """
    logger = setup_logging("main")

    for path in Path("/dev/shm").glob("iox2_*"):
        try:
            path.unlink()
            logger.info(f"Removed shared memory: {path}")
        except Exception as e:
            logger.debug(f"Could not remove {path}: {e}")

    tmp_dir = Path("/tmp/iceoryx2")
    if tmp_dir.exists():
        try:
            shutil.rmtree(tmp_dir)
            logger.info("Removed /tmp/iceoryx2")
        except Exception as e:
            logger.debug(f"Could not remove /tmp/iceoryx2: {e}")
