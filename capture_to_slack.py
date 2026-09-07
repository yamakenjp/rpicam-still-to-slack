#!/usr/bin/env python3
"""Capture a still image with rpicam-still and upload it to Slack."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import math
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Formatter
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
SECRET_OPTION_MARKERS = {"TOKEN", "SECRET", "PASSWORD"}
PROFILES = {"day", "twilight", "night"}
DEPRECATED_OPTIONS = {
    "AUTO_SHUTTER_REFERENCE_US",
    "AUTO_SHUTTER_REFERENCE_LUX",
    "AUTO_SHUTTER_DAY_MAX_US",
    "AUTO_SHUTTER_TWILIGHT_MAX_US",
}


@dataclass(frozen=True)
class PreviewResult:
    lens_position: float | None
    metadata: dict[str, Any]
    metadata_path: Path
    temporary_paths: tuple[Path, ...]


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


def parse_finite_float(name: str, raw_value: str | float | int) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number: {raw_value!r}") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"{name} must be finite: {raw_value!r}")
    return value


def opt_float(options: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = options.get(name)
        if value not in (None, ""):
            return parse_finite_float(name, value)
    return None


def num(options: dict[str, str], name: str, default: float) -> float:
    return parse_finite_float(name, options.get(name, default))


def integer(options: dict[str, str], name: str, default: int) -> int:
    value = num(options, name, default)
    if not value.is_integer():
        raise RuntimeError(f"{name} must be an integer: {value}")
    return int(value)


def safe_options(options: dict[str, str]) -> dict[str, str]:
    return {
        key: (
            "********"
            if any(marker in key.upper() for marker in SECRET_OPTION_MARKERS)
            else value
        )
        for key, value in sorted(options.items())
    }


def run(
    command: list[str],
    dry_run: bool,
    *,
    timeout_s: float,
    successful_stderr_level: int = logging.DEBUG,
) -> None:
    logging.info("%s", shlex.join(command))
    if dry_run:
        return
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
        logging.log(successful_stderr_level, "%s", result.stderr.strip())
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: "
            f"{shlex.join(command)}{suffix}"
        )


def load_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("failed to read metadata %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logging.warning("metadata root is not an object: %s", path)
        return {}
    return data


def metadata_number(metadata: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float | str):
            with contextlib.suppress(TypeError, ValueError):
                number = float(value)
                if math.isfinite(number):
                    return number
    return None


def metadata_exposure_us(metadata: dict[str, Any]) -> float | None:
    # FrameDuration is deliberately excluded: it is not an exposure time.
    return metadata_number(metadata, "ExposureTime", "SensorExposureTime")


def metadata_total_gain(metadata: dict[str, Any]) -> float | None:
    analogue = metadata_number(metadata, "AnalogueGain")
    digital = metadata_number(metadata, "DigitalGain")
    gains = [gain for gain in (analogue, digital) if gain is not None and gain > 0]
    if not gains:
        return None
    return math.prod(gains)


def log_metadata(
    label: str,
    path: Path,
    metadata: dict[str, Any],
    debug: bool,
) -> None:
    exposure = metadata_exposure_us(metadata)
    frame_duration = metadata_number(metadata, "FrameDuration")
    analogue_gain = metadata_number(metadata, "AnalogueGain")
    digital_gain = metadata_number(metadata, "DigitalGain")
    total_gain = metadata_total_gain(metadata)
    lux = metadata_number(metadata, "Lux")
    lens_position = metadata_number(metadata, "LensPosition")
    focus_fom = metadata_number(metadata, "FocusFoM")
    logging.info(
        "%s metadata exposure_us=%s frame_duration_us=%s analogue_gain=%s "
        "digital_gain=%s total_gain=%s lux=%s lens_position=%s focus_fom=%s",
        label,
        exposure,
        frame_duration,
        analogue_gain,
        digital_gain,
        total_gain,
        lux,
        lens_position,
        focus_fom,
    )
    if debug:
        logging.debug("%s metadata file: %s", label, path)
        logging.debug(
            "%s metadata: %s",
            label,
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
        )


def configured_lens_position(options: dict[str, str]) -> float | None:
    return opt_float(options, "LENS_POSITION", "lens-position", "LENS-POSITION")


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def round_to_step(value: float, step: float) -> int:
    if step <= 0:
        raise RuntimeError("AUTO_SHUTTER_STEP_US must be greater than zero")
    return int(round(value / step) * step)


def auto_shutter_profiles(options: dict[str, str]) -> set[str]:
    raw = pick(options, "AUTO_SHUTTER_PROFILES", default="night")
    profiles = {item.strip().lower() for item in raw.split(",") if item.strip()}
    invalid = profiles - PROFILES
    if not profiles:
        raise RuntimeError("AUTO_SHUTTER_PROFILES must not be empty")
    if invalid:
        raise RuntimeError(
            f"AUTO_SHUTTER_PROFILES contains unknown values: {', '.join(sorted(invalid))}"
        )
    return profiles


def adaptive_shutter_enabled(options: dict[str, str]) -> bool:
    return truthy(pick(options, "AUTO_SHUTTER", default="1"))


def estimate_adaptive_shutter_us(
    metadata: dict[str, Any],
    options: dict[str, str],
) -> int | None:
    exposure_us = metadata_exposure_us(metadata)
    if exposure_us is None or exposure_us <= 0:
        logging.warning(
            "adaptive shutter unavailable because preview ExposureTime is missing"
        )
        return None

    total_gain = metadata_total_gain(metadata)
    target_gain = num(options, "AUTO_SHUTTER_TARGET_GAIN", 2.0)
    effective_gain = total_gain if total_gain is not None else target_gain
    min_us = num(options, "AUTO_SHUTTER_MIN_US", 1_000)
    max_us = num(options, "AUTO_SHUTTER_MAX_US", 12_000_000)
    step_us = num(options, "AUTO_SHUTTER_STEP_US", 1_000)

    # Do not shorten an already low-gain exposure. The adaptive path may trade
    # excess gain for a longer shutter, but must not introduce extra gain.
    gain_scale = max(effective_gain / target_gain, 1.0)
    estimated = exposure_us * gain_scale
    clamped = clamp(estimated, min_us, max_us)
    rounded = round_to_step(clamped, step_us)
    selected = int(clamp(rounded, min_us, max_us))
    logging.info(
        "adaptive shutter preview_exposure_us=%s total_gain=%s "
        "target_gain=%s estimated_us=%.1f selected_us=%s",
        exposure_us,
        total_gain,
        target_gain,
        estimated,
        selected,
    )
    return selected


def classify(metadata: dict[str, Any], options: dict[str, str]) -> str:
    exposure = metadata_exposure_us(metadata)
    gain = metadata_total_gain(metadata)
    lux = metadata_number(metadata, "Lux")
    logging.info(
        "classifying preview exposure_us=%s total_gain=%s lux=%s",
        exposure,
        gain,
        lux,
    )

    # ExposureTime and gain can remain high in an IMX708 HDR preview even as
    # dawn arrives. Use the AE lux estimate as a safety gate so a long fixed
    # night shutter is never carried into twilight or daytime.
    if lux is not None:
        if lux >= num(options, "LUX_DAY_MIN", 100.0):
            return "day"
        if lux > num(options, "LUX_NIGHT_MAX", 2.0):
            return "twilight"

    if exposure is None and gain is None:
        return "twilight"
    if exposure is not None and exposure <= num(
        options, "EXPOSURE_DAY_MAX_US", 2_000
    ):
        if gain is None or gain <= num(options, "GAIN_DAY_MAX", 2.0):
            return "day"
    if exposure is not None and exposure >= num(
        options, "EXPOSURE_NIGHT_MIN_US", 30_000
    ):
        return "night"
    if gain is not None and gain >= num(options, "GAIN_NIGHT_MIN", 8.0):
        return "night"
    return "twilight"


def select_capture_plan(
    metadata: dict[str, Any],
    options: dict[str, str],
) -> tuple[str, int | None]:
    profile = classify(metadata, options)
    shutter_us = fixed_profile_shutter_us(profile, options)
    if shutter_us is not None:
        source = "fixed profile shutter"
    elif (
        adaptive_shutter_enabled(options)
        and profile in auto_shutter_profiles(options)
    ):
        shutter_us = estimate_adaptive_shutter_us(metadata, options)
        source = (
            "adaptive shutter" if shutter_us is not None else "preview classifier"
        )
    else:
        source = "preview classifier"
    logging.info("selected profile=%s source=%s", profile, source)
    return profile, shutter_us


def base_args(
    options: dict[str, str],
    lens_position: float | None = None,
) -> list[str]:
    args = [
        pick(options, "RPICAM_STILL", default="rpicam-still"),
        "--nopreview",
        "--hdr",
        pick(options, "HDR_MODE", default="auto"),
    ]
    if lens_position is None:
        args += [
            "--autofocus-mode",
            pick(options, "AUTOFOCUS_MODE", default="continuous"),
        ]
    else:
        args += [
            "--autofocus-mode",
            "manual",
            "--lens-position",
            f"{lens_position:.6f}",
        ]
    args += ["--metering", pick(options, "METERING", default="average")]
    return args


def fixed_profile_shutter_us(
    profile: str,
    options: dict[str, str],
) -> int | None:
    value = integer(options, f"{profile.upper()}_SHUTTER_US", 0)
    return value if value > 0 else None


def append_shutter_arg(
    args: list[str],
    shutter_us: int | None,
) -> list[str]:
    if shutter_us is not None and shutter_us > 0:
        args += ["--shutter", str(shutter_us)]
    return args


def profile_args(
    profile: str,
    options: dict[str, str],
    shutter_us: int | None = None,
) -> list[str]:
    selected_shutter = (
        shutter_us
        if shutter_us is not None
        else fixed_profile_shutter_us(profile, options)
    )
    if profile == "day":
        args = [
            "--ev",
            pick(options, "DAY_EV", default="-0.3"),
            "--exposure",
            pick(options, "DAY_EXPOSURE", default="normal"),
            "--denoise",
            pick(options, "DAY_DENOISE", default="cdn_fast"),
        ]
    elif profile == "night":
        args = [
            "--ev",
            pick(options, "NIGHT_EV", default="0.7"),
            "--exposure",
            pick(options, "NIGHT_EXPOSURE", default="long"),
            "--denoise",
            pick(options, "NIGHT_DENOISE", default="cdn_hq"),
        ]
    else:
        args = [
            "--ev",
            pick(options, "TWILIGHT_EV", default="0"),
            "--exposure",
            pick(options, "TWILIGHT_EXPOSURE", default="normal"),
            "--denoise",
            pick(options, "TWILIGHT_DENOISE", default="cdn_fast"),
        ]
    return append_shutter_arg(args, selected_shutter)


def camera_command_timeout_s(
    options: dict[str, str],
    rpicam_timeout_ms: int,
    extra_args: list[str] | None,
) -> float:
    shutter_us = 0
    if extra_args and "--shutter" in extra_args:
        index = extra_args.index("--shutter")
        with contextlib.suppress(IndexError, ValueError):
            shutter_us = int(extra_args[index + 1])
    minimum_s = rpicam_timeout_ms / 1_000 + shutter_us / 1_000_000 + 10
    return max(num(options, "COMMAND_TIMEOUT_S", 45), minimum_s)


def capture_still(
    options: dict[str, str],
    output: Path,
    metadata: Path,
    width: str,
    height: str,
    quality: str,
    timeout: str,
    dry_run: bool,
    lens_position: float | None = None,
    extra_args: list[str] | None = None,
) -> None:
    if not dry_run:
        for path in (output, metadata):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata.parent.mkdir(parents=True, exist_ok=True)
    command = base_args(options, lens_position)
    if extra_args:
        command += extra_args
    command += [
        "--timeout",
        timeout,
        "--width",
        width,
        "--height",
        height,
        "--quality",
        quality,
        "--metadata",
        str(metadata),
        "--metadata-format",
        "json",
        "--output",
        str(output),
    ]
    timeout_ms = int(timeout)
    run(
        command,
        dry_run,
        timeout_s=camera_command_timeout_s(options, timeout_ms, extra_args),
    )


def capture_preview_once(
    options: dict[str, str],
    preview: Path,
    metadata: Path,
    dry_run: bool,
    lens_position: float | None = None,
) -> dict[str, Any]:
    capture_still(
        options,
        preview,
        metadata,
        pick(options, "PREVIEW_WIDTH", default="1280"),
        pick(options, "PREVIEW_HEIGHT", default="720"),
        "75",
        pick(options, "PREVIEW_TIMEOUT_MS", default="2500"),
        dry_run,
        lens_position=lens_position,
    )
    return load_metadata(metadata)


def refine_focus_with_precaptures(
    options: dict[str, str],
    preview: Path,
    metadata: Path,
    dry_run: bool,
    debug: bool,
) -> PreviewResult:
    count = integer(options, "FOCUS_PRECAPTURE_COUNT", 3)
    if count == 1:
        meta = capture_preview_once(options, preview, metadata, dry_run)
        return PreviewResult(
            metadata_number(meta, "LensPosition"),
            meta,
            metadata,
            (preview, metadata),
        )

    temporary_paths: list[Path] = []
    best_meta: dict[str, Any] = {}
    best_metadata_path = metadata
    best_score: float | None = None
    best_lens: float | None = None
    for index in range(1, count + 1):
        sample_preview = preview.with_name(
            f"{preview.stem}-focus-{index:02d}{preview.suffix}"
        )
        sample_metadata = metadata.with_name(
            f"{metadata.stem}-focus-{index:02d}{metadata.suffix}"
        )
        temporary_paths.extend((sample_preview, sample_metadata))
        meta = capture_preview_once(
            options,
            sample_preview,
            sample_metadata,
            dry_run,
        )
        log_metadata(f"focus pre-capture {index}", sample_metadata, meta, debug)
        score = metadata_number(meta, "FocusFoM")
        lens = metadata_number(meta, "LensPosition")
        if lens is not None and score is not None and (
            best_score is None or score > best_score
        ):
            best_score = score
            best_lens = lens
            best_meta = meta
            best_metadata_path = sample_metadata

    if dry_run:
        return PreviewResult(
            None,
            {},
            best_metadata_path,
            tuple(temporary_paths),
        )
    if best_lens is None:
        logging.warning(
            "no usable LensPosition/FocusFoM pair found; "
            "using one continuous-AF preview"
        )
        meta = capture_preview_once(options, preview, metadata, dry_run)
        temporary_paths.extend((preview, metadata))
        return PreviewResult(
            metadata_number(meta, "LensPosition"),
            meta,
            metadata,
            tuple(temporary_paths),
        )

    logging.info(
        "selected pre-capture lens_position=%s focus_fom=%s",
        best_lens,
        best_score,
    )
    return PreviewResult(
        best_lens,
        best_meta,
        best_metadata_path,
        tuple(temporary_paths),
    )


def capture_final(
    options: dict[str, str],
    output: Path,
    metadata: Path,
    profile: str,
    lens_position: float | None,
    dry_run: bool,
    shutter_us: int | None = None,
) -> None:
    capture_still(
        options,
        output,
        metadata,
        pick(options, "WIDTH", default="2304"),
        pick(options, "HEIGHT", default="1296"),
        pick(options, "QUALITY", default="92"),
        pick(options, "TIMEOUT_MS", default="3000"),
        dry_run,
        lens_position=lens_position,
        extra_args=profile_args(profile, options, shutter_us=shutter_us),
    )


def focus_scan_values(options: dict[str, str]) -> list[float]:
    raw_values = pick(options, "FOCUS_SCAN_VALUES", default="")
    if raw_values:
        values = [
            parse_finite_float("FOCUS_SCAN_VALUES", item.strip())
            for item in raw_values.split(",")
            if item.strip()
        ]
        if not values:
            raise RuntimeError("FOCUS_SCAN_VALUES must contain at least one value")
    else:
        start = num(options, "FOCUS_SCAN_START", 0.0)
        end = num(options, "FOCUS_SCAN_END", 8.0)
        step = num(options, "FOCUS_SCAN_STEP", 0.5)
        if step <= 0:
            raise RuntimeError("FOCUS_SCAN_STEP must be greater than zero")
        if end < start:
            raise RuntimeError(
                "FOCUS_SCAN_END must be greater than or equal to FOCUS_SCAN_START"
            )
        count = int(math.floor((end - start) / step + 1e-9)) + 1
        values = [round(start + index * step, 6) for index in range(count)]
        if values[-1] < end and end - values[-1] < step / 10:
            values.append(round(end, 6))
    if any(value < 0 for value in values):
        raise RuntimeError("focus lens positions must be zero or greater")
    return values


def focus_calibration(
    options: dict[str, str],
    dry_run: bool,
    debug: bool,
) -> int:
    out_dir = Path(
        pick(
            options,
            "FOCUS_CALIBRATION_DIR",
            default="/tmp/rpicam-still-to-slack-focus",
        )
    )
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    extra_args: list[str] = []
    shutter = integer(options, "FOCUS_CALIBRATION_SHUTTER_US", 0)
    if shutter < 0:
        raise RuntimeError(
            "FOCUS_CALIBRATION_SHUTTER_US must be zero or greater"
        )
    if shutter > 0:
        extra_args += ["--shutter", str(shutter)]
    ev = pick(options, "FOCUS_CALIBRATION_EV", default="")
    if ev:
        extra_args += ["--ev", ev]

    rows: list[tuple[float, float | None, Path, Path]] = []
    best: tuple[float, float, Path, Path] | None = None
    for lens in focus_scan_values(options):
        image = out_dir / f"focus-{lens:.3f}.jpg"
        meta_path = out_dir / f"focus-{lens:.3f}.json"
        capture_still(
            options,
            image,
            meta_path,
            pick(
                options,
                "FOCUS_CALIBRATION_WIDTH",
                default=pick(options, "PREVIEW_WIDTH", default="1280"),
            ),
            pick(
                options,
                "FOCUS_CALIBRATION_HEIGHT",
                default=pick(options, "PREVIEW_HEIGHT", default="720"),
            ),
            pick(options, "FOCUS_CALIBRATION_QUALITY", default="85"),
            pick(
                options,
                "FOCUS_CALIBRATION_TIMEOUT_MS",
                default=pick(options, "PREVIEW_TIMEOUT_MS", default="2500"),
            ),
            dry_run,
            lens_position=lens,
            extra_args=extra_args,
        )
        meta = load_metadata(meta_path)
        score = metadata_number(meta, "FocusFoM")
        rows.append((lens, score, image, meta_path))
        logging.info(
            "focus scan lens_position=%s focus_fom=%s image=%s",
            lens,
            score,
            image,
        )
        if score is not None and (best is None or score > best[1]):
            best = (lens, score, image, meta_path)
        if debug:
            log_metadata(f"focus scan {lens:.3f}", meta_path, meta, debug)

    report = out_dir / "results.csv"
    if dry_run:
        logging.info(
            "focus calibration dry-run completed; "
            "no LENS_POSITION recommendation was generated"
        )
        return 0

    report.write_text(
        "lens_position,focus_fom,image,metadata\n"
        + "".join(
            f"{lens},{'' if score is None else score},{image},{meta}\n"
            for lens, score, image, meta in rows
        ),
        encoding="utf-8",
    )
    if best is None:
        raise RuntimeError(
            "focus calibration produced no usable FocusFoM metadata"
        )
    lens, score, image, meta = best
    logging.info(
        "recommended LENS_POSITION=%s focus_fom=%s image=%s metadata=%s",
        lens,
        score,
        image,
        meta,
    )
    print(f"LENS_POSITION={lens}")
    print(f"FOCUS_FOM={score}")
    print(f"FOCUS_CALIBRATION_REPORT={report}")
    return 0


def embed_metadata(
    options: dict[str, str],
    output: Path,
    metadata: Path,
    profile: str,
    dry_run: bool,
) -> None:
    if not truthy(pick(options, "EMBED_EXIF_METADATA", default="1")):
        return
    timeout_s = num(options, "EXIFTOOL_TIMEOUT_S", 30)
    command = [
        sys.executable,
        str(BASE_DIR / "embed_metadata.py"),
        str(output),
        str(metadata),
        "--profile",
        profile,
        "--exiftool",
        pick(options, "EXIFTOOL", default="exiftool"),
        "--timeout-seconds",
        str(timeout_s),
        "--log-level",
        "WARNING",
    ]
    if truthy(pick(options, "EXIF_METADATA_REQUIRED", default="0")):
        command.append("--required")
    run(
        command,
        dry_run,
        timeout_s=timeout_s + 5,
        successful_stderr_level=logging.WARNING,
    )


def format_comment(template: str, timestamp: str, profile: str) -> str:
    allowed_fields = {"timestamp", "profile"}
    for _, field_name, format_spec, _ in Formatter().parse(template):
        if field_name and field_name not in allowed_fields:
            raise RuntimeError(
                f"COMMENT_TEMPLATE has unsupported field: {field_name}"
            )
        if "{" in format_spec or "}" in format_spec:
            raise RuntimeError(
                "COMMENT_TEMPLATE nested replacement fields are not supported"
            )
    try:
        return template.format(timestamp=timestamp, profile=profile)
    except (IndexError, KeyError, ValueError) as exc:
        raise RuntimeError(f"invalid COMMENT_TEMPLATE: {exc}") from exc


def upload(
    options: dict[str, str],
    output: Path,
    profile: str,
    dry_run: bool,
    no_upload: bool,
) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    comment = format_comment(
        pick(
            options,
            "COMMENT_TEMPLATE",
            default="Photo taken at {timestamp}! profile={profile}",
        ),
        timestamp,
        profile,
    )
    logging.info("Slack comment: %s", comment)
    if dry_run or no_upload:
        logging.info("Slack upload skipped")
        return
    token = pick(options, "SLACK_BOT_TOKEN", "SLACK_TOKEN")
    channel = pick(options, "SLACK_CHANNEL_ID", "CHANNEL")
    if not output.exists():
        raise RuntimeError(f"output image not found: {output}")
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as exc:
        raise RuntimeError(
            "slack-sdk is not installed; run pip install -r requirements.txt"
        ) from exc
    try:
        WebClient(
            token=token,
            timeout=num(options, "SLACK_TIMEOUT_S", 30),
        ).files_upload_v2(
            channel=channel,
            file=str(output),
            filename=output.name,
            title=output.name,
            initial_comment=comment,
        )
    except SlackApiError as exc:
        raise RuntimeError(
            f"Slack upload failed: {exc.response.get('error')}"
        ) from exc


@contextlib.contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "another capture process is already running"
            ) from exc
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        yield


def check_secret_file_permissions(path: Path) -> None:
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        logging.warning(
            "%s is readable or writable by group/others (mode %03o); "
            "run chmod 600 %s",
            path,
            mode,
            path,
        )


def validate_options(
    options: dict[str, str],
    *,
    focus_calibration_mode: bool,
) -> None:
    positive_integers = {
        "WIDTH": 2304,
        "HEIGHT": 1296,
        "PREVIEW_WIDTH": 1280,
        "PREVIEW_HEIGHT": 720,
        "PREVIEW_TIMEOUT_MS": 2500,
        "TIMEOUT_MS": 3000,
        "FOCUS_PRECAPTURE_COUNT": 3,
        "FOCUS_CALIBRATION_WIDTH": 1280,
        "FOCUS_CALIBRATION_HEIGHT": 720,
        "FOCUS_CALIBRATION_QUALITY": 85,
        "FOCUS_CALIBRATION_TIMEOUT_MS": 2500,
    }
    for name, default in positive_integers.items():
        if integer(options, name, default) <= 0:
            raise RuntimeError(f"{name} must be greater than zero")

    quality = integer(options, "QUALITY", 92)
    if not 1 <= quality <= 100:
        raise RuntimeError("QUALITY must be between 1 and 100")
    calibration_quality = integer(options, "FOCUS_CALIBRATION_QUALITY", 85)
    if not 1 <= calibration_quality <= 100:
        raise RuntimeError(
            "FOCUS_CALIBRATION_QUALITY must be between 1 and 100"
        )

    for name, default in (
        ("COMMAND_TIMEOUT_S", 45),
        ("EXIFTOOL_TIMEOUT_S", 30),
        ("SLACK_TIMEOUT_S", 30),
    ):
        if num(options, name, default) <= 0:
            raise RuntimeError(f"{name} must be greater than zero")

    day_exposure = num(options, "EXPOSURE_DAY_MAX_US", 2_000)
    night_exposure = num(options, "EXPOSURE_NIGHT_MIN_US", 30_000)
    if day_exposure < 0 or night_exposure <= day_exposure:
        raise RuntimeError(
            "exposure thresholds must satisfy "
            "0 <= EXPOSURE_DAY_MAX_US < EXPOSURE_NIGHT_MIN_US"
        )
    day_gain = num(options, "GAIN_DAY_MAX", 2.0)
    night_gain = num(options, "GAIN_NIGHT_MIN", 8.0)
    if day_gain <= 0 or night_gain <= day_gain:
        raise RuntimeError(
            "gain thresholds must satisfy 0 < GAIN_DAY_MAX < GAIN_NIGHT_MIN"
        )
    night_lux = num(options, "LUX_NIGHT_MAX", 2.0)
    day_lux = num(options, "LUX_DAY_MIN", 100.0)
    if night_lux < 0 or day_lux <= night_lux:
        raise RuntimeError(
            "lux thresholds must satisfy 0 <= LUX_NIGHT_MAX < LUX_DAY_MIN"
        )

    if adaptive_shutter_enabled(options):
        minimum = num(options, "AUTO_SHUTTER_MIN_US", 1_000)
        maximum = num(options, "AUTO_SHUTTER_MAX_US", 12_000_000)
        step = num(options, "AUTO_SHUTTER_STEP_US", 1_000)
        target_gain = num(options, "AUTO_SHUTTER_TARGET_GAIN", 2.0)
        if minimum <= 0 or maximum < minimum:
            raise RuntimeError(
                "adaptive shutter limits must satisfy "
                "0 < AUTO_SHUTTER_MIN_US <= AUTO_SHUTTER_MAX_US"
            )
        if step <= 0:
            raise RuntimeError(
                "AUTO_SHUTTER_STEP_US must be greater than zero"
            )
        if target_gain < 1:
            raise RuntimeError(
                "AUTO_SHUTTER_TARGET_GAIN must be at least 1.0"
            )
        auto_shutter_profiles(options)

    for profile in PROFILES:
        name = f"{profile.upper()}_SHUTTER_US"
        if integer(options, name, 0) < 0:
            raise RuntimeError(f"{name} must be zero or greater")
    if integer(options, "FOCUS_CALIBRATION_SHUTTER_US", 0) < 0:
        raise RuntimeError(
            "FOCUS_CALIBRATION_SHUTTER_US must be zero or greater"
        )

    lens_position = configured_lens_position(options)
    if lens_position is not None and lens_position < 0:
        raise RuntimeError("LENS_POSITION must be zero or greater")
    autofocus_mode = pick(
        options,
        "AUTOFOCUS_MODE",
        default="continuous",
    ).lower()
    if autofocus_mode not in {"default", "auto", "continuous", "manual"}:
        raise RuntimeError(
            "AUTOFOCUS_MODE must be default, auto, continuous, or manual"
        )
    if (
        autofocus_mode == "manual"
        and lens_position is None
        and not focus_calibration_mode
    ):
        raise RuntimeError(
            "LENS_POSITION is required when AUTOFOCUS_MODE=manual"
        )

    if focus_calibration_mode:
        focus_scan_values(options)

    format_comment(
        pick(
            options,
            "COMMENT_TEMPLATE",
            default="Photo taken at {timestamp}! profile={profile}",
        ),
        "2000-01-01 00:00:00 +0000",
        "day",
    )

    deprecated = sorted(DEPRECATED_OPTIONS & options.keys())
    if deprecated:
        logging.warning(
            "deprecated options are ignored by the safer exposure model: %s",
            ", ".join(deprecated),
        )


def cleanup(paths: tuple[Path, ...]) -> None:
    for path in dict.fromkeys(paths):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--slack-option",
        type=Path,
        default=BASE_DIR / ".slack_option",
    )
    parser.add_argument(
        "--camera-option",
        type=Path,
        default=BASE_DIR / ".camera_option",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--keep-preview", action="store_true")
    parser.add_argument("--focus-calibration", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    debug = args.debug
    try:
        options = read_option_file(args.slack_option) | read_option_file(
            args.camera_option
        )
        debug = (
            args.debug
            or truthy(options.get("DEBUG"))
            or truthy(options.get("DEBUG_MODE"))
        )
        no_upload = (
            args.no_upload
            or truthy(options.get("NO_UPLOAD"))
            or truthy(options.get("DEBUG_NO_UPLOAD"))
        )
        log_level = "DEBUG" if debug else args.log_level.upper()
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
        )
        validate_options(
            options,
            focus_calibration_mode=args.focus_calibration,
        )
        check_secret_file_permissions(args.slack_option)
        if debug:
            logging.debug("debug mode enabled")
            logging.debug(
                "options: %s",
                json.dumps(
                    safe_options(options),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        if args.focus_calibration:
            return focus_calibration(options, args.dry_run, debug)

        output = Path(
            pick(options, "OUTPUT_PATH", default="/tmp/image.jpg")
        )
        preview = Path(
            pick(
                options,
                "PREVIEW_PATH",
                default="/tmp/rpicam-still-to-slack-preview.jpg",
            )
        )
        preview_metadata = Path(
            pick(
                options,
                "METADATA_PATH",
                default="/tmp/rpicam-still-to-slack-preview.json",
            )
        )
        final_metadata = Path(
            pick(
                options,
                "FINAL_METADATA_PATH",
                default="/tmp/rpicam-still-to-slack-final.json",
            )
        )
        lock_path = Path(
            pick(
                options,
                "LOCK_PATH",
                default="/tmp/rpicam-still-to-slack.lock",
            )
        )

        capture_lock = (
            contextlib.nullcontext()
            if args.dry_run
            else lock(lock_path)
        )
        with capture_lock:
            fixed_lens = configured_lens_position(options)
            if fixed_lens is not None:
                logging.info(
                    "using configured LENS_POSITION=%s; "
                    "focus pre-captures are skipped",
                    fixed_lens,
                )
                preview_meta = capture_preview_once(
                    options,
                    preview,
                    preview_metadata,
                    args.dry_run,
                    lens_position=fixed_lens,
                )
                preview_result = PreviewResult(
                    fixed_lens,
                    preview_meta,
                    preview_metadata,
                    (preview, preview_metadata),
                )
            else:
                preview_result = refine_focus_with_precaptures(
                    options,
                    preview,
                    preview_metadata,
                    args.dry_run,
                    debug,
                )

            log_metadata(
                "preview",
                preview_result.metadata_path,
                preview_result.metadata,
                debug,
            )
            profile, shutter_us = select_capture_plan(
                preview_result.metadata,
                options,
            )
            capture_final(
                options,
                output,
                final_metadata,
                profile,
                preview_result.lens_position,
                args.dry_run,
                shutter_us=shutter_us,
            )
            final_meta = load_metadata(final_metadata)
            log_metadata("final", final_metadata, final_meta, debug)
            embed_metadata(
                options,
                output,
                final_metadata,
                profile,
                args.dry_run,
            )
            upload(
                options,
                output,
                profile,
                args.dry_run,
                no_upload,
            )

            temporary_paths = preview_result.temporary_paths + (
                final_metadata,
            )
            if not args.dry_run and not (args.keep_preview or debug):
                cleanup(temporary_paths)
            else:
                for path in temporary_paths:
                    logging.debug("temporary file kept: %s", path)
        return 0
    except Exception as exc:
        if debug:
            logging.exception("%s", exc)
        else:
            logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
