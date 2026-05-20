from dataclasses import dataclass, fields
from datetime import datetime
from typing import Union

from enum import Enum

from questdb.ingress import IngressError, Sender

from client.common.constants import AppStatus
from client.common.utils import setup_logging

logger = setup_logging("database")


@dataclass
class TelemetrySample:
    ts: datetime
    fps: float
    status: AppStatus


@dataclass
class ActionSample:
    ts: datetime
    active: bool
    vx: float
    vy: float
    yawrate: float
    zdistance: float


@dataclass
class PerceptionSample:
    ts: datetime
    hand_detected: bool
    hand_x: int
    hand_y: int
    gesture_name: str
    gesture_confidence: float


class Database:
    def __init__(
        self,
        conf: str = "http::addr=localhost:9000;username=admin;password=quest;",
        table_name: str = "telemetry_cf",
        max_buffer: int = 10,
        precision: int = 2,
    ):
        self.conf = conf
        self.table_name = table_name
        self.max_buffer = max_buffer
        self.precision = precision
        self.closed = False
        self._connected = False
        self._buffered_count = 0
        self.sender = None

        self._connect()

    def _connect(self) -> bool:
        if self.closed:
            return False
        try:
            self.sender = Sender.from_conf(self.conf)
            self.sender.establish()
            self._connected = True
            logger.info("Successfully connected and established connection to QuestDB.")
            return True
        except IngressError as e:
            self._connected = False
            self.sender = None
            logger.warning(f"Could not connect to QuestDB: {e}. Will retry dynamically on next log/flush.")
            return False
        except Exception as e:
            self._connected = False
            self.sender = None
            logger.error(f"Unexpected error connecting to QuestDB: {e}")
            return False

    def log(self, sample: Union[TelemetrySample, ActionSample, PerceptionSample]):
        if self.closed:
            return

        if not self._connected:
            if not self._connect():
                return

        try:
            columns = {}
            symbols = {}
            for f in fields(sample):
                if f.name == "ts":
                    continue
                val = getattr(sample, f.name)
                if isinstance(val, Enum):
                    symbols[f.name] = val.name
                elif isinstance(val, str):
                    symbols[f.name] = val
                else:
                    if isinstance(val, float):
                        val = round(val, self.precision)
                    columns[f.name] = val

            kwargs = {"at": sample.ts}
            if symbols:
                kwargs["symbols"] = symbols
            if columns:
                kwargs["columns"] = columns

            self.sender.row(
                self.table_name,
                **kwargs
            )
            self._buffered_count += 1

            if self._buffered_count >= self.max_buffer:
                self.flush()
        except IngressError as e:
            logger.error(f"QuestDB ingress error during logging: {e}. Resetting connection.")
            self._connected = False
            self.sender = None
        except Exception as e:
            logger.error(f"Unexpected error during QuestDB logging: {e}")

    def flush(self):
        if self.closed:
            return

        if not self._connected or not self.sender:
            return

        if self._buffered_count == 0:
            return

        try:
            self.sender.flush()
            self._buffered_count = 0
        except IngressError as e:
            logger.error(f"QuestDB ingress error during flush: {e}. Resetting connection.")
            self._connected = False
            self.sender = None
        except Exception as e:
            logger.error(f"Unexpected error during QuestDB flush: {e}")

    def close(self):
        if self.closed:
            return

        self.closed = True

        if self._connected and self.sender:
            try:
                self.flush()
            except Exception:
                pass
            try:
                self.sender.close()
            except Exception:
                pass

        self.sender = None
        self._connected = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()