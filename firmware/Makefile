# ==== Config ====

DOCKER_IMAGE := bitcraze/aideck
DOCKER_WORKDIR := /module/ai

URI ?= radio://0/80/2M/E7E7E7E7E7
IMG := ai/BUILD/GAP8_V2/GCC_RISCV_FREERTOS/target.board.devices.flash.img

JOBS ?= $(shell nproc)

OPENOCD_CABLE ?= interface/ftdi/olimex-arm-usb-ocd-h.cfg

# ==== Targets ====

.PHONY: help build rebuild flash flash-jtag clean shell

help:
	@echo "Targets:"
	@echo "  make build        → Build firmware (Docker)"
	@echo "  make rebuild      → Clean + build"
	@echo "  make flash        → Flash via radio (cfloader)"
	@echo "  make flash-jtag   → Flash via JTAG (OpenOCD)"
	@echo "  make clean        → Clean build"
	@echo "  make shell        → Docker shell"

# =========================================================
# BUILD (DOCKER ONLY)
# =========================================================

build:
	@echo ">>> Building AI-deck firmware ($(JOBS) jobs)"
	@docker run --rm \
		-v $(CURDIR):/module \
		-w $(DOCKER_WORKDIR) \
		$(DOCKER_IMAGE) \
		bash -c "source /gap_sdk/configs/ai_deck.sh && make -j$(JOBS) build image"

	@if [ ! -f "$(IMG)" ]; then \
		echo "❌ Build failed: $(IMG) not found"; \
		exit 1; \
	fi

	@echo "✅ Build OK → $(IMG)"

rebuild:
	@echo ">>> Full rebuild"
	@docker run --rm \
		-v $(CURDIR):/module \
		-w $(DOCKER_WORKDIR) \
		$(DOCKER_IMAGE) \
		bash -c "source /gap_sdk/configs/ai_deck.sh && make clean && make -j$(JOBS) build image"

# =========================================================
# FLASH (RADIO)
# =========================================================

flash:
	@if [ ! -f "$(IMG)" ]; then \
		echo "❌ No image. Run make build first."; \
		exit 1; \
	fi

	@echo ">>> Flashing via radio"
	cfloader flash $(IMG) deck-bcAI:gap8-fw -w $(URI)

# =========================================================
# FLASH (JTAG - DIRECT OPENOCD, NO SDK MAKE FLASH)
# =========================================================

flash-jtag:
	@if [ ! -f "$(IMG)" ]; then \
		echo "❌ No image. Run make build first."; \
		exit 1; \
	fi

	@echo ">>> Flashing via JTAG (OpenOCD)"

	openocd \
		-f $(OPENOCD_CABLE) \
		-f target/gap8revb.tcl \
		-c "init; reset halt; program $(IMG) verify reset exit"

# =========================================================
# CLEAN
# =========================================================

clean:
	@docker run --rm \
		-v $(CURDIR):/module \
		-w $(DOCKER_WORKDIR) \
		$(DOCKER_IMAGE) \
		bash -c "source /gap_sdk/configs/ai_deck.sh && make clean"

# =========================================================
# SHELL
# =========================================================

shell:
	docker run --rm -it \
		-v $(CURDIR):/module \
		-w $(DOCKER_WORKDIR) \
		$(DOCKER_IMAGE) \
		bash