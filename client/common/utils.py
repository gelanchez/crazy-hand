import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

LOG_DIR = Path("data/logs")

_LEVEL_COLORS = {
    "DEBUG": "\033[94m",    # Blue
    "INFO": "\033[92m",     # Green
    "WARNING": "\033[93m",  # Yellow
    "ERROR": "\033[91m",    # Red
    "CRITICAL": "\033[95m", # Magenta
}

_RESET = "\033[0m"

_FMT = (
    "%(asctime)s.%(msecs)03.0f "
    "%(levelname)-7s "
    "[%(name)s:%(lineno)d] "
    "%(message)s"
)

_DATEFMT = "%H:%M:%S"


class _ColorFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        color = _LEVEL_COLORS.get(record.levelname, "")
        levelname = f"{record.levelname:<7}"
        colored_level = f"{color}{levelname}{_RESET}"
        return message.replace(levelname, colored_level, 1)


def setup_logging(name: str, level: int = logging.INFO, enable_file_logging: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    plain_formatter = logging.Formatter(_FMT, _DATEFMT)

    # CONSOLE HANDLER
    console_handler = logging.StreamHandler(sys.stdout)
    use_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()    
    if use_color:
        console_handler.setFormatter(_ColorFormatter(_FMT, _DATEFMT))
    else:
        console_handler.setFormatter(plain_formatter)
    logger.addHandler(console_handler)

    # FILE HANDLER
    if enable_file_logging:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / f"{name}.log",
            maxBytes=1_000_000, # 1 MB
            backupCount=1,
            encoding="utf-8")
        file_handler.setFormatter(plain_formatter)
        logger.addHandler(file_handler)

    return logger