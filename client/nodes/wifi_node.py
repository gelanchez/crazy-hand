

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
import logging

from cflib.crazyflie import Crazyflie
from cflib.cpx import CPXFunction
from PIL import Image
from common.constants import (
    CRAZYFLIE_IP,
    CRAZYFLIE_URI,
    ServiceName,
    EventId,
    IMAGE_WIDTH,
    IMAGE_HEIGHT,
    IMAGE_SIZE,
)
from common.payloads import ImageData, CommandData
from common.utils import setup_logging, setup_iceoryx2_config

NODE_NAME = "wifi_node"

logger = setup_logging(NODE_NAME, logging.DEBUG)


def _fill_sim_frame(
    image: np.ndarray, frame_id: int, _y: np.ndarray, _x: np.ndarray
) -> None:
    """Diagonal scrolling gradient — each frame shifts one pixel, making
    dropped or out-of-order frames immediately visible."""
    image[:] = ((_x + _y + frame_id) % 256).astype(np.uint8)


class WifiNode:
    def __init__(self, sim=False):
        setup_iceoryx2_config()
        self.sim = sim
        self._cf_connected = threading.Event()
        self._frame_id = 0
        self._running = False
        self._tcp_socket = None
        logger.info(f"{NODE_NAME} initialized")

    def _receive_images(self) -> None:
        logger.info("Image reception thread started")
        while self._running:
            try:
                packet = self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.5)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error receiving CPX packet: {e}")
                continue

            try:
                data = packet.data
                if len(data) < 11 or data[0] != 0xBC:
                    continue

                magic, width, height, depth, fmt, size = struct.unpack("<BHHBBI", data[:11])
                
                # The header is sent in its own packet, so we start with an empty stream for the image data
                img_stream = bytearray()
                
                # Accumulate subsequent packets until we have the full image
                # logger.debug(f"Header received: {width}x{height}, size={size}, fmt={fmt}")
                while len(img_stream) < size and self._running:
                    try:
                        packet = self.cf.link.cpx.receivePacket(CPXFunction.APP, timeout=0.5)
                        img_stream.extend(packet.data)
                        # if len(img_stream) % (1024 * 5) < 1024: # Log every ~5KB
                        #     logger.debug(f"Progress: {len(img_stream)}/{size} bytes")
                    except queue.Empty:
                        logger.warning("Timeout waiting for image data packet")
                        continue
                
                if not self._running:
                    break
                    
                # logger.debug(f"Frame assembled: {len(img_stream)} bytes")
                self._publish_frame(bytes(img_stream[:size]), fmt)
            except Exception as e:
                logger.error(f"Error assembling image: {e}")

    def _publish_frame(self, frame_data: bytes, fmt: int) -> None:
        if fmt == 1:  # JPEG
            img = Image.open(io.BytesIO(frame_data)).convert("L")
            frame_data = img.tobytes()

        if self.image_publisher:
            try:
                sample = self.image_publisher.loan_uninit()
                payload = sample.payload().contents
                payload.id = self._frame_id
                payload.timestamp = int(time.time() * 1000)
                
                # Copy the pixels into the shared memory
                size = min(len(frame_data), IMAGE_SIZE)
                ctypes.memmove(payload.pixels, frame_data, size)
                
                sample = sample.assume_init()
                sample.send()
                
                if self.image_notifier:
                    self.image_notifier.notify_with_custom_event_id(self.image_ready_event)
                    
                self._frame_id += 1
                # logger.debug(f"Published frame {self._frame_id}")
            except Exception as e:
                logger.error(f"Failed to publish frame: {e}")

    def _on_console(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                # logger.debug(f"[GAP8] {line}")
                pass

    def _on_connected(self, uri: str) -> None:
        logger.info(f"Crazyflie connected: {uri}")
        # Legacy client works perfectly without socket tuning, so we leave the default OS TCP stack alone.
        self._cf_connected.set()


    def _command_loop(self) -> None:
        command_ready_event = iceoryx2.EventId.new(EventId.COMMAND_READY)

        # Wait for the command service (gui_node creates it)
        command_subscriber = None
        command_listener = None
        while self._running:
            try:
                command_service = (
                    self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.COMMAND))
                    .publish_subscribe(CommandData)
                    .open_or_create()
                )
                command_subscriber = command_service.subscriber_builder().create()
                command_event = (
                    self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.COMMAND))
                    .event()
                    .open()
                )
                command_listener = command_event.listener_builder().create()
                logger.info("Command service connected")
                break
            except Exception:
                time.sleep(0.1)

        if command_subscriber is None or command_listener is None:
            return

        sample = None
        while self._running:
            sample = None  # release any previous borrow before blocking
            try:
                event_id = command_listener.try_wait_one()
                if event_id is None:
                    time.sleep(0.02)  # Yield the GIL so CPXRouter can read packets!
                    continue
            except Exception:
                break
            if event_id != command_ready_event:
                continue
            try:
                sample = command_subscriber.receive()
            except Exception as e:
                logger.warning(f"Command receive error: {e}")
                continue
            if sample is not None:
                cmd = sample.payload().contents
                active = bool(cmd.active)
                vx, vy, yawrate, zdist = cmd.vx, cmd.vy, cmd.yawrate, cmd.zdistance
                del cmd, sample
                sample = None
                try:
                    if active:
                        self.cf.commander.send_hover_setpoint(vx, vy, yawrate, zdist)
                    else:
                        self.cf.commander.send_stop_setpoint()
                except Exception as e:
                    logger.warning(f"Failed to send command: {e}")

    def _on_connection_failed(self, uri: str, msg: str) -> None:
        logger.error(f"Crazyflie connection failed ({uri}): {msg}")
        self._cf_connected.set()

    def _on_disconnected(self, uri: str) -> None:
        logger.warning(f"Crazyflie disconnected: {uri}")

    def check_connection(self):
        host = CRAZYFLIE_IP
        param = "-n" if platform.system().lower() == "windows" else "-c"
        command = ["ping", param, "1", "-W", "1", host]
        return (
            subprocess.call(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            == 0
        )

    def run(self):
        logger.info(f"{NODE_NAME} running")

        # iceoryx2 initialization
        self.node = (
            iceoryx2.NodeBuilder.new()
            .name(iceoryx2.NodeName.new(NODE_NAME))
            .create(iceoryx2.ServiceType.Ipc)
        )

        self.image_service = (
            self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.IMAGE))
            .publish_subscribe(ImageData)
            .open_or_create()
        )
        self.image_publisher = self.image_service.publisher_builder().create()

        self.image_event = (
            self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.IMAGE))
            .event()
            .open_or_create()
        )
        self.image_notifier = self.image_event.notifier_builder().create()
        self.image_ready_event = iceoryx2.EventId.new(EventId.IMAGE_READY)

        try:
            frame_id = 0

            if self.sim:
                # Precompute coordinate grids once
                _y = np.arange(IMAGE_HEIGHT, dtype=np.uint16).reshape(-1, 1)
                _x = np.arange(IMAGE_WIDTH, dtype=np.uint16).reshape(1, -1)

                while True:
                    self.node.wait(iceoryx2.Duration.from_millis(1000))

                    # Loan unitialized sample from the publisher's memory pool
                    sample = self.image_publisher.loan_uninit()
                    payload = sample.payload().contents

                    # metadata
                    payload.id = frame_id
                    payload.timestamp = int(time.time() * 1000)

                    # zero-copy buffer view into the shared memory region
                    image = np.ctypeslib.as_array(payload.pixels).reshape(
                        IMAGE_HEIGHT, IMAGE_WIDTH
                    )

                    _fill_sim_frame(image, frame_id, _y, _x)

                    frame_id += 1

                    # Send
                    sample = sample.assume_init()
                    sample.send()

                    self.image_notifier.notify_with_custom_event_id(
                        self.image_ready_event
                    )
            else:
                logger.info(f"Waiting for network connectivity to {CRAZYFLIE_IP}...")
                while not self.check_connection():
                    logger.warning(f"Cannot reach {CRAZYFLIE_IP}, retrying in 5s...")
                    self.node.wait(iceoryx2.Duration.from_secs(5))

                cflib.crtp.init_drivers()
                self.cf = Crazyflie(rw_cache="./data/cache")
                self.cf.fully_connected.add_callback(self._on_connected)
                self.cf.connection_failed.add_callback(self._on_connection_failed)
                self.cf.disconnected.add_callback(self._on_disconnected)
                self.cf.console.receivedChar.add_callback(self._on_console)

                while not self.cf.is_connected():
                    self._cf_connected.clear()
                    logger.info(f"Opening link to {CRAZYFLIE_URI}...")
                    self.cf.open_link(CRAZYFLIE_URI)
                    # Increase timeout to 10s to allow for TOC download over WiFi
                    if self._cf_connected.wait(timeout=10.0):
                        logger.info("Handshake complete, link fully established")
                        break
                    else:
                        logger.warning("Connection handshake timed out, retrying...")
                        if self.cf.is_connected():
                             break
                        time.sleep(0.5)

                self._running = True
                image_thread = threading.Thread(
                    target=self._receive_images, daemon=True
                )
                image_thread.start()
                
                threading.Thread(
                    target=self._command_loop, daemon=True
                ).start()
                logger.info("Receiving frames via polling thread...")

                while True:
                    time.sleep(1.0)  # Use time.sleep to ensure GIL is released

        except (iceoryx2.NodeWaitFailure, KeyboardInterrupt):
            try:
                logger.info(f"{NODE_NAME} shutting down...")
            except Exception:
                pass
            self._running = False
            if not self.sim and hasattr(self, "cf"):
                try:
                    self.cf.close_link()
                except Exception:
                    pass


@click.command()
@click.option("--sim", is_flag=True, help="Run in simulation mode")
def main(sim):
    global logger
    logger = setup_logging(NODE_NAME, logging.DEBUG)
    
    node = WifiNode(sim=sim)
    node.run()


if __name__ == "__main__":
    main()
