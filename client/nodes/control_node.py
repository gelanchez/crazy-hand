import time
from client.common.node import Node
import logging

class ControlNode(Node):
    def run(self):
        self.logger.info(f"{self.name} running")
        try:
            while self.running:
                time.sleep(0.1)
        finally:
            self.logger.info(f"{self.name} shut down")

def main():
    node = ControlNode("control_node", logging.DEBUG)
    node.run()


if __name__ == "__main__":
    main()
