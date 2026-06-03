"""Async-buffered time-series writers for InfluxDB 3 Core (default) and QuestDB (QuestDBDatabase, legacy)."""

import http.client
import json
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Union

from questdb.ingress import IngressError, Sender

from client.common.constants import (
    INFLUXDB3_BINARY,
    INFLUXDB3_DATA_DIR,
    INFLUXDB3_DATABASE,
    INFLUXDB3_HOST,
    INFLUXDB3_RETENTION,
    INFLUXDB3_TOKEN,
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
    TABLE = "telemetry"
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
    TABLE = "action"
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
    TABLE = "perception"
    ts: datetime
    hand_detected: bool
    hand_x: int
    hand_y: int
    gesture_name: str
    gesture_confidence: float
    hand_span: float


Sample = Union[TelemetrySample, ActionSample, PerceptionSample]


class QuestDBDatabase:
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
            "telemetry": [],
            "action": [],
            "perception": [],
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
        if QuestDBDatabase.is_questdb_running():
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
            if QuestDBDatabase.is_questdb_running():
                logger.info("QuestDB started successfully")
                return
            time.sleep(0.5)

        raise RuntimeError("QuestDB did not become ready in time")

    @staticmethod
    def cleanup_old_data(hours: int = 24 * 7):
        """Drop partitions older than ``hours`` from all known tables, skipping tables that do not exist yet."""
        tables = [
            "telemetry",
            "action",
            "perception",
        ]

        for table in tables:
            try:
                # --- Existence probe (cheap, safe) ---
                probe_sql = f"SELECT count() FROM {table} LIMIT 1"
                QuestDBDatabase._exec_sql(probe_sql)

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

                QuestDBDatabase._exec_sql(sql)
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


# ---------------------------------------------------------------------------
# InfluxDB 3 Core
# ---------------------------------------------------------------------------


class InfluxDB3Database:
    """Async-buffered InfluxDB 3 Core writer with periodic flushing and automatic reconnection."""

    def __init__(
        self,
        host: str = INFLUXDB3_HOST,
        database: str = INFLUXDB3_DATABASE,
        token: str = INFLUXDB3_TOKEN,
        precision: int = 2,
        flush_interval_s: float = 1.0,
    ):
        self.database = database
        self.token = token
        self.precision = precision
        self.flush_interval_s = flush_interval_s

        parsed = urllib.parse.urlparse(host)
        self._http_host = parsed.hostname or "127.0.0.1"
        self._http_port = parsed.port or 8181

        if not token:
            logger.warning("INFLUXDB3_TOKEN is not set — all writes will fail with 401")

        self.closed = False
        self._stop = False

        self.buffers = {
            "telemetry": [],
            "action": [],
            "perception": [],
        }

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        logger.info(f"InfluxDB3Database ready ({self._http_host}:{self._http_port}, db={database})")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _worker(self):
        while not self._stop:
            time.sleep(self.flush_interval_s)
            try:
                self.flush()
            except Exception as e:
                logger.error(f"Flush worker error: {e}")

    def log(self, sample: Sample):
        """Serialize a sample to an ILP string and append to the table buffer."""
        if self.closed:
            return

        try:
            lp = self._to_line_protocol(sample)
            table = sample.TABLE

            if table not in self.buffers:
                self.buffers[table] = []

            self.buffers[table].append(lp)

        except Exception as e:
            logger.error(f"Unexpected error during logging: {e}")

    def _to_line_protocol(self, sample: Sample) -> str:
        """Convert a dataclass sample to an InfluxDB Line Protocol string.

        Enum/str fields → tags (indexed, low-cardinality).
        bool/int/float fields → fields (must precede int check since bool is a subclass of int).
        Timestamp is Unix nanoseconds embedded in the LP string.
        """
        field_parts = []

        for f in fields(sample):
            if f.name == "ts":
                continue

            val = getattr(sample, f.name)

            if isinstance(val, Enum):
                field_parts.append(f'{f.name}="{val.name}"')
            elif isinstance(val, str):
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                field_parts.append(f'{f.name}="{escaped or "none"}"')
            elif isinstance(val, bool):
                field_parts.append(f"{f.name}={'true' if val else 'false'}")
            elif isinstance(val, int):
                field_parts.append(f"{f.name}={val}i")
            elif isinstance(val, float):
                val = round(val, self.precision)
                field_parts.append(f"{f.name}={val}")

        ts_ns = int(sample.ts.timestamp() * 1_000_000_000)
        return f"{sample.TABLE} {','.join(field_parts)} {ts_ns}"

    def flush(self):
        """Send all buffered rows to InfluxDB3 via HTTP ILP endpoint."""
        try:
            for table in list(self.buffers.keys()):
                self._flush_table(table)
        except Exception as e:
            logger.error(f"Unexpected flush error: {e}")

    def _flush_table(self, table: str):
        """Atomically swap the named table's buffer and POST its rows to InfluxDB3, restoring rows on failure."""
        rows = self.buffers.get(table)

        if not rows:
            return

        self.buffers[table] = []

        try:
            body = "\n".join(rows).encode("utf-8")
            path = f"/api/v3/write_lp?db={urllib.parse.quote(self.database)}&precision=ns"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "text/plain; charset=utf-8",
            }

            conn = http.client.HTTPConnection(self._http_host, self._http_port, timeout=5)
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode(errors="ignore")
            conn.close()

            if resp.status not in (200, 204):
                raise RuntimeError(f"({resp.status}) {data}")

            logger.debug(f"Flushed {len(rows)} rows -> {table}")

        except Exception as e:
            logger.error(f"Flush error for {table}: {e}")
            self.buffers[table] = rows + self.buffers[table]

    def close(self):
        if self.closed:
            return

        self._stop = True
        self.closed = True

        try:
            self._thread.join(timeout=2)
        except Exception:
            pass

        try:
            self.flush()
        except Exception:
            pass

    # --- InfluxDB3 startup ---

    @staticmethod
    def is_influxdb3_running(host="127.0.0.1", port=8181):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex((host, port)) == 0

    @staticmethod
    def start_influxdb3():
        if InfluxDB3Database.is_influxdb3_running():
            logger.info("InfluxDB3 already running")
            InfluxDB3Database.ensure_database()
            return

        if not INFLUXDB3_BINARY.exists():
            raise RuntimeError(f"InfluxDB3 binary not found at {INFLUXDB3_BINARY}")

        logger.info("Starting InfluxDB3...")

        data_dir = INFLUXDB3_DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)

        log_dir = Path.home() / ".influxdb/logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "server.log", "a")

        import os
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

        subprocess.Popen(
            [
                str(INFLUXDB3_BINARY),
                "serve",
                "--node-id-from-env=INFLUXDB3_NODE_ID",
                "--object-store=file",
                f"--data-dir={data_dir}",
            ],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )

        timeout_s = 10.0
        start = time.time()
        while time.time() - start < timeout_s:
            if InfluxDB3Database.is_influxdb3_running():
                logger.info("InfluxDB3 started successfully")
                InfluxDB3Database.ensure_database()
                return
            time.sleep(0.5)

        raise RuntimeError("InfluxDB3 did not become ready in time")

    @staticmethod
    def ensure_database(
        database: str = INFLUXDB3_DATABASE,
        token: str = INFLUXDB3_TOKEN,
        retention: str = INFLUXDB3_RETENTION,
        host: str = "127.0.0.1",
        port: int = 8181,
    ):
        """Create the InfluxDB3 database with retention policy if it does not already exist."""
        body = json.dumps({"db": database, "retention_period": retention})
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("POST", "/api/v3/configure/database", body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode(errors="ignore")
        finally:
            conn.close()

        if resp.status in (200, 201):
            logger.info(f"Created database '{database}' with retention {retention}")
        elif resp.status == 409:
            logger.debug(f"Database '{database}' already exists")
        else:
            logger.warning(f"Unexpected status creating database '{database}': {resp.status} — {data}")
