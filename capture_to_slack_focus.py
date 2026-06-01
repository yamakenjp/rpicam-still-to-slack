#!/usr/bin/env python3
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
TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
SECRET_OPTION_NAMES = {"SLACK_TOKEN", "SLACK_BOT_TOKEN"}


def read_option_file(path: Path) -> dict[str, str]:
    values = {}
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
        values[key.strip()] = value.strip().strip("'\"")
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


def opt_float(options: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = options.get(name)
        if value not in (None, ""):
            return float(value)
    return None


def num(options: dict[str, str], name: str, default: float) -> float:
    return float(options.get(name, default))


def safe_options(options: dict[str, str]) -> dict[str, str]:
    return {k: ("********" if k in SECRET_OPTION_NAMES else v) for k, v in sorted(options.items())}


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


def log_metadata(label: str, path: Path, metadata: dict, debug: bool) -> None:
    exposure = metadata_number(metadata, "ExposureTime", "SensorExposureTime", "FrameDuration")
    analogue_gain = metadata_number(metadata, "AnalogueGain")
    digital_gain = metadata_number(metadata, "DigitalGain")
    lux = metadata_number(metadata, "Lux")
    lens_position = metadata_number(metadata, "LensPosition")
    focus_fom = metadata_number(metadata, "FocusFoM")
    logging.info(
        "%s metadata exposure=%s analogue_gain=%s digital_gain=%s lux=%s lens_position=%s focus_fom=%s",
        label, exposure, analogue_gain, digital_gain, lux, lens_position, focus_fom,
    )
    if debug:
        logging.debug("%s metadata file: %s", label, path)
        logging.debug("%s metadata: %s", label, json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))


def configured_lens_position(options: dict[str, str]) -> float | None:
    return opt_float(options, "LENS_POSITION", "lens-position", "LENS-POSITION")


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


def base_args(options: dict[str, str], lens_position: float | None = None) -> list[str]:
    args = [pick(options, "RPICAM_STILL", default="rpicam-still"), "--nopreview", "--hdr", pick(options, "HDR_MODE", default="auto")]
    if lens_position is None:
        args += ["--autofocus-mode", pick(options, "AUTOFOCUS_MODE", default="continuous")]
    else:
        args += ["--autofocus-mode", "manual", "--lens-position", f"{lens_position:.6f}"]
    args += ["--metering", pick(options, "METERING", default="average")]
    return args


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


def capture_still(options: dict[str, str], output: Path, metadata: Path, width: str, height: str, quality: str, timeout: str, dry_run: bool, lens_position: float | None = None, extra_args: list[str] | None = None) -> None:
    for path in (output, metadata):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata.parent.mkdir(parents=True, exist_ok=True)
    command = base_args(options, lens_position)
    if extra_args:
        command += extra_args
    command += ["--timeout", timeout, "--width", width, "--height", height, "--quality", quality, "--metadata", str(metadata), "--metadata-format", "json", "--output", str(output)]
    run(command, dry_run)


def capture_preview_once(options: dict[str, str], preview: Path, metadata: Path, dry_run: bool, lens_position: float | None = None) -> dict:
    capture_still(
        options, preview, metadata,
        pick(options, "PREVIEW_WIDTH", default="1280"), pick(options, "PREVIEW_HEIGHT", default="720"), "75", pick(options, "PREVIEW_TIMEOUT_MS", default="2500"),
        dry_run, lens_position=lens_position,
    )
    return load_metadata(metadata)


def refine_focus_with_precaptures(options: dict[str, str], preview: Path, metadata: Path, dry_run: bool, debug: bool) -> tuple[float | None, dict]:
    count = int(num(options, "FOCUS_PRECAPTURE_COUNT", 3))
    if count <= 1:
        meta = capture_preview_once(options, preview, metadata, dry_run)
        return metadata_number(meta, "LensPosition"), meta

    best_meta = {}
    best_score = -1.0
    best_lens = None
    for index in range(1, count + 1):
        sample_preview = preview.with_name(f"{preview.stem}-focus-{index:02d}{preview.suffix}")
        sample_metadata = metadata.with_name(f"{metadata.stem}-focus-{index:02d}{metadata.suffix}")
        meta = capture_preview_once(options, sample_preview, sample_metadata, dry_run)
        log_metadata(f"focus pre-capture {index}", sample_metadata, meta, debug)
        score = metadata_number(meta, "FocusFoM") or -1.0
        lens = metadata_number(meta, "LensPosition")
        if lens is not None and score > best_score:
            best_score = score
            best_lens = lens
            best_meta = meta
    if best_lens is None:
        logging.warning("no usable LensPosition found during focus pre-captures")
        meta = capture_preview_once(options, preview, metadata, dry_run)
        return metadata_number(meta, "LensPosition"), meta
    logging.info("selected pre-capture lens_position=%s focus_fom=%s", best_lens, best_score)
    return best_lens, best_meta


def capture_final(options: dict[str, str], output: Path, metadata: Path, profile: str, lens_position: float | None, dry_run: bool) -> None:
    capture_still(
        options, output, metadata,
        pick(options, "WIDTH", default="2304"), pick(options, "HEIGHT", default="1296"), pick(options, "QUALITY", default="92"), pick(options, "TIMEOUT_MS", default="3000"),
        dry_run, lens_position=lens_position, extra_args=profile_args(profile, options),
    )


