#!/usr/bin/env python3
"""Embed selected rpicam metadata into a JPEG file."""

from __future__ import annotations

import argparse
import json
import logging
import math
import shlex
import subprocess
from pathlib import Path
from typing import Any


def metadata_number(metadata: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float | str):
            try:
                number = float(value)
                if math.isfinite(number):
                    return number
            except (TypeError, ValueError):
                pass
    return None


def metadata_total_gain(metadata: dict[str, Any]) -> float | None:
    analogue = metadata_number(metadata, "AnalogueGain")
    digital = metadata_number(metadata, "DigitalGain")
    gains = [gain for gain in (analogue, digital) if gain is not None and gain > 0]
    return math.prod(gains) if gains else None


def run(command: list[str], timeout_s: float) -> None:
    logging.info("%s", shlex.join(command))
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout_s:g}s: {shlex.join(command)}"
        ) from exc
    if result.stdout:
        logging.debug("%s", result.stdout.strip())
    if result.stderr:
        logging.debug("%s", result.stderr.strip())
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: "
            f"{shlex.join(command)}{suffix}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jpeg", type=Path)
    parser.add_argument("metadata_json", type=Path)
    parser.add_argument("--profile", default="unknown")
    parser.add_argument("--exiftool", default="exiftool")
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
            raise RuntimeError("--timeout-seconds must be a positive finite number")
        raw_metadata = json.loads(args.metadata_json.read_text(encoding="utf-8"))
        if not isinstance(raw_metadata, dict):
            raise RuntimeError("metadata root must be a JSON object")
        metadata: dict[str, Any] = raw_metadata

        exposure_us = metadata_number(metadata, "ExposureTime", "SensorExposureTime")
        analogue_gain = metadata_number(metadata, "AnalogueGain")
        digital_gain = metadata_number(metadata, "DigitalGain")
        total_gain = metadata_total_gain(metadata)
        lux = metadata_number(metadata, "Lux")
        lens_position = metadata_number(metadata, "LensPosition")
        focus_fom = metadata_number(metadata, "FocusFoM")

        payload = {
            "source": "rpicam-still-to-slack",
            "profile": args.profile,
            "ExposureTime": exposure_us,
            "AnalogueGain": analogue_gain,
            "DigitalGain": digital_gain,
            "TotalGain": total_gain,
            "Lux": lux,
            "LensPosition": lens_position,
            "FocusFoM": focus_fom,
        }
        comment = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        description = (
            f"rpicam-still-to-slack profile={args.profile} "
            f"exposure_us={exposure_us} analogue_gain={analogue_gain} "
            f"digital_gain={digital_gain} total_gain={total_gain} lux={lux}"
        )

        command = [
            args.exiftool,
            "-overwrite_original",
            "-EXIF:Software=rpicam-still-to-slack",
            f"-EXIF:ImageDescription={description}",
            f"-EXIF:UserComment={comment}",
        ]
        if exposure_us and exposure_us > 0:
            command.append(f"-EXIF:ExposureTime={exposure_us / 1_000_000:.6f}")
        if total_gain and total_gain > 0:
            command.append(f"-EXIF:ISO={round(total_gain * 100)}")
        command.append(str(args.jpeg))
        run(command, args.timeout_seconds)
        return 0
    except Exception as exc:
        if args.required:
            logging.error("%s", exc)
            return 1
        logging.warning("failed to embed metadata: %s", exc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
