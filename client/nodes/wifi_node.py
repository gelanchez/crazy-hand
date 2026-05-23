import ctypes
import logging
import platform
import queue
import struct
import subprocess
import threading
import time

import cflib.crtp
import click
import cv2
import iceoryx2
import numpy as np

from cflib.crazyflie import Crazyflie
from cflib.cpx import CPXFunction

from client.common.constants import (
    CRAZYFLIE_IP,
    CRAZYFLIE_URI,
    AppStatus,
    DEFAULT_HEIGHT,
    EventId,
    FlightCommand,
    IMAGE_HEIGHT,
    IMAGE_SIZE,
    IMAGE_WIDTH,
    ServiceName,
)
from client.common.node import Node
from client.common.payloads import ActionData, ImageData, TelemetryData
from client.common.utils import FPSCounter

NODE_NAME = "wifi_node"

# How often to emit frame-level DEBUG messages to console (every N frames)
_FRAME_LOG_INTERVAL = 10

# Action loop timing
_LOOP_INTERVAL = 0.05   # seconds (~20 Hz) — CF watchdog needs setpoints at least every 500 ms
_UNLOCK_PACKETS = 10    # unlock packets at loop rate before first hover setpoint (~500 ms)

# Monkey-patch cflib to redirect all print() calls and logger output through our logger.
# cflib uses bare print() throughout its transport and driver code — these bypass Python
# logging entirely and appear as noise on stdout/stderr during normal connect/disconnect cycles.
try:
    import socket as _socket
    import cflib.cpx
    import cflib.cpx.transports
    import cflib.crtp.tcpdriver
    import logging as _logging

    _cflib_logger = _logging.getLogger(NODE_NAME)

    # Silence cflib's own Python logger (e.g. "Couldn't load link driver") so it doesn't
    # leak to stderr via the root logger's last-resort handler.  Our connection callbacks
    # already capture and log all meaningful events.
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
                    pass
                else:
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
            self.cpx.close()
            self.cpx = None
        except Exception as e:
            _cflib_logger.warning("TcpDriver close error: %s", e)
        _cflib_logger.debug("TcpDriver closed")
        self.cpx = None

    cflib.crtp.tcpdriver.TcpDriver.close = _patched_tcp_driver_close

except Exception:
    pass


def _fill_sim_frame(
    image: np.ndarray, frame_id: int, _y: np.ndarray, _x: np.ndarray
) -> None:
    image[:] = ((_x + _y + frame_id) % 256).astype(np.uint8)


