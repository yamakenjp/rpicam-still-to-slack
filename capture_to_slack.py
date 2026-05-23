#!/usr/bin/env python3
"""Capture with rpicam-still and upload to Slack."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

BASE_DIR = Path(__file__).resolve().parent
SECRET_OPTION_NAMES = {"SLACK_TOKEN", "SLACK_BOT_TOKEN"}
TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}


def read_option_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        values[key.strip()] = value
    return values


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def pick(options: dict[str, str], *names: str, default: str | None = None) -> str:
    for name in names:
        value = options.get(name)
        if value:
            return value
    if default is not None:
        return default
    raise RuntimeError(f"missing option: {', '.join(names)}")


def num(options: dict[str, str], name: str, default: float) -> float:
    return float(options.get(name, default))


def safe_options(options: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in sorted(options.items()):
        safe[key] = "********" if key in SECRET_OPTION_NAMES else value
    return safe


def run(command: list[str], dry_run: bool) -> None:
    logging.info("%s", shlex.join(command))
    if dry_run:
        return
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.stdout:
        logging.debug(result.stdout.strip())
    if result.stderr:
        logging.debug(result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {shlex.join(command)}")


def load_metadata(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("failed to parse metadata: %s", path)
        return {}


def metadata_number(metadata: dict, *keys: str) -> float | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                return float(value)
    return None


def classify(metadata: dict, options: dict[str, str]) -> str:
    exposure = metadata_number(metadata, "ExposureTime", "SensorExposureTime", "FrameDuration")
    gain = metadata_number(metadata, "AnalogueGain", "DigitalGain")
    logging.info("metadata exposure=%s gain=%s", exposure, gain)

    if exposure is None and gain is None:
        return "twilight"
    if exposure is not None and exposure <= num(options, "EXPOSURE_DAY_MAX_US", 2000):
        if gain is None or gain <= num(options, "GAIN_DAY_MAX", 2.0):
            return "day"
    if exposure is not None and exposure >= num(options, "EXPOSURE_NIGHT_MIN_US", 30000):
        return "night"
    if gain is not None and gain >= num(options, "GAIN_NIGHT_MIN", 8.0):
        return "night"
    return "twilight"


def base_args(options: dict[str, str]) -> list[str]:
    return [
        pick(options, "RPICAM_STILL", default="rpicam-still"),
        "--nopreview",
        "--hdr", pick(options, "HDR_MODE", default="auto"),
        "--autofocus-mode", pick(options, "AUTOFOCUS_MODE", default="continuous"),
        "--metering", pick(options, "METERING", default="average"),
    ]


def profile_args(profile: str, options: dict[str, str]) -> list[str]:
    if profile == "day":
        return ["--ev", pick(options, "DAY_EV", default="-0.3"), "--exposure", pick(options, "DAY_EXPOSURE", default="normal"), "--denoise", pick(options, "DAY_DENOISE", default="cdn_fast")]
    if profile == "night":
        args = ["--ev", pick(options, "NIGHT_EV", default="0.7"), "--exposure", pick(options, "NIGHT_EXPOSURE", default="long"), "--denoise", pick(options, "NIGHT_DENOISE", default="cdn_hq")]
        shutter = int(num(options, "NIGHT_SHUTTER_US", 0))
        if shutter > 0:
            args += ["--shutter", str(shutter)]
        return args
    return ["--ev", pick(options, "TWILIGHT_EV", default="0"), "--exposure", pick(options, "TWILIGHT_EXPOSURE", default="normal"), "--denoise", pick(options, "TWILIGHT_DENOISE", default="cdn_fast")]


def capture_preview(options: dict[str, str], preview: Path, metadata: Path, dry_run: bool) -> None:
    for path in (preview, metadata):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    command = base_args(options) + [
        "--timeout", pick(options, "PREVIEW_TIMEOUT_MS", default="2500"),
        "--width", pick(options, "PREVIEW_WIDTH", default="1280"),
        "--height", pick(options, "PREVIEW_HEIGHT", default="720"),
        "--quality", "75",
        "--metadata", str(metadata),
        "--metadata-format", "json",
        "--output", str(preview),
    ]
    run(command, dry_run)


def capture_final(options: dict[str, str], output: Path, profile: str, dry_run: bool) -> None:
    with contextlib.suppress(FileNotFoundError):
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = base_args(options) + profile_args(profile, options) + [
        "--timeout", pick(options, "TIMEOUT_MS", default="3000"),
        "--width", pick(options, "WIDTH", default="2304"),
        "--height", pick(options, "HEIGHT", default="1296"),
        "--quality", pick(options, "QUALITY", default="92"),
        "--output", str(output),
    ]
    run(command, dry_run)


def upload(options: dict[str, str], output: Path, profile: str, dry_run: bool, no_upload: bool) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    comment = pick(options, "COMMENT_TEMPLATE", default="Photo taken at {timestamp}! profile={profile}").format(timestamp=timestamp, profile=profile)
    logging.info("Slack comment: %s", comment)
    if dry_run or no_upload:
        logging.info("Slack upload skipped")
        return

    token = pick(options, "SLACK_BOT_TOKEN", "SLACK_TOKEN")
    channel = pick(options, "SLACK_CHANNEL_ID", "CHANNEL")
    if not output.exists():
        raise RuntimeError(f"output image not found: {output}")
    try:
        WebClient(token=token).files_upload_v2(channel=channel, file=str(output), filename=output.name, title=output.name, initial_comment=comment)
    except SlackApiError as exc:
        raise RuntimeError(f"Slack upload failed: {exc.response.get('error')}") from exc


@contextlib.contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another capture process is already running") from exc
        fp.write(str(os.getpid()))
        fp.flush()
        yield


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slack-option", type=Path, default=BASE_DIR / ".slack_option")
    parser.add_argument("--camera-option", type=Path, default=BASE_DIR / ".camera_option")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--keep-preview", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    try:
        options = read_option_file(args.slack_option) | read_option_file(args.camera_option)
        debug = args.debug or truthy(options.get("DEBUG")) or truthy(options.get("DEBUG_MODE"))
        no_upload = args.no_upload or truthy(options.get("NO_UPLOAD")) or truthy(options.get("DEBUG_NO_UPLOAD"))
        log_level = "DEBUG" if debug else args.log_level.upper()

        logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
        if debug:
            logging.debug("debug mode enabled")
            logging.debug("options: %s", json.dumps(safe_options(options), ensure_ascii=False, sort_keys=True))

        output = Path(pick(options, "OUTPUT_PATH", default="/tmp/image.jpg"))
        preview = Path(pick(options, "PREVIEW_PATH", default="/tmp/rpicam-still-to-slack-preview.jpg"))
        metadata_path = Path(pick(options, "METADATA_PATH", default="/tmp/rpicam-still-to-slack-preview.json"))
        lock_path = Path(pick(options, "LOCK_PATH", default="/tmp/rpicam-still-to-slack.lock"))

        with lock(lock_path):
            capture_preview(options, preview, metadata_path, args.dry_run)
            metadata = load_metadata(metadata_path)
            if debug:
                logging.debug("metadata file: %s", metadata_path)
                logging.debug("metadata: %s", json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))
            profile = classify(metadata, options)
            logging.info("selected profile: %s", profile)
            capture_final(options, output, profile, args.dry_run)
            upload(options, output, profile, args.dry_run, no_upload)
            if not (args.keep_preview or debug):
                for path in (preview, metadata_path):
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
            elif debug:
                logging.debug("preview kept: %s", preview)
                logging.debug("metadata kept: %s", metadata_path)
        return 0
    except Exception as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
