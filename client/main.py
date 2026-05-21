import os
import signal
import subprocess
import sys
import time

import click

from client.common.database import Database
from client.common.utils import setup_logging

logger = setup_logging("main")


@click.option("--sim", is_flag=True, help="Run in simulation mode")
@click.command()
def main(sim):
    logger.info("Starting Crazyflie client")

    # Find the python executable in the venv
    venv_python = os.path.join(os.getcwd(), ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    Database.start_questdb()
    # Database.drop_old_partitions()

    processes = []
    nodes = [
        "wifi_node",
        "control_node",
        "vision_node",
        "logger_node",
        "gui_node",
    ]

    try:
        # Start nodes as separate processes
        # Using start_new_session=True to handle signals correctly
        for node in nodes:
            logger.info(f"Launching {node}...")
            args = [venv_python, "-m", f"client.nodes.{node}"]
            if sim:
                args.append("--sim")

            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            process = subprocess.Popen(args, preexec_fn=os.setsid, env=env)
            processes.append(process)
            time.sleep(0.1)

        logger.info("All nodes started. Press Ctrl+C to terminate.")

        while True:
            for process in processes:
                if process.poll() is not None:
                    node_cmd = " ".join(process.args)
                    if process.returncode in (0, -signal.SIGINT, -signal.SIGTERM):
                        logger.info(f"Node '{node_cmd}' finished.")
                    else:
                        logger.error(
                            f"Node '{node_cmd}' terminated unexpectedly with code {process.returncode}"
                        )
                    raise KeyboardInterrupt
            time.sleep(0.5)

    except KeyboardInterrupt:
        logger.info("Terminating all nodes...")

        # Send SIGINT to all process groups
        for process in processes:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except Exception:
                pass

        # Wait for processes to exit gracefully
        for process in processes:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning(
                    f"Node {process.args} did not terminate in time, killing..."
                )
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    pass

        logger.info("Client shutdown complete.")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
