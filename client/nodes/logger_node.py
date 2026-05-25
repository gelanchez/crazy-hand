import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import iceoryx2
import numpy as np

from client.common.blackboards import CONFIG
from client.common.constants import (
    IMAGE_HEIGHT,
    IMAGE_SIZE,
    IMAGE_WIDTH,
    ActionSource,
    AppStatus,
    EventId,
    FlightCommand,
    FlightState,
    ServiceName,
)
from client.common.database import (
    ActionSample,
    Database,
    PerceptionSample,
    TelemetrySample,
)
from client.common.node import Node
from client.common.payloads import ActionData, ImageData, PerceptionData, TelemetryData

NODE_NAME = "logger_node"
IMAGES_PATH = Path("./data/images")
PROCESSED_IMAGES_PATH = Path("./data/processed")


class LoggerNode(Node):
    def __init__(self, level=logging.INFO):
        super().__init__(NODE_NAME, level=level)
        self.database = Database()
        # Background thread for non-blocking image saves
        self._save_queue: queue.Queue = queue.Queue(maxsize=10)
        self._save_thread = threading.Thread(
            target=self._image_save_worker, daemon=True
        )
        self._save_thread.start()
        # Cached blackboard values — refreshed at most once per second
        self._save_images: bool = False
        self._save_images_last_read: float = 0.0

    def _image_save_worker(self):
        """Consume (pixels_bytes, path) tuples and write them to disk."""
        while True:
            item = self._save_queue.get()
            if item is None:  # sentinel — shut down
                break
            pixels_bytes, path = item
            try:
                arr = np.frombuffer(pixels_bytes, dtype=np.uint8)
                if len(arr) == IMAGE_SIZE:
                    arr = arr.reshape(IMAGE_HEIGHT, IMAGE_WIDTH)
                else:
                    arr = arr.reshape(IMAGE_HEIGHT, IMAGE_WIDTH, 3)
                cv2.imwrite(str(path), arr)
            except Exception as e:
                self.logger.warning(f"Image save failed: {e}")
            finally:
                self._save_queue.task_done()

    def _get_save_images(self) -> bool:
        """Return save_images flag, refreshing from blackboard at most once per second."""
        now = time.monotonic()
        if now - self._save_images_last_read >= 1.0:
            try:
                self._save_images = self.blackboard_read(
                    self.blackboard_reader, "save_images"
                )
            except Exception:
                pass
            self._save_images_last_read = now
        return self._save_images

    def _enqueue_save(
        self, pixels_bytes: bytes, path: Path, label: str = "frame"
    ) -> None:
        try:
            self._save_queue.put_nowait((pixels_bytes, path))
            self.logger.debug(f"Enqueued save: {path.name}")
        except queue.Full:
            self.logger.warning(f"Image save queue full — dropping {label}")

    def _handle_image(self):
        # Drain all queued samples to prevent notification pile-up
        while True:
            sample = self.image_port.subscriber.receive()
            if sample is None:
                break
            if self._get_save_images():
                data = sample.payload()
                pixels_bytes = bytes(data.contents.pixels)
                image_name = f"{data.contents.timestamp}.png"
                del data
                self._enqueue_save(pixels_bytes, IMAGES_PATH / image_name)
            del sample

    def _handle_telemetry(self):
        # Drain all queued samples to prevent notification pile-up
        while True:
            sample = self.telemetry_port.subscriber.receive()
            if sample is None:
                break
            data = sample.payload()
            self.logger.debug(f"Received Telemetry: {data.contents}")
            try:
                status_enum = AppStatus(data.contents.status)
            except ValueError:
                status_enum = AppStatus.DISCONNECTED
            telemetry_sample = TelemetrySample(
                ts=datetime.now(timezone.utc),
                fps=data.contents.fps,
                status=status_enum,
            )
            del data, sample
            self.database.log(telemetry_sample)

    def _handle_action(self):
        # Drain all queued samples to prevent notification pile-up
        while True:
            sample = self.action_port.subscriber.receive()
            if sample is None:
                break
            data = sample.payload()
            self.logger.debug(f"Received Action: {data.contents}")
            c = data.contents
            command = FlightCommand(c.command)
            zero = not c.active or command == FlightCommand.EMERGENCY_STOP
            action_sample = ActionSample(
                ts=datetime.now(timezone.utc),
                active=c.active,
                command=command,
                state=FlightState(c.state),
                source=ActionSource(c.source),
                vx=0.0 if zero else c.vx,
                vy=0.0 if zero else c.vy,
                yawrate=0.0 if zero else c.yawrate,
                zdistance=0.0 if zero else c.zdistance,
                ema_x=c.ema_x,
                ema_y=c.ema_y,
            )
            del c, data, sample
            self.database.log(action_sample)

    def _handle_perception(self):
        # Drain all queued samples to prevent notification pile-up
        while True:
            sample = self.perception_port.subscriber.receive()
            if sample is None:
                break
            data = sample.payload()
            self.logger.debug(f"Received Perception: {data.contents}")

            raw_gesture = data.contents.gesture_name
            if isinstance(raw_gesture, bytes):
                gesture_name = raw_gesture.decode("utf-8", errors="ignore").rstrip(
                    "\x00"
                )
            else:
                gesture_name = str(raw_gesture)

            perception_sample = PerceptionSample(
                ts=datetime.now(timezone.utc),
                hand_detected=data.contents.hand_detected,
                hand_x=data.contents.hand_x,
                hand_y=data.contents.hand_y,
                gesture_name=gesture_name,
                gesture_confidence=data.contents.gesture_confidence,
            )

            if self._get_save_images():
                processed_bytes = bytes(data.contents.processed_pixels)
                proc_name = f"{data.contents.timestamp}.png"
                self._enqueue_save(
                    processed_bytes,
                    PROCESSED_IMAGES_PATH / proc_name,
                    label="processed frame",
                )

            del data, sample
            self.database.log(perception_sample)

    def run(self):
        # 1. Setup Ports
        self.image_port = self.create_subscriber(
            ServiceName.IMAGE, ImageData, EventId.IMAGE_READY
        )
        self.telemetry_port = self.create_subscriber(
            ServiceName.TELEMETRY, TelemetryData, EventId.TELEMETRY_READY
        )
        self.action_port = self.create_subscriber(
            ServiceName.ACTION, ActionData, EventId.ACTION_READY
        )
        self.perception_port = self.create_subscriber(
            ServiceName.PERCEPTION, PerceptionData, EventId.PERCEPTION_READY
        )

        if (
            self.image_port.subscriber is None
            or self.telemetry_port.subscriber is None
            or self.action_port.subscriber is None
            or self.perception_port.subscriber is None
        ):
            return

        self.blackboard_reader = self.create_blackboard_reader("/config", CONFIG)
        if self.blackboard_reader is None:
            return

        IMAGES_PATH.mkdir(parents=True, exist_ok=True)
        PROCESSED_IMAGES_PATH.mkdir(parents=True, exist_ok=True)

        # TODO: Replace timed_wait_one workaround with WaitSet once the
        # iceoryx2 spinning bug is fixed (see GitHub issue in thesis/Iceoryx2.md).
        # Intended WaitSet code (4 attachments — image, telemetry, action, perception):
        #
        #   waitset = iceoryx2.WaitSetBuilder.new().create(iceoryx2.ServiceType.Ipc)
        #   image_guard     = waitset.attach_notification(self.image_port.listener)
        #   telemetry_guard = waitset.attach_notification(self.telemetry_port.listener)
        #   action_guard    = waitset.attach_notification(self.action_port.listener)
        #   perception_guard= waitset.attach_notification(self.perception_port.listener)
        #   while self.running:
        #       ids, result = waitset.wait_and_process_with_timeout(Duration.from_millis(100))
        #       for event_id in ids:
        #           if event_id.has_event_from(image_guard): self._handle_image()
        #           elif event_id.has_event_from(telemetry_guard): self._handle_telemetry()
        #           elif event_id.has_event_from(action_guard): self._handle_action()
        #           elif event_id.has_event_from(perception_guard): self._handle_perception()
        #   finally: [all guards].delete(); waitset.delete()
        self.logger.info("Polling loop initialized, listening for events...")

        try:
            while self.running:
                # Block up to 100ms waiting for an image event (highest-frequency)
                event_id = self.image_port.listener.timed_wait_one(
                    iceoryx2.Duration.from_millis(100)
                )
                if event_id == self.image_port.event:
                    self._handle_image()

                # Non-blocking drain of remaining subscribers each iteration
                self._handle_telemetry()
                self._handle_action()
                self._handle_perception()

        except (
            iceoryx2.NodeWaitFailure,
            iceoryx2.ListenerWaitError,
            KeyboardInterrupt,
        ):
            pass
        except Exception as e:
            self.logger.error(f"LoggerNode error: {e}", exc_info=True)
        finally:
            self._save_queue.put(None)  # signal save worker to stop
            self._save_thread.join(timeout=5)
            self.database.close()
            self.logger.info(f"{NODE_NAME} shut down")


def main():
    node = LoggerNode()
    node.run()


if __name__ == "__main__":
    main()
