"""Async-buffered QuestDB writer with periodic flushing, automatic reconnection, and old-data cleanup."""

import http.client
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from typing import Union

from questdb.ingress import IngressError, Sender

from client.common.constants import (
    QUESTDB_CONF,
    QUESTDB_SCRIPT,
    ActionSource,
    AppStatus,
    FlightCommand,
    FlightState,
)
from client.common.utils import setup_logging

logger = setup_logging("database")


@dataclass
class TelemetrySample:
    TABLE = "telemetry_cf"
    ts: datetime
    fps: float
    status: AppStatus
    # State estimate (Kalman filter)
    x: float = 0.0  # position m
    y: float = 0.0
    z: float = 0.0
    vx: float = 0.0  # velocity m/s
    vy: float = 0.0
    vz: float = 0.0
    roll: float = 0.0  # attitude deg
    pitch: float = 0.0
    yaw: float = 0.0
    # Motor (0–100 %)
    m1: int = 0
    m2: int = 0
    m3: int = 0
    m4: int = 0
    # Battery
    vbat: float = 0.0


@dataclass
class ActionSample:
    TABLE = "action_cf"
    ts: datetime
    active: bool
    command: FlightCommand
    state: FlightState
    source: ActionSource
    vx: float
    vy: float
    yawrate: float
    zdistance: float
    ema_x: float
    ema_y: float
    estimated_distance: float


@dataclass
class PerceptionSample:
    TABLE = "perception_cf"
    ts: datetime
    hand_detected: bool
    hand_x: int
    hand_y: int
    gesture_name: str
    gesture_confidence: float
    hand_span: float


Sample = Union[TelemetrySample, ActionSample, PerceptionSample]


class Database:
    """Buffers telemetry, action, and perception samples and flushes them to QuestDB on a background thread."""

    def __init__(
        self,
        conf: str = QUESTDB_CONF,
        precision: int = 2,
        flush_interval_s: float = 1.0,
    ):
        self.conf = conf
        self.precision = precision
        self.flush_interval_s = flush_interval_s

        self.closed = False
        self._stop = False

        self.sender = None

        self.buffers = {
            "telemetry_cf": [],
            "action_cf": [],
            "perception_cf": [],
        }

        self._connect()

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _connect(self) -> bool:
        if self.closed:
            return False

        try:
            if self.sender is not None:
                try:
                    self.sender.close()
                except Exception:
                    pass
                self.sender = None

            self.sender = Sender.from_conf(self.conf)
            self.sender.establish()
            logger.info("Connected to QuestDB")
            return True

        except Exception as e:
            self.sender = None
            logger.warning(f"QuestDB connection failed: {e}")
            return False

    def _worker(self):
        while not self._stop:
            time.sleep(self.flush_interval_s)
            try:
                self.flush()
            except Exception as e:
                logger.error(f"Flush worker error: {e}")

    def log(self, sample: Sample):
        """Serialize a sample into the corresponding table buffer; Enum/str fields become QuestDB symbols."""
        if self.closed:
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
                elif isinstance(val, bool):
                    columns[f.name] = val  # must precede int/float — bool is subclass of int
                else:
                    if isinstance(val, float):
                        val = round(val, self.precision)
                    columns[f.name] = val

            kwargs = {"at": sample.ts}
            if symbols:
                kwargs["symbols"] = symbols
            if columns:
                kwargs["columns"] = columns

            table = sample.TABLE

            if table not in self.buffers:
                self.buffers[table] = []

            self.buffers[table].append(kwargs)

        except Exception as e:
            logger.error(f"Unexpected error during logging: {e}")

    def flush(self):
        """Send all buffered rows to QuestDB, reconnecting first if the sender is unavailable."""
        if self.sender is None:
            if not self._connect():
                return

        try:
            for table in list(self.buffers.keys()):
                self._flush_table(table)

        except Exception as e:
            logger.error(f"Unexpected flush error: {e}")
            self.sender = None

    def _flush_table(self, table: str):
        """Atomically swap the named table's buffer and write its rows to QuestDB, restoring rows on failure."""
        rows = self.buffers.get(table)

        if not rows:
            return

        # swap buffer immediately (non-blocking logger)
        self.buffers[table] = []

        try:
            sender = self.sender

            for row in rows:
                sender.row(table, **row)

            sender.flush()

            logger.debug(f"Flushed {len(rows)} rows -> {table}")

        except IngressError as e:
            logger.error(f"Flush error for {table}: {e}")
            self.sender = None

            # restore lost data, preserving chronological order
            self.buffers[table] = rows + self.buffers[table]

    def close(self):
        if self.closed:
            return

        self._stop = True
        self.closed = True  # worker checks this; stops immediately on next tick

        try:
            self._thread.join(timeout=2)
        except Exception:
            pass

        # final flush after worker is stopped
        try:
            self.flush()
        except Exception:
            pass

        if self.sender:
            try:
                self.sender.close()
            except Exception:
                pass

        self.sender = None

    # --- QuestDB startup ---

    @staticmethod
    def is_questdb_running(host="127.0.0.1", port=9000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex((host, port)) == 0

    @staticmethod
    def start_questdb():
        if Database.is_questdb_running():
            logger.info("QuestDB already running")
            return

        logger.info("Starting QuestDB...")

        result = subprocess.run(
            [str(QUESTDB_SCRIPT), "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.stderr:
            logger.warning(result.stderr.strip())

        if result.returncode != 0:
            raise RuntimeError("Failed to start QuestDB")

        timeout_s = 5.0
        start = time.time()

        while time.time() - start < timeout_s:
            if Database.is_questdb_running():
                logger.info("QuestDB started successfully")
                return
            time.sleep(0.5)

        raise RuntimeError("QuestDB did not become ready in time")

    @staticmethod
    def cleanup_old_data(hours: int = 24 * 7):
        """Drop partitions older than ``hours`` from all known tables, skipping tables that do not exist yet."""
        tables = [
            "telemetry_cf",
            "action_cf",
            "perception_cf",
        ]

        for table in tables:
            try:
                # --- Existence probe (cheap, safe) ---
                probe_sql = f"SELECT count() FROM {table} LIMIT 1"
                Database._exec_sql(probe_sql)

            except Exception:
                # table likely does not exist → skip silently or debug log
                logger.debug(f"[{table}] does not exist, skipping cleanup")
                continue

            try:
                # --- Actual cleanup ---
                sql = f"""
                    ALTER TABLE {table}
                    DROP PARTITION WHERE timestamp < dateadd('h', -{hours}, now())
                """

                Database._exec_sql(sql)
                logger.info(f"[{table}] cleanup executed (> {hours}h)")

            except Exception as e:
                # real cleanup failure (not existence issue)
                logger.warning(f"[{table}] cleanup failed: {e}")

    @staticmethod
    def _exec_sql(sql: str, host="127.0.0.1", port=9000) -> str:
        """Execute a SQL statement against QuestDB's HTTP ``/exec`` endpoint and return the raw JSON response."""
        conn = http.client.HTTPConnection(host, port, timeout=3)

        path = "/exec?query=" + urllib.parse.quote(sql)

        conn.request("GET", path)
        resp = conn.getresponse()

        data = resp.read().decode(errors="ignore")
        conn.close()

        if resp.status != 200:
            raise RuntimeError(f"QuestDB HTTP error {resp.status}: {data}")

        return data
