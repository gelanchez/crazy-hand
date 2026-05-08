# crazyflie-tfm

## INSTALLATION

### Dependencies

- [Docker](https://docs.docker.com/engine/install/ubuntu/)
- [Python 3](https://www.python.org/downloads/)
- Build tools (make, gcc, etc).

### Python environment

Create virtual enviroment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

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
```

Output image:

```text
ai/BUILD/GAP8_V2/GCC_RISCV_FREERTOS/target.board.devices.flash.img
```

Flash via radio:

```bash
make flash

# Override radio URI
make flash URI=radio://0/80/2M/E7E7E7E7E7

# Flash via JTAG
make flash-jtag
```

Clean:

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

Clean Iceoryx2 memory:

```bash
rm -rf /tmp/iceoryx2/*
rm -rf /dev/shm/iox2_*
```

## LINKS
