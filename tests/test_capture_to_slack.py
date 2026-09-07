from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import capture_to_slack as app


class MetadataTests(unittest.TestCase):
    def test_frame_duration_is_not_exposure(self) -> None:
        metadata = {"FrameDuration": 33_333}
        self.assertIsNone(app.metadata_exposure_us(metadata))

    def test_total_gain_multiplies_analogue_and_digital(self) -> None:
        metadata = {"AnalogueGain": 4, "DigitalGain": "1.25"}
        self.assertEqual(app.metadata_total_gain(metadata), 5.0)

    def test_non_finite_metadata_is_ignored(self) -> None:
        metadata = {"ExposureTime": "nan", "SensorExposureTime": 1_500}
        self.assertEqual(app.metadata_exposure_us(metadata), 1_500)


class ExposurePlanTests(unittest.TestCase):
    OPTIONS = {
        "EXPOSURE_DAY_MAX_US": "2000",
        "GAIN_DAY_MAX": "2.0",
        "EXPOSURE_NIGHT_MIN_US": "10000",
        "GAIN_NIGHT_MIN": "4.0",
        "AUTO_SHUTTER": "1",
        "AUTO_SHUTTER_PROFILES": "night",
        "AUTO_SHUTTER_TARGET_GAIN": "2.0",
        "AUTO_SHUTTER_MIN_US": "1000",
        "AUTO_SHUTTER_MAX_US": "12000000",
        "AUTO_SHUTTER_STEP_US": "1000",
    }

    def test_bright_scene_does_not_get_fixed_shutter(self) -> None:
        metadata = {
            "ExposureTime": 1_000,
            "AnalogueGain": 1.0,
            "DigitalGain": 1.0,
            "Lux": 1.0,
        }
        self.assertEqual(
            app.select_capture_plan(metadata, self.OPTIONS),
            ("day", None),
        )

    def test_twilight_scene_does_not_get_fixed_shutter(self) -> None:
        metadata = {
            "ExposureTime": 5_000,
            "AnalogueGain": 1.5,
            "DigitalGain": 1.0,
        }
        self.assertEqual(
            app.select_capture_plan(metadata, self.OPTIONS),
            ("twilight", None),
        )

    def test_night_shutter_uses_exposure_and_total_gain(self) -> None:
        metadata = {
            "ExposureTime": 50_000,
            "AnalogueGain": 8.0,
            "DigitalGain": 1.25,
        }
        self.assertEqual(
            app.select_capture_plan(metadata, self.OPTIONS),
            ("night", 250_000),
        )

    def test_explicit_night_shutter_overrides_adaptive_estimate(self) -> None:
        metadata = {
            "ExposureTime": 66_541,
            "AnalogueGain": 7.876923,
            "DigitalGain": 1.008,
        }
        options = dict(self.OPTIONS, NIGHT_SHUTTER_US="4000000")
        self.assertEqual(
            app.select_capture_plan(metadata, options),
            ("night", 4_000_000),
        )

    def test_adaptive_shutter_does_not_shorten_low_gain_exposure(self) -> None:
        metadata = {
            "ExposureTime": 50_000,
            "AnalogueGain": 1.0,
            "DigitalGain": 1.0,
        }
        self.assertEqual(
            app.select_capture_plan(metadata, self.OPTIONS),
            ("night", 50_000),
        )

    def test_missing_exposure_keeps_night_profile_without_estimate(self) -> None:
        metadata = {"AnalogueGain": 8.0, "DigitalGain": 1.0}
        self.assertEqual(
            app.select_capture_plan(metadata, self.OPTIONS),
            ("night", None),
        )

    def test_profile_args_uses_fixed_shutter_without_planned_value(self) -> None:
        options = dict(self.OPTIONS, NIGHT_SHUTTER_US="4000000")
        args = app.profile_args("night", options, shutter_us=None)
        self.assertEqual(args[-2:], ["--shutter", "4000000"])


class ValidationTests(unittest.TestCase):
    def test_sample_configuration_is_valid(self) -> None:
        root = Path(__file__).resolve().parents[1]
        options = app.read_option_file(root / ".camera_option.sample")
        options |= app.read_option_file(root / ".slack_option.sample")
        app.validate_options(options, focus_calibration_mode=False)

    def test_zero_adaptive_step_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "STEP_US"):
            app.validate_options(
                {"AUTO_SHUTTER_STEP_US": "0"},
                focus_calibration_mode=False,
            )

    def test_sub_unity_target_gain_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "at least 1.0"):
            app.validate_options(
                {"AUTO_SHUTTER_TARGET_GAIN": "0.5"},
                focus_calibration_mode=False,
            )

    def test_zero_focus_step_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            app.focus_scan_values({"FOCUS_SCAN_STEP": "0"})

    def test_reversed_focus_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "END"):
            app.focus_scan_values(
                {
                    "FOCUS_SCAN_START": "2",
                    "FOCUS_SCAN_END": "1",
                    "FOCUS_SCAN_STEP": "0.5",
                }
            )

    def test_non_finite_number_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "finite"):
            app.num({"GAIN_DAY_MAX": "nan"}, "GAIN_DAY_MAX", 2.0)

    def test_unknown_comment_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported field"):
            app.format_comment("{unknown}", "timestamp", "day")

    def test_focus_scan_values_include_regular_range(self) -> None:
        values = app.focus_scan_values(
            {
                "FOCUS_SCAN_START": "0",
                "FOCUS_SCAN_END": "1",
                "FOCUS_SCAN_STEP": "0.5",
            }
        )
        self.assertEqual(values, [0.0, 0.5, 1.0])


