#!/bin/sh
set -eu

JPEG_FILE=${1:-/tmp/image.jpg}
METADATA_JSON=${2:-/tmp/rpicam-still-to-slack-final.json}
PROFILE=${3:-unknown}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 "$SCRIPT_DIR/embed_metadata.py" "$JPEG_FILE" "$METADATA_JSON" --profile "$PROFILE"
