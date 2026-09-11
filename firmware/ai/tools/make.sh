#!/usr/bin/env bash
#
# make.sh - Docker wrapper for the AI-deck GAP SDK build system.
#
# This script mounts the repository root into a bitcraze/aideck container
# and executes the local 'tools/make' script.
#
# Usage: ./make.sh [target] [make_args...]
# Common targets: build, all, clean
#
set -e

# Resolve repo root (ai/)
scriptDir=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
repoRoot=$( realpath "${scriptDir}/.." )

docker run -it --rm \
  -v "${repoRoot}":/module \
  --privileged \
  bitcraze/aideck \
  /module/tools/make . "$@"

