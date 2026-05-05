

import time
import logging
import iceoryx2
import signal
from common.payloads import ImageData
from common.constants import ServiceName, EventId
from common.utils import setup_logging, setup_iceoryx2_config

NODE_NAME = "processor_node"

logger = setup_logging(NODE_NAME, logging.DEBUG)


class ProcessorNode:
    def __init__(self):
        setup_iceoryx2_config()
        self._running = True
        logger.info(f"{NODE_NAME} initialized")

    def _signal_handler(self, sig, frame):
        self._running = False

    def run(self):
        logger.info(f"{NODE_NAME} running")
        # TODO Config file or env
        self.node = (
            iceoryx2.NodeBuilder.new()
            .name(iceoryx2.NodeName.new(NODE_NAME))
            .create(iceoryx2.ServiceType.Ipc)
        )

        logger.info("Waiting for image service...")
        while True:
            try:
                self.image_service = (
                    self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.IMAGE))
                    .publish_subscribe(ImageData)
                    .open_or_create()
                )
                break
            except iceoryx2.PublishSubscribeOpenError:
                time.sleep(0.1)
        logger.info("Image service connected")
        self.image_subscriber = self.image_service.subscriber_builder().create()

        while True:
            try:
                self.image_event = (
                    self.node.service_builder(iceoryx2.ServiceName.new(ServiceName.IMAGE))
                    .event()
                    .open_or_create()
                )
                break
            except Exception:
                time.sleep(0.1)
        logger.info("Image event connected")
        self.image_listener = self.image_event.listener_builder().create()
        self.image_ready_event = iceoryx2.EventId.new(EventId.IMAGE_READY)

        signal.signal(signal.SIGINT, self._signal_handler)

        try:
            while self._running:
                # Use timed wait instead of blocking wait to allow periodic check of self._running
                event_id = self.image_listener.timed_wait_one(
                    iceoryx2.Duration.from_millis(500)
                )
                if event_id == self.image_ready_event:
                    sample = self.image_subscriber.receive()
                    if sample is not None:
                        data = sample.payload()
                        del data, sample

        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError, KeyboardInterrupt):
            pass
        finally:
            logger.info(f"{NODE_NAME} shut down")


def main():
    node = ProcessorNode()
    node.run()


if __name__ == "__main__":
    main()

