

import time
import logging
import iceoryx2
from common.payloads import ImageData
from common.constants import ServiceName, EventId
from common.utils import setup_logging, setup_iceoryx2_config

NODE_NAME = "processor_node"

logger = setup_logging(NODE_NAME, logging.DEBUG)


class ProcessorNode:
    def __init__(self):
        setup_iceoryx2_config()
        logger.info(f"{NODE_NAME} initialized")

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

        try:
            while True:
                event_id = self.image_listener.blocking_wait_one()
                if event_id == self.image_ready_event:
                    sample = self.image_subscriber.receive()
                    if sample is not None:
                        data = sample.payload()
                        del data, sample

        except (iceoryx2.NodeWaitFailure, KeyboardInterrupt):
            pass


def main():
    node = ProcessorNode()
    node.run()


if __name__ == "__main__":
    main()