class OperationTests(unittest.TestCase):
    def test_end_to_end_capture_uses_safe_night_plan_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_camera = root / "fake-rpicam-still"
            command_log = root / "commands.jsonl"
            fake_camera.write_text(
                """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1]

output = Path(option("--output"))
metadata_path = Path(option("--metadata"))
output.write_bytes(b"fake jpeg")
score = 20 if "-focus-02" in output.name else 10
lens = 0.3 if "-focus-02" in output.name else 0.2
metadata = {
    "ExposureTime": 50000,
    "AnalogueGain": 8.0,
    "DigitalGain": 1.25,
    "LensPosition": lens,
    "FocusFoM": score,
}
metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
with Path(%r).open("a", encoding="utf-8") as fp:
    fp.write(json.dumps(args) + "\\n")
"""
                % str(command_log),
                encoding="utf-8",
            )
            fake_camera.chmod(0o755)

            output = root / "image.jpg"
            preview = root / "preview.jpg"
            preview_metadata = root / "preview.json"
            final_metadata = root / "final.json"
            camera_options = root / "camera.options"
            camera_options.write_text(
                "\n".join(
                    [
                        f"RPICAM_STILL={fake_camera}",
                        f"OUTPUT_PATH={output}",
                        f"PREVIEW_PATH={preview}",
                        f"METADATA_PATH={preview_metadata}",
                        f"FINAL_METADATA_PATH={final_metadata}",
                        f"LOCK_PATH={root / 'capture.lock'}",
                        "PREVIEW_TIMEOUT_MS=10",
                        "TIMEOUT_MS=10",
                        "FOCUS_PRECAPTURE_COUNT=2",
                        "AUTO_SHUTTER=1",
                        "AUTO_SHUTTER_PROFILES=night",
                        "AUTO_SHUTTER_TARGET_GAIN=2",
                        "AUTO_SHUTTER_MIN_US=1000",
                        "AUTO_SHUTTER_MAX_US=12000000",
                        "AUTO_SHUTTER_STEP_US=1000",
                        "EXPOSURE_NIGHT_MIN_US=10000",
                        "GAIN_NIGHT_MIN=4",
                        "EMBED_EXIF_METADATA=0",
                    ]
                ),
                encoding="utf-8",
            )

            argv = [
                "capture_to_slack.py",
                "--camera-option",
                str(camera_options),
                "--slack-option",
                str(root / "missing-slack.options"),
                "--no-upload",
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(app.main(), 0)

            commands = [
                json.loads(line)
                for line in command_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(commands), 3)
            final_command = commands[-1]
            self.assertEqual(
                final_command[final_command.index("--shutter") + 1],
                "250000",
            )
            self.assertEqual(
                final_command[final_command.index("--lens-position") + 1],
                "0.300000",
            )
            self.assertTrue(output.exists())
            self.assertFalse(final_metadata.exists())
            self.assertEqual(list(root.glob("preview*")), [])

    def test_dry_run_does_not_modify_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.jpg"
            metadata = Path(directory) / "metadata.json"
            output.write_text("image", encoding="utf-8")
            metadata.write_text("metadata", encoding="utf-8")

            app.capture_still(
                {},
                output,
                metadata,
                "1280",
                "720",
                "75",
                "2500",
                True,
            )

            self.assertEqual(output.read_text(encoding="utf-8"), "image")
            self.assertEqual(metadata.read_text(encoding="utf-8"), "metadata")

    def test_subprocess_timeout_is_reported(self) -> None:
        with patch(
            "capture_to_slack.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["command"], 1),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                app.run(["command"], False, timeout_s=1)

    def test_lock_content_is_not_truncated_by_contender(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lock"
            with app.lock(path):
                owner = path.read_text(encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with app.lock(path):
                        pass
                self.assertEqual(path.read_text(encoding="utf-8"), owner)

    def test_focus_calibration_dry_run_has_no_fake_recommendation(self) -> None:
        options = {"FOCUS_SCAN_VALUES": "0.0,0.5"}
        output = StringIO()
        with redirect_stdout(output):
            result = app.focus_calibration(options, dry_run=True, debug=False)
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "")

    def test_embed_warning_is_forwarded_at_warning_level(self) -> None:
        with patch("capture_to_slack.run") as run:
            app.embed_metadata(
                {},
                Path("/tmp/image.jpg"),
                Path("/tmp/metadata.json"),
                "night",
                False,
            )
        self.assertEqual(
            run.call_args.kwargs["successful_stderr_level"],
            logging.WARNING,
        )

    def test_camera_timeout_accounts_for_long_shutter(self) -> None:
        timeout = app.camera_command_timeout_s(
            {"COMMAND_TIMEOUT_S": "5"},
            6_000,
            ["--shutter", "12000000"],
        )
        self.assertEqual(timeout, 28)


if __name__ == "__main__":
    unittest.main()
