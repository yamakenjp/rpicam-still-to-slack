#!/usr/bin/env python3
"""Embed selected rpicam metadata into a JPEG file."""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import subprocess
from pathlib import Path


def metadata_number(metadata: dict, *keys: str) -> float | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    return None


def run(command: list[str]) -> None:
    logging.info("%s", shlex.join(command))
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.stdout:
        logging.debug(result.stdout.strip())
    if result.stderr:
        logging.debug(result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {shlex.join(command)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jpeg", type=Path)
    parser.add_argument("metadata_json", type=Path)
    parser.add_argument("--profile", default="unknown")
    parser.add_argument("--exiftool", default="exiftool")
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")

    try:
        metadata = json.loads(args.metadata_json.read_text(encoding="utf-8"))
        exposure_us = metadata_number(metadata, "ExposureTime", "SensorExposureTime")
        analogue_gain = metadata_number(metadata, "AnalogueGain")
        digital_gain = metadata_number(metadata, "DigitalGain")
        lux = metadata_number(metadata, "Lux")
        lens_position = metadata_number(metadata, "LensPosition")
        focus_fom = metadata_number(metadata, "FocusFoM")

        payload = {
            "source": "rpicam-still-to-slack",
            "profile": args.profile,
            "ExposureTime": exposure_us,
            "AnalogueGain": analogue_gain,
            "DigitalGain": digital_gain,
            "Lux": lux,
            "LensPosition": lens_position,
            "FocusFoM": focus_fom,
        }
        comment = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        description = f"rpicam-still-to-slack profile={args.profile} exposure_us={exposure_us} analogue_gain={analogue_gain} digital_gain={digital_gain} lux={lux}"

        command = [
            args.exiftool,
            "-overwrite_original",
            "-EXIF:Software=rpicam-still-to-slack",
            f"-EXIF:ImageDescription={description}",
            f"-EXIF:UserComment={comment}",
        ]
        if exposure_us and exposure_us > 0:
            command.append(f"-EXIF:ExposureTime={exposure_us / 1000000:.6f}")
        if analogue_gain and analogue_gain > 0:
            command.append(f"-EXIF:ISO={round(analogue_gain * 100)}")
        command.append(str(args.jpeg))
        run(command)
        return 0
    except Exception as exc:
        if args.required:
            logging.error("%s", exc)
            return 1
        logging.warning("failed to embed metadata: %s", exc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
