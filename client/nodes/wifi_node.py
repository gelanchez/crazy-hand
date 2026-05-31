"""ROS2-style node that connects to a Crazyflie over Wi-Fi, streams camera frames via CPX, and publishes telemetry and flight-command interfaces over iceoryx2."""
import ctypes
import logging
import math
import platform
import queue
import random
import struct
import subprocess
import threading
import time

import cflib.crtp
import click
import cv2
import iceoryx2
import numpy as np
from cflib.cpx import CPXFunction
from cflib.crazyflie import Crazyflie
from cflib.crazyflie.log import LogConfig

from client.common.constants import (
    CRAZYFLIE_IP,
    CRAZYFLIE_PORT,
    CRAZYFLIE_URI,
    DEFAULT_HEIGHT,
    IMAGE_HEIGHT,
    IMAGE_SIZE,
    IMAGE_WIDTH,
    AppStatus,
    EventId,
    FlightCommand,
    FlightState,
    ImageFormat,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import ActionData, ImageData, TelemetryData
from client.common.utils import FPSCounter

NODE_NAME = "wifi_node"

_MOTOR_PWM_MAX = 65535  # Crazyflie uint16 motor PWM range

# cflib log key → TelemetryData field name (float, default 0.0)
_TELE_FLOAT_FIELDS = (
    ("stateEstimate.x",   "x"),
    ("stateEstimate.y",   "y"),
    ("stateEstimate.z",   "z"),
    ("stateEstimate.vx",  "vx"),
    ("stateEstimate.vy",  "vy"),
    ("stateEstimate.vz",  "vz"),
    ("stateEstimate.roll",  "roll"),
    ("stateEstimate.pitch", "pitch"),
    ("stateEstimate.yaw",   "yaw"),
    ("pm.vbat", "vbat"),
)

# How often to emit frame-level DEBUG messages to console (every N frames)
_FRAME_LOG_INTERVAL = 10

# Action loop timing
_LOOP_INTERVAL = 0.05  # seconds (~20 Hz) — CF watchdog needs setpoints at least every 500 ms
_UNLOCK_PACKETS = 10  # unlock packets at loop rate before first hover setpoint (~500 ms)


def _fill_sim_frame(image: np.ndarray, frame_id: int, _y: np.ndarray, _x: np.ndarray) -> None:
    image[:] = ((_x + _y + frame_id) % 256).astype(np.uint8)


class WifiNode(Node):
    """Manages the Crazyflie Wi-Fi connection, image streaming, telemetry, and flight commands."""

    # Seconds without a frame before watchdog reconnects.
    # GAP8 may need up to ~10 s to reinit camera and start streaming after
    # a power-on or after a CPX reconnect; 30 s avoids thrashing.
    _NO_IMAGE_TIMEOUT = 30.0

    def __init__(self, sim=False):
        # DEBUG goes to file; console stays at INFO to avoid per-frame spam
        super().__init__(NODE_NAME, level=logging.DEBUG, console_level=logging.INFO)
        self._sim = sim
        self._cf_connected = threading.Event()
        self._frame_id = 0
        self._fps_counter = FPSCounter()
        self._console_buffer = ""
        self._frames_since_log = 0
        self._last_frame_time: float | None = None  # set in _commit_image_sample
        self._connect_time: float = 0.0  # set after successful connect
        self._reconnecting = False  # True while watchdog is reconnecting
        self._drone_state: dict = {}  # latest log values from cflib
        self._drone_state_lock = threading.Lock()
        self._sim_start_time: float = 0.0  # set when sim starts
        self._sim_flying: bool = False  # synced from _action_loop in sim mode
        self._sim_hover_z: float = DEFAULT_HEIGHT
        self._sim_flight_start: float = 0.0  # set at TAKEOFF in sim mode

    # --- cflib patching ---

    @staticmethod
    def _patch_cflib() -> None:
        """Redirect cflib print() calls and noisy logger output through our logger.

        cflib uses bare print() throughout its transport and driver code — these
        bypass Python logging entirely and appear as noise on stdout/stderr during
        normal connect/disconnect cycles.
        """
        try:
            import logging as _logging
            import socket as _socket

            import cflib.cpx
            import cflib.cpx.transports
            import cflib.crtp.tcpdriver

            _cflib_logger = _logging.getLogger(NODE_NAME)

            # Silence cflib's own Python logger (e.g. "Couldn't load link driver") so it
            # doesn't leak to stderr via the root logger's last-resort handler.
            _cflib_root = _logging.getLogger("cflib")
            if not any(isinstance(h, _logging.NullHandler) for h in _cflib_root.handlers):
                _cflib_root.addHandler(_logging.NullHandler())
            _cflib_root.propagate = False

            # CPXRouter.run — remove print(traceback) on transport errors during disconnect
            def _patched_cpx_router_run(self):
                while self._connected:
                    try:
                        packet = self._transport.readPacket()
                        if packet.function.value not in self._rxQueues:
                            _cflib_logger.debug("CPXRouter: auto-creating queue for %s", packet.function)
                            self._rxQueues[packet.function.value] = queue.Queue()
                        self._rxQueues[packet.function.value].put(packet)
                    except Exception:
                        if self._connected:
                            _cflib_logger.error("CPXRouter transport error", exc_info=True)

            cflib.cpx.CPXRouter.run = _patched_cpx_router_run

            # CPXRouter.receivePacket — remove "Creating queue for ..." print
            def _patched_cpx_router_receive_packet(self, function, timeout=None):
                if function.value not in self._rxQueues:
                    _cflib_logger.debug("CPXRouter: creating queue for %s", function)
                    self._rxQueues[function.value] = queue.Queue()
                return self._rxQueues[function.value].get(block=True, timeout=timeout)

            cflib.cpx.CPXRouter.receivePacket = _patched_cpx_router_receive_packet

            # SocketTransport — remove connect/disconnect/init prints
            def _patched_socket_transport_init(self, host, port):
                _cflib_logger.debug("CPX socket transport: %s:%s", host, port)
                self._host = host
                self._port = port
                self.connect()

            def _patched_socket_transport_connect(self):
                _cflib_logger.info("Connecting CPX socket on %s:%s...", self._host, self._port)
                self._socket = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                self._socket.connect((self._host, self._port))
                _cflib_logger.debug("CPX socket connected")

            def _patched_socket_transport_disconnect(self):
                _cflib_logger.debug("Closing CPX socket transport")
                self._socket.shutdown(_socket.SHUT_WR)
                self._socket.close()
                self._socket = None

            cflib.cpx.transports.SocketTransport.__init__ = _patched_socket_transport_init
            cflib.cpx.transports.SocketTransport.connect = _patched_socket_transport_connect
            cflib.cpx.transports.SocketTransport.disconnect = _patched_socket_transport_disconnect

            # TcpDriver.close — remove "Driver closed" print
            def _patched_tcp_driver_close(self):
                try:
                    self._thread.stop()  # join non-daemon receive thread before closing socket
                    self.cpx.close()
                    self.cpx = None
                except Exception as e:
                    _cflib_logger.warning("TcpDriver close error: %s", e)
                _cflib_logger.debug("TcpDriver closed")
                self.cpx = None

            cflib.crtp.tcpdriver.TcpDriver.close = _patched_tcp_driver_close

            # ping_thread raises BrokenPipeError when the socket closes before
            # the thread exits — expected during shutdown, not an error.
            import threading as _threading
            _orig_excepthook = _threading.excepthook

            def _thread_excepthook(args):
                if args.exc_type is BrokenPipeError:
                    return
                _orig_excepthook(args)

            _threading.excepthook = _thread_excepthook

        except Exception:
            pass

    # --- Image pipeline ---

    def _loan_image_sample(self):
        """Loan an uninitialised image sample and pre-fill id/timestamp."""
        sample = self.image_port.publisher.loan_uninit()
        payload = sample.payload().contents
        payload.id = self._frame_id
        payload.timestamp = int(time.time() * 1000)
        return sample, payload

    def _commit_image_sample(self, sample) -> None:
        """Send a prepared sample and update frame counters and notifier."""
        sample.assume_init().send()
        self._frame_id += 1
        self._fps_counter.update()
        self._last_frame_time = time.time()
        self._frames_since_log += 1
        if self._frames_since_log >= _FRAME_LOG_INTERVAL:
            self.logger.debug(f"FPS: {self._fps_counter.fps:.1f}")
            self._frames_since_log = 0
        try:
            self.image_port.notifier.notify_with_custom_event_id(self.image_port.event)
        except Exception:
            # Listener may have disconnected (e.g. GUI shutdown) — not an error
            pass

    def _receive_images(self) -> None:
        """Receive CPX APP packets from GAP8, reassemble JPEG/RAW frames, and publish them."""
        self.logger.info("Image reception thread started")
        _no_packet_count = 0
        while self.running:
            if self._reconnecting or not self.cf.is_connected():
                time.sleep(0.2)
                continue
            try:
                packet = self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.5)
                _no_packet_count = 0  # reset on success
            except queue.Empty:
                _no_packet_count += 1
                if _no_packet_count % 4 == 0:  # every ~2s
                    self.logger.debug(
                        "receive_images: no APP packet for ~%.0fs (reconnecting=%s, connected=%s)",
                        _no_packet_count * 0.5,
                        self._reconnecting,
                        self.cf.is_connected() if hasattr(self, "cf") else "N/A",
                    )
                continue
            except Exception as e:
                if self.running and self.cf.is_connected():
                    self.logger.error(f"Error receiving CPX packet: {e}")
                time.sleep(0.2)  # avoid tight loop while link is down
                continue

            try:
                data = packet.data
                if len(data) != 11 or data[0] != 0xBC:
                    continue

                magic, width, height, depth, fmt, size = struct.unpack("<BHHBBI", data[:11])
                self.logger.debug(f"Header received: {width}x{height}, size={size}, fmt={fmt}")

                img_stream = bytearray()
                assembly_ok = True

                while len(img_stream) < size and self.running and self.cf.is_connected():
                    try:
                        packet = self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.5)
                        img_stream.extend(packet.data)
                    except queue.Empty:
                        continue
                    except Exception as e:
                        if self.running and self.cf.is_connected():
                            self.logger.warning(f"Error during image assembly, dropping frame: {e}")
                        assembly_ok = False
                        break

                if not self.running:
                    break

                if assembly_ok:
                    self._publish_frame(bytes(img_stream[:size]), fmt)
            except Exception as e:
                if self.running:
                    self.logger.error(f"Error assembling image: {e}")

    def _publish_frame(self, frame_data: bytes, fmt: int) -> None:
        """Decode a raw or JPEG frame and publish it to the image iceoryx2 port."""
        if fmt == 1:  # JPEG
            try:
                arr = np.frombuffer(frame_data, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
                if img is None:
                    raise ValueError("cv2.imdecode returned None")
                frame_data = img.tobytes()
            except Exception as e:
                self.logger.error(f"JPEG decode error: {e}")
                return
        try:
            sample, payload = self._loan_image_sample()
            payload.format = int(ImageFormat.JPEG if fmt == 1 else ImageFormat.RAW)
            ctypes.memmove(payload.pixels, frame_data, min(len(frame_data), IMAGE_SIZE))
            self._commit_image_sample(sample)
        except Exception as e:
            self.logger.error(f"Error publishing frame: {e}")

    def _image_watchdog(self) -> None:
        """Reconnect if no images arrive within _NO_IMAGE_TIMEOUT seconds.

        _startup_link_reset() closes cflib, sends a bare-TCP ping to trigger
        GAP8's first WIFI_CTRL_STATUS_CLIENT_CONNECTED notification, then
        reconnects cflib as the reliable second notification that restarts
        the camera_task streaming loop.
        """
        self.logger.info("Image watchdog started")
        while self.running:
            time.sleep(1.0)
            if not self.running or self._reconnecting:
                continue

            ref = self._last_frame_time if self._last_frame_time is not None else self._connect_time
            elapsed = time.time() - ref

            if elapsed <= self._NO_IMAGE_TIMEOUT:
                continue

            self.logger.warning(f"No images for {elapsed:.0f}s — reconnecting to restart GAP8 streaming...")
            self._reconnecting = True
            try:
                # Close dead link so _startup_link_reset starts clean
                try:
                    self.cf.close_link()
                except Exception:
                    pass

                # Wait for drone WiFi to be reachable (important after power cycles).
                # 60 s covers the full ESP32 boot + AP-up sequence.
                deadline = time.time() + 60.0
                while self.running and time.time() < deadline:
                    if WifiNode._check_connection():
                        break
                    self.logger.info("Watchdog: waiting for drone WiFi...")
                    time.sleep(3.0)

                if not self.running:
                    return

                if not WifiNode._check_connection():
                    self.logger.warning("Drone unreachable after 60s — will retry")
                else:
                    # _startup_link_reset: close → TCP ping → cflib reopen, triggers GAP8
                    if self._startup_link_reset():
                        self._prewarm_cpx_queue()
                        self.logger.info("Watchdog reconnect successful — waiting for images...")
                    else:
                        self.logger.warning("Watchdog reconnect failed, will retry")
            except Exception as e:
                self.logger.error(f"Watchdog reconnect error: {e}")
            finally:
                # Reset timer so we don't reconnect in a tight loop
                self._last_frame_time = time.time()
                self._reconnecting = False

    # --- Telemetry ---

    def _telemetry_loop(self) -> None:
        """Publish a TelemetryData sample at ~10 Hz, sourced from cflib logs or sim state."""
        # Note: telemetry_port is created in run() before this thread starts
        while self.running:
            try:
                sample = self.telemetry_port.publisher.loan_uninit()
                payload = sample.payload().contents
                payload.fps = self._fps_counter.fps
                if self._sim:
                    payload.status = AppStatus.SIMULATING
                elif self._reconnecting:
                    payload.status = AppStatus.RECONNECTING
                elif hasattr(self, "cf") and self.cf is not None and self.cf.is_connected():
                    payload.status = AppStatus.CONNECTED
                else:
                    payload.status = AppStatus.DISCONNECTED

                # Drone state — synthetic in sim, cflib log subsystem when connected
                if self._sim:
                    self._update_sim_drone_state()
                with self._drone_state_lock:
                    state = dict(self._drone_state)

                for cf_key, field in _TELE_FLOAT_FIELDS:
                    setattr(payload, field, state.get(cf_key, 0.0))
                for m in ("m1", "m2", "m3", "m4"):
                    setattr(payload, m, round(state.get(f"motor.{m}", 0) / _MOTOR_PWM_MAX * 100))

                sample.assume_init().send()
                try:
                    self.telemetry_port.notifier.notify_with_custom_event_id(self.telemetry_port.event)
                except Exception:
                    # Listener may have disconnected — not an error
                    pass
                self.logger.debug(f"Sent {payload}")
            except Exception as e:
                if self.running:
                    self.logger.warning(f"Telemetry publish error: {e}")
            time.sleep(0.1)

    # --- Flight control ---

    def _action_loop(self) -> None:
        """Consume FlightCommand events and drive the CF commander at ~20 Hz.

        Sends unlock/hover setpoints when flying, motor-test setpoints when testing,
        and keep-alive zero-setpoints while grounded to prevent EKF drift.
        """
        self.logger.info("Action loop started")
        flying = False
        unlocking = 0  # countdown: sends thrust=0 packets before first hover setpoint (~500 ms)
        hover = [0.0, 0.0, 0.0, DEFAULT_HEIGHT]  # vx, vy, yawrate, zdist
        flight_state = FlightState.IDLE
        motor_test_thrust = 0

        while self.running:
            try:
                event_id = self.action_port.listener.try_wait_one()
            except Exception as e:
                if self.running:
                    self.logger.error(f"Action listener error: {e}")
                time.sleep(0.1)
                continue

            if event_id is not None and event_id == self.action_port.event:
                try:
                    sample = self.action_port.subscriber.receive()
                except Exception as e:
                    self.logger.warning(f"Action receive error: {e}")
                    sample = None

                if sample is not None:
                    action = sample.payload().contents
                    command = FlightCommand(action.command)
                    flight_state = FlightState(action.state)
                    hover[0] = action.vx
                    hover[1] = action.vy
                    hover[2] = action.yawrate
                    hover[3] = action.zdistance
                    motor_test_thrust = action.thrust
                    del action, sample

                    match command:
                        case FlightCommand.TAKEOFF:
                            if not flying:
                                self.logger.info(f"Taking off to z={hover[3]:.2f}m")
                                flying = True
                                unlocking = _UNLOCK_PACKETS
                                if self._sim:
                                    self._sim_flight_start = time.time()

                        case FlightCommand.LAND:
                            if flying:
                                if not self._sim:
                                    try:
                                        self.cf.commander.send_stop_setpoint()
                                    except Exception as e:
                                        self.logger.warning(f"Land stop error: {e}")
                                flying = False
                                unlocking = 0
                                self.logger.info("Motors stopped — landed")

                        case FlightCommand.EMERGENCY_STOP:
                            self.logger.warning("EMERGENCY STOP — cutting motors")
                            if not self._sim:
                                try:
                                    self.cf.commander.send_stop_setpoint()
                                except Exception as e:
                                    self.logger.error(f"Emergency stop error: {e}")
                            flying = False
                            unlocking = 0
                            flight_state = FlightState.IDLE
                            hover[0] = hover[1] = hover[2] = 0.0

                        case FlightCommand.MOTOR_TEST:
                            self.logger.info("Motor test — spinning motors")

            # In sim: sync state for _update_sim_drone_state, skip CF commander calls
            if self._sim:
                self._sim_flying = flying
                self._sim_hover_z = hover[3]
                time.sleep(_LOOP_INTERVAL)
                continue

            if self._reconnecting:
                # Link is dead — skip all sends to avoid Broken pipe spam
                time.sleep(_LOOP_INTERVAL)
                continue

            if flying:
                if unlocking > 0:
                    try:
                        self.cf.commander.send_setpoint(0, 0, 0, 0)
                        unlocking -= 1
                    except Exception as e:
                        self.logger.warning(f"Unlock error: {e}")
                else:
                    try:
                        self.cf.commander.send_hover_setpoint(*hover)
                    except Exception as e:
                        self.logger.warning(f"Hover send error: {e}")
            elif flight_state == FlightState.MOTOR_TESTING:
                # Motor test: spin at low thrust — visible but won't lift
                try:
                    self.cf.commander.send_setpoint(0, 0, 0, motor_test_thrust)
                except Exception as e:
                    self.logger.warning(f"Motor test send error: {e}")
            else:
                # Keep commander alive while grounded — prevents EKF drift between flights
                try:
                    self.cf.commander.send_setpoint(0, 0, 0, 0)
                except Exception as e:
                    self.logger.warning(f"Keep-alive error: {e}")

            time.sleep(_LOOP_INTERVAL)

        # Ensure motors stop on exit
        if flying and not self._sim:
            try:
                self.cf.commander.send_stop_setpoint()
            except Exception:
                pass
        self.logger.info("Action loop exited")

    # --- Crazyflie callbacks ---

    def _on_console(self, text):
        """Buffer Crazyflie console characters and forward complete lines to the Python logger."""
        self._console_buffer += text
        if "\n" in self._console_buffer:
            lines = self._console_buffer.split("\n")
            for line in lines[:-1]:
                line = line.strip()
                if not line:
                    continue

                # Check for standardized log prefixes from firmware
                if line.startswith("[ERROR]"):
                    self.logger.error(f"CF Console: {line[7:].strip()}")
                elif line.startswith("[WARNING]"):
                    self.logger.warning(f"CF Console: {line[9:].strip()}")
                elif line.startswith("[DEBUG]"):
                    self.logger.debug(f"CF Console: {line[7:].strip()}")
                elif line.startswith("[INFO]"):
                    self.logger.info(f"CF Console: {line[6:].strip()}")
                else:
                    self.logger.info(f"CF Console: {line}")
            self._console_buffer = lines[-1]

    def _on_connected(self, uri):
        self.logger.info(f"Crazyflie connected: {uri}")
        self._cf_connected.set()

    def _setup_log_subsystem(self, suffix: str = "") -> None:
        """Subscribe to Crazyflie log variables at 10 Hz (state estimate, motors, battery).

        suffix: appended to config names to avoid duplicate-name errors on retry.
        """

        def _log_cb(timestamp, data, logconf):
            with self._drone_state_lock:
                self._drone_state.update(data)

        configs = [
            (
                f"state_pos_vel{suffix}",
                100,
                [
                    ("stateEstimate.x", "float"),
                    ("stateEstimate.y", "float"),
                    ("stateEstimate.z", "float"),
                    ("stateEstimate.vx", "float"),
                    ("stateEstimate.vy", "float"),
                    ("stateEstimate.vz", "float"),
                ],
            ),
            (
                f"state_att_batt{suffix}",
                100,
                [
                    ("stateEstimate.roll", "float"),
                    ("stateEstimate.pitch", "float"),
                    ("stateEstimate.yaw", "float"),
                    ("pm.vbat", "float"),
                ],
            ),
            (
                f"motors{suffix}",
                100,
                [
                    ("motor.m1", "uint16_t"),
                    ("motor.m2", "uint16_t"),
                    ("motor.m3", "uint16_t"),
                    ("motor.m4", "uint16_t"),
                ],
            ),
        ]

        for name, period_ms, variables in configs:
            try:
                lc = LogConfig(name=name, period_in_ms=period_ms)
                for var_name, var_type in variables:
                    lc.add_variable(var_name, var_type)
                self.cf.log.add_config(lc)
                lc.data_received_cb.add_callback(_log_cb)
                lc.error_cb.add_callback(lambda conf, msg: self.logger.warning(f"Log error [{conf.name}]: {msg}"))
                lc.start()
                self.logger.info(f"Log config '{name}' started")
            except Exception as e:
                self.logger.warning(f"Log config '{name}' setup failed: {e}")

    def _on_connection_failed(self, uri: str, msg: str) -> None:
        """Handle a failed cflib connection attempt, logging only the summary line from the error."""
        # msg from cflib includes a full embedded traceback — log only the summary line
        summary = msg.splitlines()[0] if msg else "unknown error"
        self.logger.warning(f"Crazyflie connection failed ({uri}): {summary}")
        self._cf_connected.set()

    def _on_disconnected(self, uri: str) -> None:
        """Handle a cflib disconnect event and accelerate the image-watchdog reconnect timer."""
        self.logger.info(f"Crazyflie disconnected: {uri}")
        # Fast-path image watchdog — reconnect in ~5 s rather than _NO_IMAGE_TIMEOUT
        if not self._reconnecting:
            self._last_frame_time = time.time() - (self._NO_IMAGE_TIMEOUT - 5.0)

    # --- Connection ---

    def _startup_link_reset(self) -> bool:
        """Close link, send a TCP ping as GAP8's first WIFI_CTRL(connected),
        then reconnect cflib as the reliable second notification.

        When cflib itself does close+reopen the link, the ESP32 firmware
        immediately accepts then drops the new CPX connection (brief disconnect)
        because the old CPX session's cleanup is still in-flight.  GAP8 ends up
        receiving connected+disconnected → stops streaming.

        Using a bare-TCP ping (no CPX/CRTP) as the first touch avoids the
        CPX-level state machine conflict.  By the time cflib reconnects the ESP32
        is idle and accepts the connection cleanly — GAP8 gets only "connected".

        Returns True if cflib reconnected, False on timeout.
        """
        self.logger.info("Startup link reset — re-triggering GAP8 streaming...")
        try:
            self.cf.close_link()
            self._cf_connected.clear()
            time.sleep(1.0)  # let cflib CPX session close fully on ESP32
            WifiNode._tcp_ping()  # first WIFI_CTRL(connected) to GAP8
            time.sleep(1.5)  # let ping socket clear before cflib reconnects
            self.cf.open_link(CRAZYFLIE_URI)
            if self._cf_connected.wait(timeout=10.0) and self.cf.is_connected():
                self.logger.info("Startup link reset complete")
                return True
            self.logger.warning("Startup link reset reconnect timed out — proceeding without reset")
            return False
        except Exception as e:
            self.logger.error(f"Startup link reset error: {e}")
            return False

    def _prewarm_cpx_queue(self) -> None:
        """Pre-register the CPX APP queue and drain any stale packets.

        The patched CPXRouter.run() creates queues on first packet (no silent
        drops), so APP frames from GAP8 that arrived during the cflib handshake
        are already queued by the time this is called.  Draining them all here
        avoids _receive_images() trying to assemble a partial/stale frame that
        began before the connection was fully confirmed.
        """
        drained = 0
        while True:
            try:
                self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.01)
                drained += 1
            except queue.Empty:
                break
        if drained:
            self.logger.info(f"CPX APP queue pre-registered (drained {drained} stale packet(s))")
        else:
            self.logger.info("CPX APP queue pre-registered (empty)")

    @staticmethod
    def _tcp_ping(hold_secs: float = 0.3) -> None:
        """Open a bare TCP connection to the drone, hold briefly, then close.

        This sends WIFI_CTRL_STATUS_CLIENT_CONNECTED to GAP8 via the ESP32
        firmware without any CPX/CRTP overhead.  GAP8's camera_task does not
        stream reliably on its FIRST such notification after boot; using this
        ping as the "first connect" makes cflib's subsequent open_link() the
        reliable second notification.

        Fails silently — caller proceeds regardless.
        """
        try:
            import socket as _socket

            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((CRAZYFLIE_IP, CRAZYFLIE_PORT))
            time.sleep(hold_secs)
            s.close()
        except Exception:
            pass

    @staticmethod
    def _check_connection(host=CRAZYFLIE_IP):
        """Return True if the drone host responds to a single ICMP ping within 1 second."""
        param = "-n" if platform.system().lower() == "windows" else "-c"
        command = ["ping", param, "1", "-W", "1", host]
        return subprocess.call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

    def _check_required_decks(self) -> bool:
        """Verify AI-deck and Flow2 are attached by polling params.

        Returns True if both detected within timeout, False otherwise.
        """
        decks_status = {"bcFlow2": False, "bcAI": False}
        decks_event = threading.Event()

        def _deck_cb_flow(name, value_str):
            if int(value_str):
                decks_status["bcFlow2"] = True
            if decks_status["bcFlow2"] and decks_status["bcAI"]:
                decks_event.set()

        def _deck_cb_ai(name, value_str):
            if int(value_str):
                decks_status["bcAI"] = True
            if decks_status["bcFlow2"] and decks_status["bcAI"]:
                decks_event.set()

        self.cf.param.add_update_callback(group="deck", name="bcFlow2", cb=_deck_cb_flow)
        self.cf.param.add_update_callback(group="deck", name="bcAI", cb=_deck_cb_ai)
        self.cf.param.request_param_update("deck.bcFlow2")
        self.cf.param.request_param_update("deck.bcAI")

        if not decks_event.wait(timeout=10.0):
            if not self.cf.is_connected():
                self.logger.error("Deck check timed out — connection dropped during handshake")
            else:
                self.logger.error("Required decks (AI-deck, Flow2) not detected!")
            return False

        self.logger.info("AI-deck and Flow2 decks detected.")
        return True

    def _connect_cf(self) -> bool:
        """Initialize cflib and connect to Crazyflie, retrying until connected or stopped.

        Returns True if connected, False if self.running became False.
        """
        WifiNode._patch_cflib()
        cflib.crtp.init_drivers()
        self.cf = Crazyflie(rw_cache="./data/cache")

        # Disable parameter flood to save bandwidth for images
        self.cf.param.request_update_of_all_params = lambda: self.logger.info("Parameter flood disabled")

        self.cf.connected.add_callback(self._on_connected)
        self.cf.connection_failed.add_callback(self._on_connection_failed)
        self.cf.disconnected.add_callback(self._on_disconnected)
        self.cf.console.receivedChar.add_callback(self._on_console)

        self.logger.info(f"Opening link to {CRAZYFLIE_URI}...")
        self.cf.open_link(CRAZYFLIE_URI)

        while self.running and not self.cf.is_connected():
            self._cf_connected.clear()
            if self._cf_connected.wait(timeout=10.0):
                if self.cf.is_connected():
                    break
            if not self.cf.is_connected():
                self.logger.warning("Connection timed out, retrying...")
                try:
                    self.cf.close_link()
                except Exception:
                    pass
                time.sleep(1.0)
                self.logger.info(f"Opening link to {CRAZYFLIE_URI}...")
                self.cf.open_link(CRAZYFLIE_URI)

        return self.running

    # --- Simulation ---

    def _update_sim_drone_state(self) -> None:
        """Populate _drone_state with synthetic flight data for sim mode.

        Simulates takeoff/landing based on _sim_flying, gentle oscillating
        position/attitude, slow yaw rotation, slightly varying motor PWM,
        and a draining battery.
        """
        t = time.time() - self._sim_start_time

        # z rises to hover height over 5 s from takeoff; before first takeoff
        # command use elapsed sim time so telemetry is live from the start.
        if self._sim_flying:
            t_fly = time.time() - self._sim_flight_start
        else:
            t_fly = t  # simulate always-airborne until first real command
        z_frac = min(t_fly / 5.0, 1.0)
        z = self._sim_hover_z * z_frac
        vz = self._sim_hover_z * 0.2 * (1.0 - z_frac)
        x = 0.10 * math.sin(t * 0.5)
        y = 0.05 * math.sin(t * 0.3 + 0.5)
        vx = 0.05 * math.cos(t * 0.5)
        vy = 0.015 * math.cos(t * 0.3 + 0.5)
        roll = 2.0 * math.sin(t * 0.5)
        pitch = 1.5 * math.sin(t * 0.3 + 1.0)
        yaw = ((t * 5.0) % 360.0) - 180.0
        base_pwm = int(20000 + 12000 * z_frac)
        m1 = base_pwm + random.randint(-300, 300)
        m2 = base_pwm + random.randint(-300, 300)
        m3 = base_pwm + random.randint(-300, 300)
        m4 = base_pwm + random.randint(-300, 300)

        # Battery — starts at 4.10 V, drains at ~1 mV/s
        vbat = max(3.50, 4.10 - t * 0.001)

        with self._drone_state_lock:
            self._drone_state.update({
                "stateEstimate.x": x,
                "stateEstimate.y": y,
                "stateEstimate.z": z,
                "stateEstimate.vx": vx,
                "stateEstimate.vy": vy,
                "stateEstimate.vz": vz,
                "stateEstimate.roll": roll,
                "stateEstimate.pitch": pitch,
                "stateEstimate.yaw": yaw,
                "motor.m1": m1,
                "motor.m2": m2,
                "motor.m3": m3,
                "motor.m4": m4,
                "pm.vbat": vbat,
            })

    def _run_sim(self) -> None:
        """Run the simulation loop: publish synthetic gradient frames and drive telemetry/action threads."""
        self._sim_start_time = time.time()
        _y = np.arange(IMAGE_HEIGHT, dtype=np.uint16).reshape(-1, 1)
        _x = np.arange(IMAGE_WIDTH, dtype=np.uint16).reshape(1, -1)
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        threading.Thread(target=self._action_loop, daemon=True).start()
        while self.running:
            self.node.wait(iceoryx2.Duration.from_millis(100))
            if not self.running:
                break
            try:
                sample, payload = self._loan_image_sample()
                payload.format = int(ImageFormat.RAW)
                image = np.ctypeslib.as_array(payload.pixels).reshape(IMAGE_HEIGHT, IMAGE_WIDTH)
                _fill_sim_frame(image, self._frame_id, _y, _x)
                self._commit_image_sample(sample)
            except Exception as e:
                self.logger.error(f"Sim frame error: {e}")

    # --- Hardware startup ---

    def _startup_hardware(self) -> bool:
        """Wait for network, connect CF, and start all hardware threads.

        Returns True on success, False if startup failed or node was stopped.
        """
        self.logger.info(f"Waiting for network connectivity to {CRAZYFLIE_IP}...")
        while self.running and not WifiNode._check_connection():
            self.node.wait(iceoryx2.Duration.from_secs(5))

        if not self.running:
            return False

        # TCP ping: send GAP8's first WIFI_CTRL(connected) via a bare TCP socket,
        # then connect cflib as the reliable second notification.  When cflib
        # itself does the close+reopen the ESP32 briefly accepts then drops the
        # new CPX connection (firmware bug), leaving GAP8 in "disconnected" state.
        # A raw TCP ping has no CPX overhead, so the ESP32 state machine stays
        # clean for cflib's subsequent connect.
        self.logger.info("TCP ping — priming GAP8 for second WIFI_CTRL(connected)...")
        WifiNode._tcp_ping()
        time.sleep(2.0)  # let ping socket clear on ESP32 before cflib opens

        if not self._connect_cf():
            return False

        # 1 s delay: with TOC and params cached, cflib connects fast (~876 ms)
        # and LOG_START arrives before the drone's CRTP stack finishes
        # its post-connect init.  The drone silently drops it → no LOG_DATA.
        # A short pause eliminates this race.
        time.sleep(1.0)
        self._setup_log_subsystem()

        # Verify LOG_DATA is actually flowing (pm.vbat is always >0 on a live drone).
        # If still zero after 2 s, drone silently dropped LOG_START — retry once.
        _log_deadline = time.time() + 2.0
        while time.time() < _log_deadline:
            with self._drone_state_lock:
                if self._drone_state.get("pm.vbat", 0.0) != 0.0:
                    break
            time.sleep(0.1)
        else:
            self.logger.warning("LOG_DATA not received after 2s — retrying log setup")
            self._setup_log_subsystem(suffix="_r1")

        # Pre-register CPX APP queue immediately.  GAP8 starts streaming
        # ~800 ms before cflib fires _on_connected; any APP packet that
        # arrives before the queue exists is silently dropped by cflib's
        # CPX router.  Registering here — right after the connection is
        # confirmed — ensures no early frames are lost.
        self._prewarm_cpx_queue()

        # Start image reception immediately so queued frames are consumed
        # while deck detection runs in parallel.
        threading.Thread(target=self._receive_images, daemon=True).start()

        if not self._check_required_decks():
            self.logger.error("Startup aborted — required decks not confirmed")
            self.running = False
            return False

        self._connect_time = time.time()

        threading.Thread(target=self._action_loop, daemon=True).start()
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        threading.Thread(target=self._image_watchdog, daemon=True).start()

        self.logger.info("Node fully operational, receiving frames and commands...")
        return True

    # --- Entry point ---

    def run(self):
        """Set up iceoryx2 ports and enter the main node loop (hardware or simulation)."""
        try:
            # INITIALIZE ICEORYX2
            self.image_port = self.create_publisher(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY)
            self.telemetry_port = self.create_publisher(ServiceName.TELEMETRY, TelemetryData, EventId.TELEMETRY_READY)
            self.action_port = self.create_subscriber(ServiceName.ACTION, ActionData, EventId.ACTION_READY)

            if self.action_port is None or self.action_port.subscriber is None:
                self.logger.error("Failed to create action subscriber")
                return

            if self._sim:
                self._run_sim()
            else:
                if not self._startup_hardware():
                    return
                while self.running:
                    time.sleep(0.5)

        except KeyboardInterrupt:
            pass
        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError) as e:
            self.logger.warning(f"iceoryx2 wait interrupted: {e}")
        except Exception as e:
            self.logger.error(f"WifiNode error: {e}")
        finally:
            self.stop()
            self.running = False
            if not self._sim and hasattr(self, "cf"):
                try:
                    self.cf.commander.send_stop_setpoint()
                    self.cf.close_link()
                except Exception:
                    pass
            self.logger.info(f"{NODE_NAME} shut down")


@click.command()
@click.option("--sim", is_flag=True, help="Run in simulation mode")
def main(sim):
    node = WifiNode(sim=sim)
    node.run()


if __name__ == "__main__":
    main()
