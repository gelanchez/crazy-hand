import glob
import os
import signal
import subprocess
import sys
import shutil
import time

from pathlib import Path

import click

from client.common.database import Database
from client.common.utils import setup_logging

logger = setup_logging("main")


def cleanup_iceoryx2():
    """
    Cleans stale Iceoryx2 shared memory and temp files.
    Safe to run at startup when no nodes are running.
    """

    # /dev/shm (shared memory)
    for path in glob.glob("/dev/shm/iox2_*"):
        try:
            os.remove(path)
            logger.info(f"Removed shared memory: {path}")
        except Exception as e:
            logger.debug(f"Could not remove {path}: {e}")

    # /tmp/iceoryx2 (temp files)
    tmp_dir = Path("/tmp/iceoryx2")
    if tmp_dir.exists():
        try:
            shutil.rmtree(tmp_dir)
            logger.info("Removed /tmp/iceoryx2")
        except Exception as e:
            logger.debug(f"Could not remove /tmp/iceoryx2: {e}")


@click.option("--sim", is_flag=True, help="Run in simulation mode")
@click.command()
def main(sim):
    logger.info("Starting Crazyflie client")

    # Find the python executable in the venv
    venv_python = os.path.join(os.getcwd(), ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    Database.start_questdb()
    Database.cleanup_old_data(24 * 7 * 30)  # 30 days

    cleanup_iceoryx2()

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
