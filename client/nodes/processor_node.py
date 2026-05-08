import logging
import iceoryx2
from client.common.payloads import ImageData
from client.common.constants import ServiceName, EventId
from client.common.node import Node

NODE_NAME = "processor_node"

class ProcessorNode(Node):
    def __init__(self, level=logging.DEBUG):
        super().__init__(NODE_NAME, level=level)

    def run(self):
        self.image_port = self.create_subscriber(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY)
        
        if self.image_port.subscriber is None:
            return

        try:
            while self.running:
                # Use timed wait instead of blocking wait to allow periodic check of self.running
                event_id = self.image_port.listener.timed_wait_one(
                    iceoryx2.Duration.from_millis(500)
                )
                if event_id == self.image_port.event:
                    sample = self.image_port.subscriber.receive()
                    if sample is not None:
                        data = sample.payload()
                        # Process image data here if needed
                        del data, sample

        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError):
            pass

        finally:
            self.logger.info(f"{NODE_NAME} shut down")


def main():
    node = ProcessorNode()
    node.run()


if __name__ == "__main__":
    main()
