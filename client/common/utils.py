import logging
import sys

from common.constants import IOX2_CONFIG

_LEVEL_COLORS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[1;31m", # bold red
}
_RESET = "\033[0m"

_FMT = "%(asctime)s.%(msecs)03.0f %(levelname)-8s [%(name)s:%(lineno)d] %(message)s"
_DATEFMT = "%H:%M:%S"


class _ColorFormatter(logging.Formatter):
    def format(self, record):
        try:
            color = _LEVEL_COLORS.get(record.levelname, "")
            record = logging.makeLogRecord(record.__dict__)
            record.levelname = f"{color}{record.levelname}{_RESET}"
            return super().format(record)
        except (BrokenPipeError, IOError):
            return ""


def setup_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    use_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    formatter = _ColorFormatter(_FMT, _DATEFMT) if use_color else logging.Formatter(_FMT, _DATEFMT)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    return logging.getLogger(name)


def setup_iceoryx2_config() -> None:
    import iceoryx2
    iceoryx2.config.setup_global_config_from_file(iceoryx2.FilePath.new(str(IOX2_CONFIG)))

