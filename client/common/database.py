import socket
import subprocess
import threading
import time
import http.client
import urllib.parse

from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Union

from questdb.ingress import IngressError, Sender

from client.common.constants import AppStatus
from client.common.utils import setup_logging

QUESTDB_SCRIPT = Path("/home/jose/apps/questdb-9.3.5-rt-linux-x86-64/bin/questdb.sh")

logger = setup_logging("database")


# =========================================================
# DATA MODELS
# =========================================================


@dataclass
class TelemetrySample:
    TABLE = "telemetry_cf"
    ts: datetime
    fps: float
    status: AppStatus


@dataclass
class ActionSample:
    TABLE = "action_cf"
    ts: datetime
    active: bool
    vx: float
    vy: float
    yawrate: float
    zdistance: float


@dataclass
class PerceptionSample:
    TABLE = "perception_cf"
    ts: datetime
    hand_detected: bool
    hand_x: int
    hand_y: int
    gesture_name: str
    gesture_confidence: float


Sample = Union[TelemetrySample, ActionSample, PerceptionSample]


# =========================================================
# DATABASE
# =========================================================


class Database:
    def __init__(
        self,
        conf: str = "tcp::addr=127.0.0.1:9009;",
        table_name: str = "telemetry_cf",
        precision: int = 2,
        flush_interval_s: float = 1.0,
    ):
        self.conf = conf
        self.table_name = table_name
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

    # =====================================================
    # CONNECTION
    # =====================================================

    def _connect(self) -> bool:
        if self.closed:
            return False

        try:
            self.sender = Sender.from_conf(self.conf)
            self.sender.establish()
            logger.info("Connected to QuestDB")
            return True

        except Exception as e:
            self.sender = None
            logger.warning(f"QuestDB connection failed: {e}")
            return False

    # =====================================================
    # WORKER
    # =====================================================

    def _worker(self):
        while not self._stop:
            time.sleep(self.flush_interval_s)
            try:
                self.flush()
            except Exception as e:
                logger.error(f"Flush worker error: {e}")

    # =====================================================
    # LOG
    # =====================================================

    def log(self, sample: Sample):
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
                else:
                    if isinstance(val, float):
                        val = round(val, self.precision)
                    columns[f.name] = val

            kwargs = {"at": sample.ts}
            if symbols:
                kwargs["symbols"] = symbols
            if columns:
                kwargs["columns"] = columns

            table = getattr(sample, "TABLE", self.table_name)

            if table not in self.buffers:
                self.buffers[table] = []

            self.buffers[table].append(kwargs)

        except Exception as e:
            logger.error(f"Unexpected error during logging: {e}")

    # =====================================================
    # FLUSH
    # =====================================================

    def flush(self):
        if self.closed:
            return

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

            # restore lost data (optional choice)
            self.buffers[table].extend(rows)

    # =====================================================
    # SHUTDOWN
    # =====================================================

    def close(self):
        if self.closed:
            return

        self._stop = True

        try:
            self._thread.join(timeout=2)
        except Exception:
            pass

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
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # =====================================================
    # QUESTDB STARTUP
    # =====================================================

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
        tables = [
            "telemetry_cf",
            "action_cf",
            "perception_cf",
        ]

        for table in tables:
            try:
                # ---- existence probe (cheap, safe) ----
                probe_sql = f"SELECT count() FROM {table} LIMIT 1"
                Database._exec_sql(probe_sql)

            except Exception as e:
                # table likely does not exist → skip silently or debug log
                logger.debug(f"[{table}] does not exist, skipping cleanup")
                continue

            try:
                # ---- actual cleanup ----
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
        conn = http.client.HTTPConnection(host, port, timeout=3)

        path = "/exec?query=" + urllib.parse.quote(sql)

        conn.request("GET", path)
        resp = conn.getresponse()

        data = resp.read().decode(errors="ignore")
        conn.close()

        if resp.status != 200:
            raise RuntimeError(f"QuestDB HTTP error {resp.status}: {data}")

        return data
