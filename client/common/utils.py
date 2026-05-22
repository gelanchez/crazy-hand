import atexit
import logging
import queue
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
        message = super().format(record)
        color = _LEVEL_COLORS.get(record.levelname, "")
        levelname = f"{record.levelname:<7}"
        colored_level = f"{color}{levelname}{_RESET}"
        return message.replace(levelname, colored_level, 1)


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

    # CONSOLE HANDLER — limited to console_level to suppress high-frequency DEBUG
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    use_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if use_color:
        console_handler.setFormatter(_ColorFormatter(_FMT, _DATEFMT))
    else:
        console_handler.setFormatter(plain_formatter)
    logger.addHandler(console_handler)

    # ASYNC FILE HANDLER — background thread writes to disk, zero latency on callers
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

        log_queue: queue.Queue = queue.Queue(-1)  # unbounded; background thread drains it
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

            # Reset if stream stalled
            if dt > self.timeout:
                self.frame_intervals.clear()

            if dt > 0:
                self.frame_intervals.append(dt)
                avg_dt = sum(self.frame_intervals) / len(self.frame_intervals)
                self._fps = 1.0 / avg_dt

        self.last_time = now
        return self._fps

    def reset(self):
        self.last_time = None
        self.frame_intervals.clear()
        self._fps = 0.0

    @property
    def fps(self):
        return self._fps
