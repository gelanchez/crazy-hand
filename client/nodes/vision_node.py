import logging
import numpy as np
import iceoryx2
from client.common.payloads import ImageData
from client.common.constants import ServiceName, EventId
from client.common.node import Node
from client.common.blackboards import CONFIG


NODE_NAME = "vision_node"

class VisionNode(Node):
    def __init__(self, level=logging.DEBUG):
        super().__init__(NODE_NAME, level=level)

    def run(self):
        self.image_port = self.create_subscriber(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY)
        
        if self.image_port.subscriber is None:
            return

        self.blackboard_reader = self.create_blackboard_reader("/config", CONFIG)
        if self.blackboard_reader is None:
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
                        if self.blackboard_read(self.blackboard_reader, "process_images"):
                            data = sample.payload()
                            pixels = np.ctypeslib.as_array(data.contents.pixels).copy()
                            del data
                            # TODO: Process image
                        del sample

        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError):
            pass

        finally:
            self.logger.info(f"{NODE_NAME} shut down")


def main():
    node = VisionNode()
    node.run()


if __name__ == "__main__":
    main()