class WifiNode(Node):
    # Seconds without a frame before watchdog reconnects
    _NO_IMAGE_TIMEOUT = 5.0

    def __init__(self, sim=False):
        # DEBUG goes to file; console stays at INFO to avoid per-frame spam
        super().__init__(NODE_NAME, level=logging.DEBUG, console_level=logging.INFO)
        self.sim = sim
        self._cf_connected = threading.Event()
        self._frame_id = 0
        self.fps_counter = FPSCounter()
        self._console_buffer = ""
        self._frames_since_log = 0
        self._last_frame_time: float | None = None  # set in _publish_frame
        self._connect_time: float = 0.0  # set after successful connect
        self._reconnecting = False  # True while watchdog is reconnecting

    # --- Image pipeline ---

    def _receive_images(self) -> None:
        self.logger.info("Image reception thread started")
        while self.running:
            if self._reconnecting or not self.cf.is_connected():
                time.sleep(0.2)
                continue
            try:
                packet = self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.5)
            except queue.Empty:
                continue
            except Exception as e:
                if self.running and self.cf.is_connected():
                    self.logger.error(f"Error receiving CPX packet: {e}")
                time.sleep(0.2)  # avoid tight loop while link is down
                continue

            try:
                data = packet.data
                # Add a verbose debug print to see EXACTLY what we are receiving
                if data:
                    self.logger.debug(
                        f"Received CPX APP packet: len={len(data)}, first_byte=0x{data[0]:02X}"
                    )

                if len(data) != 11 or data[0] != 0xBC:
                    continue

                magic, width, height, depth, fmt, size = struct.unpack(
                    "<BHHBBI", data[:11]
                )
                self.logger.debug(
                    f"Header received: {width}x{height}, size={size}, fmt={fmt}"
                )

                img_stream = bytearray()
                assembly_ok = True

                while len(img_stream) < size and self.running and self.cf.is_connected():
                    try:
                        packet = self.cf.link.cpx.receivePacket(
                            CPXFunction.APP, timeout=0.5
                        )
                        img_stream.extend(packet.data)
                    except queue.Empty:
                        continue
                    except Exception as e:
                        if self.running and self.cf.is_connected():
                            self.logger.warning(
                                f"Error during image assembly, dropping frame: {e}"
                            )
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
            sample = self.image_port.publisher.loan_uninit()
            payload = sample.payload().contents
            payload.id = self._frame_id
            payload.timestamp = int(time.time() * 1000)

            # Copy frame data
            ctypes.memmove(payload.pixels, frame_data, min(len(frame_data), IMAGE_SIZE))
            self._frame_id += 1
            sample.assume_init().send()
            self.fps_counter.update()
            self._last_frame_time = time.time()  # watchdog heartbeat

            # Rate-limit FPS log to avoid flooding (full detail goes to file via DEBUG)
            self._frames_since_log += 1
            if self._frames_since_log >= _FRAME_LOG_INTERVAL:
                self.logger.debug(f"FPS: {self.fps_counter.fps:.1f}")
                self._frames_since_log = 0

            try:
                self.image_port.notifier.notify_with_custom_event_id(
                    self.image_port.event
                )
            except Exception:
                # Listener may have disconnected (e.g. GUI shutdown) — not an error
                pass

        except Exception as e:
            self.logger.error(f"Error publishing frame: {e}")

    def _image_watchdog(self) -> None:
        """Reconnect if no images arrive within _NO_IMAGE_TIMEOUT seconds.

        When the TCP connection is re-established the ESP32 re-sends
        WIFI_CTRL_STATUS_CLIENT_CONNECTED to the GAP8, which restarts
        the camera_task streaming loop.
        """
        self.logger.info("Image watchdog started")
        while self.running:
            time.sleep(1.0)
            if not self.running or self._reconnecting:
                continue

            ref = (
                self._last_frame_time
                if self._last_frame_time is not None
                else self._connect_time
            )
            elapsed = time.time() - ref

            if elapsed > self._NO_IMAGE_TIMEOUT:
                self.logger.warning(
                    f"No images for {elapsed:.0f}s — reconnecting to restart GAP8 streaming..."
                )
                self._reconnecting = True
                try:
                    self.cf.close_link()
                    time.sleep(1.5)
                    self._cf_connected.clear()
                    self.cf.open_link(CRAZYFLIE_URI)
                    if self._cf_connected.wait(timeout=10.0) and self.cf.is_connected():
                        self._prewarm_cpx_queue()
                        self.logger.info(
                            "Watchdog reconnect successful — waiting for images..."
                        )
                    else:
                        self.logger.warning("Watchdog reconnect failed, will retry")
                except Exception as e:
                    self.logger.error(f"Watchdog reconnect error: {e}")
                finally:
                    # Always reset the timer so we don't reconnect in a tight loop
                    self._last_frame_time = time.time()
                    self._reconnecting = False

    # --- Telemetry ---

    def _telemetry_loop(self) -> None:
        # Note: telemetry_port is created in run() before this thread starts
        while self.running:
            try:
                sample = self.telemetry_port.publisher.loan_uninit()
                payload = sample.payload().contents
                payload.fps = self.fps_counter.fps
                if self.sim:
                    payload.status = AppStatus.SIMULATING
                else:
                    if (
                        hasattr(self, "cf")
                        and self.cf is not None
                        and self.cf.is_connected()
                    ):
                        payload.status = AppStatus.CONNECTED
                    else:
                        payload.status = AppStatus.DISCONNECTED
                sample.assume_init().send()
                try:
                    self.telemetry_port.notifier.notify_with_custom_event_id(
                        self.telemetry_port.event
                    )
                except Exception:
                    # Listener may have disconnected — not an error
                    pass
                self.logger.debug(f"Sent {payload}")
            except Exception as e:
                if self.running:
                    self.logger.warning(f"Telemetry publish error: {e}")
            time.sleep(1)  # TODO

    # --- Flight control ---

    def _action_loop(self) -> None:
        self.logger.info("Action loop started")
        flying = False
        unlocking = 0  # countdown: sends thrust=0 packets before first hover setpoint (~500 ms)
        hover = [0.0, 0.0, 0.0, DEFAULT_HEIGHT]  # vx, vy, yawrate, zdist

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
                    act = sample.payload().contents
                    command = FlightCommand(act.command)
                    hover[0] = act.vx
                    hover[1] = act.vy
                    hover[2] = act.yawrate
                    hover[3] = act.zdistance
                    del act, sample

                    match command:
                        case FlightCommand.TAKEOFF:
                            if not flying:
                                self.logger.info(f"Taking off to z={hover[3]:.2f}m")
                                flying = True
                                unlocking = _UNLOCK_PACKETS

                        case FlightCommand.LAND:
                            if flying:
                                try:
                                    self.cf.commander.send_stop_setpoint()
                                except Exception as e:
                                    self.logger.warning(f"Land stop error: {e}")
                                flying = False
                                unlocking = 0
                                self.logger.info("Motors stopped — landed")

                        case FlightCommand.EMERGENCY_STOP:
                            self.logger.warning("EMERGENCY STOP — cutting motors")
                            try:
                                self.cf.commander.send_stop_setpoint()
                            except Exception as e:
                                self.logger.error(f"Emergency stop error: {e}")
                            flying = False
                            unlocking = 0
                            hover[0] = hover[1] = hover[2] = 0.0

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
            else:
                # Keep commander alive while grounded — prevents EKF drift between flights
                try:
                    self.cf.commander.send_setpoint(0, 0, 0, 0)
                except Exception as e:
                    self.logger.warning(f"Keep-alive error: {e}")

            time.sleep(_LOOP_INTERVAL)

        # Ensure motors stop on exit
        if flying:
            try:
                self.cf.commander.send_stop_setpoint()
            except Exception:
                pass
        self.logger.info("Action loop exited")

    # --- Crazyflie callbacks ---

    def _on_console(self, text):
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

    def _on_connection_failed(self, uri: str, msg: str) -> None:
        # msg from cflib includes a full embedded traceback — log only the summary line
        summary = msg.splitlines()[0] if msg else "unknown error"
        self.logger.warning(f"Crazyflie connection failed ({uri}): {summary}")
        self._cf_connected.set()

    def _on_disconnected(self, uri: str) -> None:
        self.logger.warning(f"Crazyflie disconnected: {uri}")

    # --- Connection ---

    def _prewarm_cpx_queue(self) -> None:
        """Pre-register the CPX APP queue before _receive_images starts.

        The cflib router silently drops packets whose function queue does not
        exist yet (see CPXRouter.run()).  The queue is created lazily on the
        first receivePacket() call, so if the GAP8 starts streaming before
        that call is made the first N frames are lost and the stream never
        recovers.  Calling receivePacket with a very short timeout here
        creates the queue immediately after connect.
        """
        try:
            self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.01)
        except queue.Empty:
            pass  # Expected — we just wanted the queue created
        self.logger.info("CPX APP queue pre-registered")

    @staticmethod
    def check_connection(host=CRAZYFLIE_IP):
        param = "-n" if platform.system().lower() == "windows" else "-c"
        command = ["ping", param, "1", "-W", "1", host]
        return (
            subprocess.call(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            == 0
        )

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

        self.cf.param.add_update_callback(
            group="deck", name="bcFlow2", cb=_deck_cb_flow
        )
        self.cf.param.add_update_callback(group="deck", name="bcAI", cb=_deck_cb_ai)
        self.cf.param.request_param_update("deck.bcFlow2")
        self.cf.param.request_param_update("deck.bcAI")

        if not decks_event.wait(timeout=5.0):
            self.logger.error("Required decks (AI-deck, Flow2) not detected!")
            return False

        self.logger.info("AI-deck and Flow2 decks detected.")
        return True

    def _connect_cf(self) -> bool:
        """Initialize cflib and connect to Crazyflie, retrying until connected or stopped.

        Returns True if connected, False if self.running became False.
        """
        cflib.crtp.init_drivers()
        self.cf = Crazyflie(rw_cache="./data/cache")

        # Disable parameter flood to save bandwidth for images
        self.cf.param.request_update_of_all_params = lambda: self.logger.info(
            "Parameter flood disabled"
        )

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

    def _run_sim(self) -> None:
        _y = np.arange(IMAGE_HEIGHT, dtype=np.uint16).reshape(-1, 1)
        _x = np.arange(IMAGE_WIDTH, dtype=np.uint16).reshape(1, -1)
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        while self.running:
            self.node.wait(iceoryx2.Duration.from_millis(100))
            if not self.running:
                break
            sample = self.image_port.publisher.loan_uninit()
            payload = sample.payload().contents
            payload.id = self._frame_id
            payload.timestamp = int(time.time() * 1000)
            image = np.ctypeslib.as_array(payload.pixels).reshape(
                IMAGE_HEIGHT, IMAGE_WIDTH
            )
            _fill_sim_frame(image, self._frame_id, _y, _x)
            self._frame_id += 1
            sample.assume_init().send()
            self.fps_counter.update()
            self.logger.debug(f"FPS: {self.fps_counter.fps:.1f}")
            self.image_port.notifier.notify_with_custom_event_id(self.image_port.event)

    # --- Entry point ---

    def run(self):
        try:
            # INITIALIZE ICEORYX2
            self.image_port = self.create_publisher(
                ServiceName.IMAGE, ImageData, EventId.IMAGE_READY
            )
            self.telemetry_port = self.create_publisher(
                ServiceName.TELEMETRY, TelemetryData, EventId.TELEMETRY_READY
            )

            self.action_port = self.create_subscriber(
                ServiceName.ACTION, ActionData, EventId.ACTION_READY
            )

            if self.action_port is None or self.action_port.subscriber is None:
                self.logger.error("Failed to create action subscriber")
                return

            if self.sim:
                self._run_sim()
            else:
                self.logger.info(
                    f"Waiting for network connectivity to {CRAZYFLIE_IP}..."
                )
                while self.running and not WifiNode.check_connection():
                    self.node.wait(iceoryx2.Duration.from_secs(5))

                if not self.running:
                    return

                # CONNECT TO CRAZYFLIE
                if not self._connect_cf():
                    return

                # Check if AI-deck and Flow2 decks are attached
                if not self._check_required_decks():
                    self.running = False
                    return

                # Pre-register CPX APP queue to avoid silent frame drops at startup
                self._prewarm_cpx_queue()
                self._connect_time = time.time()

                # START BACKGROUND THREADS
                threading.Thread(target=self._action_loop, daemon=True).start()
                threading.Thread(target=self._receive_images, daemon=True).start()
                threading.Thread(target=self._telemetry_loop, daemon=True).start()
                threading.Thread(target=self._image_watchdog, daemon=True).start()

                self.logger.info(
                    "Node fully operational, receiving frames and commands..."
                )

                # Main loop: just keep the process alive.
                while self.running:
                    time.sleep(0.5)

        except (
            KeyboardInterrupt,
            iceoryx2.NodeWaitFailure,
            iceoryx2.ListenerWaitError,
        ):
            pass
        except Exception as e:
            self.logger.error(f"WifiNode error: {e}")
        finally:
            self.running = False
            if not self.sim and hasattr(self, "cf"):
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
