import logging
import iceoryx2
from client.common.payloads import ImageData
from client.common.constants import ServiceName, EventId, IMAGE_WIDTH, IMAGE_HEIGHT
from client.common.node import Node
from PIL import Image
from pathlib import Path

NODE_NAME = "logger_node"
IMAGES_PATH = Path("./data/images")

class LoggerNode(Node):
    def __init__(self, level=logging.INFO):
        super().__init__(NODE_NAME, level=level)

    def run(self):
        self.image_port = self.create_subscriber(ServiceName.IMAGE, ImageData, EventId.IMAGE_READY)
        
        if self.image_port.subscriber is None:
            return

        try:
            IMAGES_PATH.mkdir(parents=True, exist_ok=True)

            while self.running:
                event_id = self.image_port.listener.timed_wait_one(
                    iceoryx2.Duration.from_millis(500)
                )

                save_images = False
                
                if event_id == self.image_port.event and save_images:
                    sample = self.image_port.subscriber.receive()
                    if sample is not None:
                        data = sample.payload()
                        image = Image.frombuffer(
                            "L",  # mode (grayscale)
                            (IMAGE_WIDTH, IMAGE_HEIGHT),  # size
                            data.contents.pixels,  # buffer (no copy)
                            "raw",  # decoder
                            "L",  # raw mode
                            0,  # stride
                            1,  # orientation
                        )
                        image_name = f"{data.contents.timestamp}.png"
                        image.save(IMAGES_PATH / image_name)
                        self.logger.debug(f"Saved {data.contents}")
                        del data, sample

        except (iceoryx2.NodeWaitFailure, iceoryx2.ListenerWaitError):
            pass

        finally:
            self.logger.info(f"{NODE_NAME} shut down")


def main():
    node = LoggerNode()
    node.run()


if __name__ == "__main__":
    main()
