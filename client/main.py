

import click
import os
import sys
import subprocess
import time
import signal

from common.utils import setup_logging

logger = setup_logging("main")


@click.option("--sim", is_flag=True, help="Run in simulation mode")
@click.command()
def main(sim):
    logger.info("Starting Crazyflie client")

    # Find the python executable in the venv
    venv_python = os.path.join(os.getcwd(), ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    processes = []
    nodes = [
        "nodes.wifi_node",
        "nodes.processor_node",
        "nodes.logger_node",
        "nodes.gui_node",
    ]

    try:
        # Start nodes as separate processes
        # Using start_new_session=True to handle signals correctly
        for node in nodes:
            logger.info(f"Launching {node}...")
            args = [venv_python, "-m", node]
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
                    logger.error(
                        f"Node {process.args} terminated unexpectedly with code {process.returncode}"
                    )
                    raise KeyboardInterrupt
            time.sleep(1)

    except KeyboardInterrupt:
        try:
            logger.info("Terminating all nodes...")
        except BrokenPipeError:
            pass

        for process in processes:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except Exception:
                pass

        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    pass

        try:
            logger.info("Client shutdown complete.")
        except BrokenPipeError:
            pass
        finally:
            # Prevent "Exception ignored in: <_io.TextIOWrapper ...>" on interpreter shutdown
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                os.dup2(devnull, sys.stderr.fileno())
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass

