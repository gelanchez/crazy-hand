import ctypes
import io
import struct
import time
import threading
import click
import subprocess
import platform
import iceoryx2
import numpy as np
import queue
import cflib.crtp
import logging

from cflib.crazyflie import Crazyflie
from cflib.cpx import CPXFunction
from PIL import Image
from client.common.constants import (
    CRAZYFLIE_IP,
    CRAZYFLIE_URI,
    ServiceName,
    EventId,
    IMAGE_WIDTH,
    IMAGE_HEIGHT,
    IMAGE_SIZE,
)
from client.common.payloads import ImageData, CommandData
from client.common.node import Node

NODE_NAME = "wifi_node"

def _fill_sim_frame(
    image: np.ndarray, frame_id: int, _y: np.ndarray, _x: np.ndarray
) -> None:
    image[:] = ((_x + _y + frame_id) % 256).astype(np.uint8)

class WifiNode(Node):
    def __init__(self, sim=False):
        super().__init__(NODE_NAME, level=logging.DEBUG)
        self.sim = sim
        self._cf_connected = threading.Event()
        self._frame_id = 0

    def _receive_images(self) -> None:
        self.logger.info("Image reception thread started")
        while self.running:
            try:
                packet = self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.5)
            except queue.Empty:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"Error receiving CPX packet: {e}")
                continue

            try:
                data = packet.data
                if len(data) < 11 or data[0] != 0xBC:
                    continue

                magic, width, height, depth, fmt, size = struct.unpack("<BHHBBI", data[:11])
                self.logger.debug(f"Header received: {width}x{height}, size={size}, fmt={fmt}")
                
                img_stream = bytearray()
                
                while len(img_stream) < size and self.running:
                    try:
                        packet = self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.5)
                        img_stream.extend(packet.data)
                    except queue.Empty:
                        continue
                
                if not self.running:
                    break
                    
                self._publish_frame(bytes(img_stream[:size]), fmt)
            except Exception as e:
                if self.running:
                    self.logger.error(f"Error assembling image: {e}")


    def _publish_frame(self, frame_data: bytes, fmt: int) -> None:
        if fmt == 1:  # JPEG
            try:
                img = Image.open(io.BytesIO(frame_data)).convert("L")
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
            
            sample.assume_init().send()
            self.image_port.notifier.notify_with_custom_event_id(self.image_port.event)
            self._frame_id += 1
        except Exception as e:
            self.logger.error(f"Error publishing frame: {e}")

    def _command_loop(self) -> None:
        self.logger.info("Command loop started")
        armed = False
        hover = (0.0, 0.0, 0.0, 0.3)  # vx, vy, yawrate, zdist
        sample = None
        while self.running:
            sample = None
            try:
                event_id = self.cmd_port.listener.try_wait_one()
            except Exception as e:
                if self.running:
                    self.logger.error(f"Command listener error: {e}")
                    time.sleep(0.1)
                continue

            if event_id is not None and event_id == self.cmd_port.event:
                try:
                    sample = self.cmd_port.subscriber.receive()
                except Exception as e:
                    self.logger.warning(f"Command receive error: {e}")

                if sample is not None:
                    cmd = sample.payload().contents
                    active = bool(cmd.active)
                    hover = (cmd.vx, cmd.vy, cmd.yawrate, cmd.zdistance)
                    del cmd, sample
                    sample = None

                    # Toggle armed state on active=True (Space press)
                    if active and not armed:
                        armed = True
                        self.logger.info(f"ARMED — hovering at z={hover[3]:.2f}")
                    elif active and armed:
                        armed = False
                        self.logger.info("DISARMED")
                        try:
                            self.cf.commander.send_stop_setpoint()
                        except Exception as e:
                            self.logger.warning(f"Failed to send stop: {e}")

            # While armed, continuously re-send hover setpoints
            if armed:
                try:
                    self.cf.commander.send_hover_setpoint(*hover)
                except Exception as e:
                    self.logger.warning(f"Hover send error (still armed): {e}")

            # ~20Hz loop: balance between CF watchdog needs and CPX bandwidth
            time.sleep(0.05)
        
        # Ensure motors stop on exit
        if armed:
            try:
                self.cf.commander.send_stop_setpoint()
            except Exception:
                pass
        self.logger.info("Command loop exited")

    def _on_console(self, text):
        self.logger.info(f"CF Console: {text.strip()}")

    def _on_connected(self, uri):
        self.logger.info(f"Crazyflie connected: {uri}")
        self._cf_connected.set()

    def _on_connection_failed(self, uri: str, msg: str) -> None:
        self.logger.error(f"Crazyflie connection failed ({uri}): {msg}")
        self._cf_connected.set()

    def _on_disconnected(self, uri: str) -> None:
        self.logger.warning(f"Crazyflie disconnected: {uri}")

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

    def run(self):
        try:
            if self.sim:
                # In sim mode, we can init iceoryx2 immediately
                self.image_port = self.create_publisher(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY)                
                _y = np.arange(IMAGE_HEIGHT, dtype=np.uint16).reshape(-1, 1)
                _x = np.arange(IMAGE_WIDTH, dtype=np.uint16).reshape(1, -1)
                while self.running:
                    self.node.wait(iceoryx2.Duration.from_millis(1000))
                    if not self.running: break
                    sample = self.image_port.publisher.loan_uninit()
                    payload = sample.payload().contents
                    payload.id = self._frame_id
                    payload.timestamp = int(time.time() * 1000)
                    image = np.ctypeslib.as_array(payload.pixels).reshape(IMAGE_HEIGHT, IMAGE_WIDTH)
                    _fill_sim_frame(image, self._frame_id, _y, _x)
                    self._frame_id += 1
                    sample.assume_init().send()
                    self.image_port.notifier.notify_with_custom_event_id(self.image_port.event)
            else:
                self.logger.info(f"Waiting for network connectivity to {CRAZYFLIE_IP}...")
                while self.running and not WifiNode.check_connection():
                    self.node.wait(iceoryx2.Duration.from_secs(5))

                if not self.running: return

                # 1. CONNECT TO CRAZYFLIE FIRST (No iceoryx2 yet)
                cflib.crtp.init_drivers()
                self.cf = Crazyflie(rw_cache="./data/cache")
                
                # MONKEY-PATCH: Disable parameter flood to save bandwidth for images
                self.cf.param.request_update_of_all_params = lambda: self.logger.info("Parameter flood disabled")
                
                self.cf.connected.add_callback(self._on_connected)
                self.cf.connection_failed.add_callback(self._on_connection_failed)
                self.cf.disconnected.add_callback(self._on_disconnected)

                while self.running and not self.cf.is_connected():
                    self._cf_connected.clear()
                    self.logger.info(f"Opening link to {CRAZYFLIE_URI}...")
                    self.cf.open_link(CRAZYFLIE_URI)
                    if self._cf_connected.wait(timeout=60.0):
                        if self.cf.is_connected():
                            break
                    self.logger.warning("Connection timed out, retrying...")
                    try: self.cf.close_link()
                    except: pass
                    time.sleep(1.0)

                if not self.running: return

                # 2. NOW INITIALIZE ICEORYX2
                self.image_port = self.create_publisher(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY)

                self.cmd_port = self.create_subscriber(ServiceName.COMMAND, CommandData, EventId.COMMAND_READY)

                if self.cmd_port.subscriber is None:
                    self.logger.error("Failed to create command subscriber")
                    return

                # 3. START BACKGROUND THREADS
                threading.Thread(target=self._command_loop, daemon=True).start()
                threading.Thread(target=self._receive_images, daemon=True).start()

                self.logger.info("Node fully operational, receiving frames and commands...")

                # Main loop: just keep the process alive.
                # Command sending is handled by _command_loop.
                # Image reception is handled by _receive_images.
                while self.running:
                    time.sleep(0.5)

        except (KeyboardInterrupt, iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError):
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
