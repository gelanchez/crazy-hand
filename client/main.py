import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from client.common.database import Database
from client.common.utils import cleanup_iceoryx2, setup_logging

logger = setup_logging("main")

# Nodes that accept the --sim flag
_SIM_NODES = {"wifi_node", "gui_node"}


@click.command()
@click.option("--sim", is_flag=True, help="Run in simulation mode")
def main(sim):
    logger.info("Starting Crazyflie client")

    venv_python = Path(__file__).parent.parent / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    Database.start_questdb()
    Database.cleanup_old_data(24 * 30)  # 30 days

    cleanup_iceoryx2()

    nodes = [
        "wifi_node",
        "control_node",
        "vision_node",
        "logger_node",
        "gui_node",
    ]

    processes = []
    crashed = False

    try:
        for node in nodes:
            logger.info(f"Launching {node}...")
            args = [str(venv_python), "-m", f"client.nodes.{node}"]
            if sim and node in _SIM_NODES:
                args.append("--sim")

            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            process = subprocess.Popen(args, preexec_fn=os.setsid, env=env)
            processes.append((node, process))
            time.sleep(0.1)

        logger.info("All nodes started. Press Ctrl+C to terminate.")

        while True:
            for node, process in processes:
                if process.poll() is not None:
                    if process.returncode in (0, -signal.SIGINT, -signal.SIGTERM):
                        logger.info(f"Node '{node}' finished.")
                    else:
                        logger.error(f"Node '{node}' terminated unexpectedly with code {process.returncode}")
                        crashed = True
                    break
            else:
                time.sleep(0.5)
                continue
            break

    except KeyboardInterrupt:
        logger.info("Ctrl+C received, terminating all nodes...")

    if crashed:
        logger.info("Node crash detected, terminating all nodes...")

    for node, process in processes:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
        except Exception:
            pass

    for node, process in processes:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.warning(f"Node '{node}' did not terminate in time, killing...")
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
