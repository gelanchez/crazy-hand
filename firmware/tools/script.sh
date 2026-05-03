python ai/train.py

docker run --rm -v ${PWD}:/module aideck-with-autotiler tools/make.sh ai clean model build image
docker run --rm -v ${PWD}:/module aideck-with-autotiler tools/make.sh ai image

cfloader flash ai/model/BUILD/GAP8_V2/GCC_RISCV_FREERTOS/target.board.devices.flash.img deck-bcAI:gap8-fw -w radio://0/40/2M/E7E7E7E703