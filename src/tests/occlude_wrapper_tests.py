"""
 @file
 @brief Unit tests for the OCCLUDE integration wrapper

 @section LICENSE

 Copyright (c) 2008-2021 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

import os
import shlex
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from classes import occlude_wrapper


# A stand-in for the occlude CLI: emits progress lines and writes the
# --output file, so run_occlude can be tested without the real models.
FAKE_OCCLUDE = r"""
import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--input"); p.add_argument("--output")
a = p.parse_args()
print('OCCLUDE-PROGRESS {"stage": "Pass 1/3 detect+track", "done": 5, "total": 10}')
print('OCCLUDE-PROGRESS {"stage": "Pass 3/3 render", "done": 10, "total": 10}')
print("some ordinary log line")
sys.stderr.write("Pass 3/3 render: 100%\r\n")
open(a.output, "w").write("fake video")
"""

FAKE_OCCLUDE_FAILING = r"""
import sys
print("loading models")
sys.stderr.write("error: judge model exploded\n")
sys.exit(1)
"""


class TestProgressParsing(unittest.TestCase):
    def test_parse_progress_line(self):
        payload = occlude_wrapper.parse_progress_line(
            'OCCLUDE-PROGRESS {"stage": "Pass 3/3 render", "done": 120, "total": 4000}')
        self.assertEqual(payload, {"stage": "Pass 3/3 render", "done": 120, "total": 4000})

    def test_parse_rejects_other_lines(self):
        self.assertIsNone(occlude_wrapper.parse_progress_line("done. output: x.mp4"))
        self.assertIsNone(occlude_wrapper.parse_progress_line("OCCLUDE-PROGRESS not json"))
        self.assertIsNone(occlude_wrapper.parse_progress_line(""))

    def test_overall_fraction_spans_pipeline(self):
        def frac(stage, done, total):
            return occlude_wrapper.overall_fraction(
                {"stage": stage, "done": done, "total": total})
        self.assertEqual(frac("Pass 1/3 detect+track", 0, 100), 0.0)
        self.assertEqual(frac("Pass 3/3 render", 100, 100), 1.0)
        # monotonic through the passes
        seq = [
            frac("Pass 1/3 detect+track", 50, 100),
            frac("Pass 2/3 collect", 50, 100),
            frac("Pass 2/3 judge", 50, 100),
            frac("Pass 3/3 render", 50, 100),
        ]
        self.assertEqual(seq, sorted(seq))
        # unknown totals and stages are handled without errors
        self.assertEqual(frac("Pass 1/3 detect+track", 5, None), 0.0)
        self.assertIsNone(frac("something new", 5, 10))


class TestFindOcclude(unittest.TestCase):
    def test_env_override_wins(self):
        os.environ["OCCLUDE_COMMAND"] = "/opt/x/python -m occlude"
        try:
            self.assertEqual(
                occlude_wrapper.find_occlude(),
                shlex.split("/opt/x/python -m occlude"))
        finally:
            del os.environ["OCCLUDE_COMMAND"]

    def test_blurred_output_path(self):
        self.assertEqual(
            occlude_wrapper.blurred_output_path("/tmp/My Video.mp4"),
            "/tmp/My Video_occluded.mp4")
        self.assertEqual(
            occlude_wrapper.blurred_output_path("/tmp/clip.webm"),
            "/tmp/clip_occluded.mp4")


class TestRunOcclude(unittest.TestCase):
    def _with_fake(self, script):
        fake = os.path.join(tempfile.mkdtemp(prefix="occlude_test_"), "fake_occlude.py")
        with open(fake, "w") as f:
            f.write(script)
        os.environ["OCCLUDE_COMMAND"] = "%s %s" % (shlex.quote(sys.executable), shlex.quote(fake))
        return os.path.dirname(fake)

    def tearDown(self):
        os.environ.pop("OCCLUDE_COMMAND", None)

    def test_successful_run_reports_progress(self):
        out_dir = self._with_fake(FAKE_OCCLUDE)
        output = os.path.join(out_dir, "video_occluded.mp4")
        fractions = []
        success, message = occlude_wrapper.run_occlude(
            "input.mp4", output,
            progress_callback=lambda f, stage: fractions.append(f))
        self.assertTrue(success, message)
        self.assertTrue(os.path.exists(output))
        self.assertEqual(message, "")
        self.assertTrue(fractions)
        self.assertEqual(fractions[-1], 1.0)

    def test_failing_run_surfaces_error_output(self):
        out_dir = self._with_fake(FAKE_OCCLUDE_FAILING)
        output = os.path.join(out_dir, "video_occluded.mp4")
        success, message = occlude_wrapper.run_occlude("input.mp4", output)
        self.assertFalse(success)
        self.assertIn("judge model exploded", message)

    def test_missing_output_is_a_failure(self):
        out_dir = self._with_fake("print('did nothing')\n")
        output = os.path.join(out_dir, "video_occluded.mp4")
        success, message = occlude_wrapper.run_occlude("input.mp4", output)
        self.assertFalse(success)
        self.assertIn("no output", message)

    def test_cancel_terminates_process(self):
        out_dir = self._with_fake("import time\ntime.sleep(60)\n")
        output = os.path.join(out_dir, "video_occluded.mp4")
        success, message = occlude_wrapper.run_occlude(
            "input.mp4", output, cancel_check=lambda: True)
        self.assertFalse(success)
        self.assertEqual(message, "Cancelled")


if __name__ == "__main__":
    unittest.main()
