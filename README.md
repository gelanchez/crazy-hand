# crazyflie-tfm

## ARCHITECTURE

| Publisher      | Topic         | Subscriber(s)                             |
| -------------- | ------------- | ----------------------------------------- |
| `control_node` | `/action`     | `logger_node`, `wifi_node`                |
| `gui_node`     | `/command`    | `control_node`                            |
| `vision_node`  | `/perception` | `control_node`, `gui_node`, `logger_node` |
| `wifi_node`    | `/telemetry`  | `control_node`, `gui_node`, `logger_node` |
| `wifi_node`    | `/image`      | `gui_node`, `logger_node`, `vision_node`  |

## INSTALLATION

### Dependencies

- [Python 3](https://www.python.org/downloads/)
- [Docker](https://docs.docker.com/engine/install/ubuntu/)
- [Grafana](https://grafana.com/oss/grafana/)
- [QuestDB](https://questdb.com/docs/getting-started/quick-start/)
- Build tools (make, gcc, etc).

### Python environment

Create virtual enviroment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

### Mediapipe

Download [HandGestureClassifier](https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task) and place it in the `data` folder.

### Grafana

Follow steps in [Install Grafana](https://grafana.com/docs/grafana/latest/setup-grafana/installation/debian/).

To [start Grafana](https://grafana.com/docs/grafana/latest/setup-grafana/start-restart-grafana/), execute the following statements to configure Grafana to start automatically using systemd:

```bash
sudo /bin/systemctl daemon-reload
sudo /bin/systemctl enable grafana-server
```

Start grafana-server by executing:

```bash
sudo /bin/systemctl start grafana-server
```

Open [Grafana server](https://grafana.com/docs/grafana/latest/setup-grafana/sign-in-to-grafana/).

Grafana UI:
<http://localhost:3000>

### QuestDB

[QuestDB](https://questdb.com/docs/getting-started/quick-start/) is an open source time-series database engineered for low latency.

Start QuestDB:

```bash
# Local folder installation
cd path_to_QuestDB/bin
# cd ~/apps/questdb-9.3.5-rt-linux-x86-64/bin
./questdb.sh start

# Docker image:
docker run -p 9000:9000 -p 8812:8812 -p 9003:9003 questdb/questdb:9.3.5
```

QuestDB UI:
<http://localhost:9000>

## COMPILE FIRMWARE AND FLASH

### app - STM32 firmware

This firmware depends on the Crazyflie firmware repository. First clone it:

```bash
git clone https://github.com/bitcraze/crazyflie-firmware.git ~/git/PUBLIC/crazyflie-firmware
```

Compile app firmware:

```bash
cd app
make clean
make -j

# Override default CRAZYFLIE_BASE location
make CRAZYFLIE_BASE=/path/to/repo
```

Flash app firmware using Crazyradio:

1. Turn the Crazyflie off.
2. Hold the power button ~3 seconds to enter bootloader mode (blue LEDs blink).
3. Run:

```bash
make cload
```

### ai - AI-deck GAP8 firmware

Pull the Docker image:

```bash
docker pull bitcraze/aideck
```

Build the AI-deck firmware (inside Docker):

```bash
cd firmware

# incremental build
make build

# full rebuild
make rebuild

# build with debugging enabled
make build DEBUG=1

# full rebuild with debugging
make rebuild DEBUG=1
```

Output image:

```text
ai/BUILD/GAP8_V2/GCC_RISCV_FREERTOS/target.board.devices.flash.img
```

#### Flashing via Radio

We support two modes of flashing via the Crazyradio PA dongle:

1. **Warm Boot (`make flash`) [Default]**:
   Use this if the Crazyflie is already running its normal firmware. It will connect to the firmware, automatically reboot the drone into bootloader mode, flash the AI-deck, and restart it.

   ```bash
   make flash

   # Override radio URI (default is radio://0/80/2M/E7E7E7E7E7)
   make flash URI=radio://0/80/2M/E7E7E7E7E7
   ```

2. **Cold Boot (`make flash-cold`)**:
   Use this if the Crazyflie is already in bootloader mode (e.g. blue M2 LED blinking, listening on bootloader channel `0`) or if a previous flashing attempt was interrupted.

   ```bash
   make flash-cold
   ```

#### Flashing via JTAG

To flash directly using a JTAG cable (e.g. Olimex ARM-USB-OCD-H):

```bash
make flash-jtag
```

#### Clean Build Artifacts

```bash
make clean
```

## RUN CLIENT

```bash
python -m client.main
python -m client.main --sim
python -m client.main --help
```

## TROUBLESHOOTING

### Clean Iceoryx2 memory

```bash
rm -rf /tmp/iceoryx2/*
rm -rf /dev/shm/iox2_*
```

### Crazyradio USB "Access denied (insufficient permissions)" Error

If you get `Failed to flash: [Errno 13] Access denied (insufficient permissions)` when trying to run `cfloader` or flash:

1. Ensure the Bitcraze `udev` rules are installed in `/etc/udev/rules.d/99-bitcraze.rules`:

   ```bash
   # Crazyradio (normal operation)
   SUBSYSTEM=="usb", ATTRS{idVendor}=="1915", ATTRS{idProduct}=="7777", MODE="0664", GROUP="plugdev"
   # Bootloader
   SUBSYSTEM=="usb", ATTRS{idVendor}=="1915", ATTRS{idProduct}=="0101", MODE="0664", GROUP="plugdev"
   ```

2. Make sure your user is in the `plugdev` group:

   ```bash
   sudo usermod -aG plugdev $USER
   ```

3. **CRITICAL**: Unplug the Crazyradio dongle from the USB port and plug it back in so that the permissions are applied to the active device node.

## LINKS

- <https://ai.google.dev/edge/mediapipe/solutions/setup_python>
- <https://ai.google.dev/edge/mediapipe/solutions/vision/gesture_recognizer>
