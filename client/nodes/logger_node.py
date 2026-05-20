import logging
import queue
import threading

import iceoryx2

from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from client.common.blackboards import CONFIG
from client.common.constants import EventId, IMAGE_HEIGHT, IMAGE_WIDTH, ServiceName
from client.common.database import ActionSample, Database, TelemetrySample
from client.common.node import Node
from client.common.payloads import ActionData, ImageData, TelemetryData

NODE_NAME = "logger_node"
IMAGES_PATH = Path("./data/images")


class LoggerNode(Node):
    def __init__(self, level=logging.INFO):
        super().__init__(NODE_NAME, level=level)
        self.database = Database()
        # Background thread for non-blocking image saves
        self._save_queue: queue.Queue = queue.Queue(maxsize=10)
        self._save_thread = threading.Thread(target=self._image_save_worker, daemon=True)
        self._save_thread.start()

    def _image_save_worker(self):
        """Consume (pixels_bytes, path) tuples and write them to disk."""
        while True:
            item = self._save_queue.get() # TODO Should we add a timeout and catch the exception?
            if item is None:  # sentinel — shut down
                break
            pixels_bytes, path = item
            try:
                image = Image.frombuffer("L", (IMAGE_WIDTH, IMAGE_HEIGHT), pixels_bytes, "raw", "L", 0, 1)
                image.save(path)
            except Exception as e:
                self.logger.warning(f"Image save failed: {e}")
            finally:
                self._save_queue.task_done()

    def run(self):
        # 1. Setup Ports
        self.image_port = self.create_subscriber(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY)
        self.telemetry_port = self.create_subscriber(ServiceName.TELEMETRY, TelemetryData, EventId.TELEMETRY_READY)
        self.action_port = self.create_subscriber(ServiceName.ACTION, ActionData, EventId.ACTION_READY)
        
        if self.image_port.subscriber is None or self.telemetry_port.subscriber is None or self.action_port.subscriber is None:
            return

        self.blackboard_reader = self.create_blackboard_reader("/config", CONFIG)
        if self.blackboard_reader is None:
            return

        IMAGES_PATH.mkdir(parents=True, exist_ok=True)

        # 2. Setup WaitSet
        # We use WaitSet to multiplex between multiple listeners in a single thread.
        waitset = iceoryx2.WaitSetBuilder.new().create(iceoryx2.ServiceType.Ipc)
        
        # Attach listeners. The guards must stay in scope to remain attached.
        image_guard = waitset.attach_notification(self.image_port.listener)
        telemetry_guard = waitset.attach_notification(self.telemetry_port.listener)
        action_guard = waitset.attach_notification(self.action_port.listener)

        self.logger.info("WaitSet initialized, listening for events...")

        try:
            while self.running:
                # 3. Wait for events (blocks until an event arrives or timeout)
                ids, result = waitset.wait_and_process_with_timeout(
                    iceoryx2.Duration.from_millis(100)
                )

                # Check if we were interrupted by a signal
                if result in (iceoryx2.WaitSetRunResult.Interrupt, 
                              iceoryx2.WaitSetRunResult.TerminationRequest):
                    self.running = False
                    break

                for event_id in ids:
                    if event_id.has_event_from(image_guard):
                        sample = self.image_port.subscriber.receive()
                        if sample is not None:
                            save_images = self.blackboard_read(self.blackboard_reader, "save_images")
                            if save_images:
                                data = sample.payload()
                                # Copy pixels out of shared memory before releasing the sample
                                pixels_bytes = bytes(data.contents.pixels)
                                image_name = f"{data.contents.timestamp}.png"
                                try:
                                    self._save_queue.put_nowait((pixels_bytes, IMAGES_PATH / image_name))
                                    self.logger.debug(f"Enqueued save: {image_name}")
                                except queue.Full:
                                    self.logger.warning("Image save queue full — dropping frame")
                            del sample

                    # Handle Telemetry Event
                    elif event_id.has_event_from(telemetry_guard):
                        sample = self.telemetry_port.subscriber.receive()
                        if sample is not None:
                            data = sample.payload()
                            self.logger.debug(f"Received Telemetry: {data.contents}")
                            from client.common.constants import AppStatus
                            try:
                                status_enum = AppStatus(data.contents.status)
                            except ValueError:
                                status_enum = AppStatus.DISCONNECTED
                            telemetry_sample = TelemetrySample(ts=datetime.now(timezone.utc), fps=data.contents.fps, status=status_enum)
                            del data, sample
                            self.database.log(telemetry_sample)

                    # Handle Action Event
                    elif event_id.has_event_from(action_guard):
                        sample = self.action_port.subscriber.receive()
                        if sample is not None:
                            data = sample.payload()
                            self.logger.debug(f"Received Action: {data.contents}")
                            c = data.contents
                            action_sample = ActionSample(
                                ts=datetime.now(timezone.utc),
                                active=c.active,
                                vx=c.vx,
                                vy=c.vy,
                                yawrate=c.yawrate,
                                zdistance=c.zdistance
                            )
                            del c, data, sample
                            self.database.log(action_sample)

        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError, KeyboardInterrupt):
            pass
        except Exception as e:
            self.logger.error(f"LoggerNode error: {e}", exc_info=True)
        finally:
            image_guard.delete()
            telemetry_guard.delete()
            action_guard.delete()
            waitset.delete()
            self._save_queue.put(None)  # signal save worker to stop
            self._save_thread.join(timeout=5)
            self.database.close()
            self.logger.info(f"{NODE_NAME} shut down")


def main():
    node = LoggerNode()
    node.run()


if __name__ == "__main__":
    main()