def focus_scan_values(options: dict[str, str]) -> list[float]:
    raw_values = pick(options, "FOCUS_SCAN_VALUES", default="")
    if raw_values:
        return [float(item.strip()) for item in raw_values.split(",") if item.strip()]
    start = num(options, "FOCUS_SCAN_START", 0.0)
    end = num(options, "FOCUS_SCAN_END", 8.0)
    step = num(options, "FOCUS_SCAN_STEP", 0.5)
    values = []
    current = start
    while current <= end + (step / 10):
        values.append(round(current, 6))
        current += step
    return values


def focus_calibration(options: dict[str, str], dry_run: bool, debug: bool) -> int:
    out_dir = Path(pick(options, "FOCUS_CALIBRATION_DIR", default="/tmp/rpicam-still-to-slack-focus"))
    out_dir.mkdir(parents=True, exist_ok=True)
    extra_args = []
    shutter = int(num(options, "FOCUS_CALIBRATION_SHUTTER_US", 0))
    if shutter > 0:
        extra_args += ["--shutter", str(shutter)]
    ev = pick(options, "FOCUS_CALIBRATION_EV", default="")
    if ev:
        extra_args += ["--ev", ev]
    rows = []
    best = None
    for lens in focus_scan_values(options):
        image = out_dir / f"focus-{lens:.3f}.jpg"
        meta_path = out_dir / f"focus-{lens:.3f}.json"
        capture_still(
            options, image, meta_path,
            pick(options, "FOCUS_CALIBRATION_WIDTH", default=pick(options, "PREVIEW_WIDTH", default="1280")),
            pick(options, "FOCUS_CALIBRATION_HEIGHT", default=pick(options, "PREVIEW_HEIGHT", default="720")),
            pick(options, "FOCUS_CALIBRATION_QUALITY", default="85"),
            pick(options, "FOCUS_CALIBRATION_TIMEOUT_MS", default=pick(options, "PREVIEW_TIMEOUT_MS", default="2500")),
            dry_run, lens_position=lens, extra_args=extra_args,
        )
        meta = load_metadata(meta_path)
        score = metadata_number(meta, "FocusFoM") or -1.0
        rows.append((lens, score, image, meta_path))
        logging.info("focus scan lens_position=%s focus_fom=%s image=%s", lens, score, image)
        if best is None or score > best[1]:
            best = (lens, score, image, meta_path)
        if debug:
            log_metadata(f"focus scan {lens:.3f}", meta_path, meta, debug)
    report = out_dir / "results.csv"
    if not dry_run:
        report.write_text("lens_position,focus_fom,image,metadata\n" + "".join(f"{lens},{score},{image},{meta}\n" for lens, score, image, meta in rows), encoding="utf-8")
    if best is not None:
        lens, score, image, meta = best
        logging.info("recommended LENS_POSITION=%s focus_fom=%s image=%s metadata=%s", lens, score, image, meta)
        print(f"LENS_POSITION={lens}")
        print(f"FOCUS_FOM={score}")
        print(f"FOCUS_CALIBRATION_REPORT={report}")
    return 0


def embed_metadata(options: dict[str, str], output: Path, metadata: Path, profile: str, dry_run: bool) -> None:
    if not truthy(pick(options, "EMBED_EXIF_METADATA", default="1")):
        return
    command = [sys.executable, str(BASE_DIR / "embed_metadata.py"), str(output), str(metadata), "--profile", profile, "--exiftool", pick(options, "EXIFTOOL", default="exiftool")]
    if truthy(pick(options, "EXIF_METADATA_REQUIRED", default="0")):
        command.append("--required")
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
    parser.add_argument("--focus-calibration", action="store_true")
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
        if args.focus_calibration:
            return focus_calibration(options, args.dry_run, debug)
        output = Path(pick(options, "OUTPUT_PATH", default="/tmp/image.jpg"))
        preview = Path(pick(options, "PREVIEW_PATH", default="/tmp/rpicam-still-to-slack-preview.jpg"))
        preview_metadata = Path(pick(options, "METADATA_PATH", default="/tmp/rpicam-still-to-slack-preview.json"))
        final_metadata = Path(pick(options, "FINAL_METADATA_PATH", default="/tmp/rpicam-still-to-slack-final.json"))
        lock_path = Path(pick(options, "LOCK_PATH", default="/tmp/rpicam-still-to-slack.lock"))
        with lock(lock_path):
            fixed_lens = configured_lens_position(options)
            if fixed_lens is not None:
                logging.info("using configured LENS_POSITION=%s; focus pre-captures are skipped", fixed_lens)
                preview_meta = capture_preview_once(options, preview, preview_metadata, args.dry_run, lens_position=fixed_lens)
                final_lens = fixed_lens
            else:
                final_lens, preview_meta = refine_focus_with_precaptures(options, preview, preview_metadata, args.dry_run, debug)
            log_metadata("preview", preview_metadata, preview_meta, debug)
            profile = classify(preview_meta, options)
            logging.info("selected profile: %s", profile)
            capture_final(options, output, final_metadata, profile, final_lens, args.dry_run)
            final_meta = load_metadata(final_metadata)
            log_metadata("final", final_metadata, final_meta, debug)
            embed_metadata(options, output, final_metadata, profile, args.dry_run)
            upload(options, output, profile, args.dry_run, no_upload)
            if not (args.keep_preview or debug):
                for path in (preview, preview_metadata, final_metadata):
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
            elif debug:
                logging.debug("preview kept: %s", preview)
                logging.debug("preview metadata kept: %s", preview_metadata)
                logging.debug("final metadata kept: %s", final_metadata)
        return 0
    except Exception as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
