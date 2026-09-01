"""
 @file
 @brief Unit tests for the skin-region ("Haram Filter") detection and service
 @author OpenShot Studios

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
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
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

try:
    import openshot  # noqa: F401
except ModuleNotFoundError:
    openshot = types.SimpleNamespace(
        Clip=Mock,
        Frame=Mock,
        Fraction=Mock,
        FFmpegWriter=Mock,
        QtImageReader=Mock,
        LAYOUT_STEREO=3,
        LAYOUT_MONO=2,
    )
    sys.modules["openshot"] = openshot

import numpy as np

from classes.haram_filter import (
    HaramFilterSettings,
    box_blur,
    clean_mask,
    detect_skin_mask,
    dilate,
    erode,
    filter_frame_rgba,
    pixelate,
)


SKIN_RGB = (224, 172, 140)


def make_frame(height, width, background=(0, 0, 200)):
    """Return an opaque RGBA frame filled with a background color."""
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    frame[..., 0] = background[0]
    frame[..., 1] = background[1]
    frame[..., 2] = background[2]
    frame[..., 3] = 255
    return frame


def paint_skin(frame, top, bottom, left, right):
    frame[top:bottom, left:right, 0] = SKIN_RGB[0]
    frame[top:bottom, left:right, 1] = SKIN_RGB[1]
    frame[top:bottom, left:right, 2] = SKIN_RGB[2]


class TestSkinDetection(unittest.TestCase):
    def test_detects_skin_tones(self):
        tones = [
            (224, 172, 140),  # light
            (198, 134, 100),  # medium
            (141, 85, 61),    # tan
        ]
        for tone in tones:
            pixel = np.array([[tone]], dtype=np.uint8)
            self.assertTrue(
                detect_skin_mask(pixel)[0, 0],
                "expected %s to be detected as skin" % (tone,))

    def test_rejects_non_skin_colors(self):
        colors = [
            (60, 160, 60),    # grass green
            (128, 128, 128),  # neutral gray
            (30, 60, 200),    # blue
            (255, 255, 255),  # white
            (0, 0, 0),        # black
        ]
        for color in colors:
            pixel = np.array([[color]], dtype=np.uint8)
            self.assertFalse(
                detect_skin_mask(pixel)[0, 0],
                "expected %s NOT to be detected as skin" % (color,))

    def test_sensitivity_widens_detection(self):
        borderline = np.array([[(180, 120, 120)]], dtype=np.uint8)
        low = detect_skin_mask(borderline, sensitivity=0.0)[0, 0]
        high = detect_skin_mask(borderline, sensitivity=1.0)[0, 0]
        # Higher sensitivity may only ever detect more, never less
        self.assertGreaterEqual(int(high), int(low))


class TestMaskOperations(unittest.TestCase):
    def test_box_blur_preserves_constant_image(self):
        constant = np.full((16, 24), 9.0, dtype=np.float32)
        blurred = box_blur(constant, 3)
        self.assertEqual(blurred.shape, constant.shape)
        self.assertTrue(np.allclose(blurred, 9.0, atol=1e-3))

    def test_box_blur_spreads_impulse(self):
        impulse = np.zeros((21, 21), dtype=np.float32)
        impulse[10, 10] = 100.0
        blurred = box_blur(impulse, 2, passes=1)
        self.assertLess(blurred[10, 10], 100.0)
        self.assertGreater(blurred[10, 12], 0.0)
        self.assertAlmostEqual(float(blurred.sum()), 100.0, places=2)

    def test_dilate_grows_and_erode_shrinks(self):
        mask = np.zeros((20, 20), dtype=bool)
        mask[8:12, 8:12] = True
        self.assertGreater(dilate(mask, 2).sum(), mask.sum())
        self.assertLess(erode(mask, 1).sum(), mask.sum())

    def test_clean_mask_removes_specks(self):
        mask = np.zeros((100, 100), dtype=bool)
        mask[50, 50] = True  # single-pixel noise
        self.assertEqual(clean_mask(mask, 100, 100).sum(), 0)

    def test_pixelate_averages_blocks(self):
        image = np.zeros((8, 8, 3), dtype=np.float32)
        image[:4] = 100.0
        mosaic = pixelate(image, 8)
        self.assertEqual(mosaic.shape, image.shape)
        self.assertTrue(np.allclose(mosaic, 50.0, atol=1.0))


class TestFilterFrame(unittest.TestCase):
    def test_skin_region_is_obscured_and_rest_untouched(self):
        frame = make_frame(90, 120)
        paint_skin(frame, 30, 60, 40, 80)
        filtered, coverage = filter_frame_rgba(frame.tobytes(), 120, 90)
        self.assertGreater(coverage, 0.05)
        result = np.frombuffer(filtered, dtype=np.uint8).reshape(90, 120, 4)
        # Background far from the skin region is bit-identical
        self.assertTrue((result[0:10, 0:10] == frame[0:10, 0:10]).all())
        self.assertTrue((result[80:, 110:] == frame[80:, 110:]).all())
        # The center of the skin region was changed
        self.assertFalse((result[45, 60, :3] == frame[45, 60, :3]).all())
        # Alpha and dimensions are preserved (frame integrity)
        self.assertTrue((result[..., 3] == 255).all())
        self.assertEqual(len(filtered), frame.nbytes)

    def test_frame_without_skin_is_returned_unchanged(self):
        frame = make_frame(48, 64)
        buffer = frame.tobytes()
        filtered, coverage = filter_frame_rgba(buffer, 64, 48)
        self.assertEqual(coverage, 0.0)
        self.assertIs(filtered, buffer)

    def test_grayscale_setting_desaturates_obscured_region(self):
        frame = make_frame(90, 120)
        paint_skin(frame, 20, 70, 20, 100)
        settings = HaramFilterSettings(grayscale=True)
        filtered, _ = filter_frame_rgba(frame.tobytes(), 120, 90, settings=settings)
        result = np.frombuffer(filtered, dtype=np.uint8).reshape(90, 120, 4)
        center = result[45, 60, :3].astype(int)
        # Fully grayscale pixels have (nearly) equal channels
        self.assertLessEqual(center.max() - center.min(), 2)

    def test_color_setting_keeps_blur_colored(self):
        frame = make_frame(90, 120)
        paint_skin(frame, 20, 70, 20, 100)
        settings = HaramFilterSettings(grayscale=False)
        filtered, _ = filter_frame_rgba(frame.tobytes(), 120, 90, settings=settings)
        result = np.frombuffer(filtered, dtype=np.uint8).reshape(90, 120, 4)
        center = result[45, 60, :3].astype(int)
        self.assertGreater(center.max() - center.min(), 10)

    def test_row_padding_is_preserved(self):
        height, width = 60, 80
        frame = make_frame(height, width)
        paint_skin(frame, 20, 40, 20, 60)
        stride = width * 4 + 16
        rows = np.zeros((height, stride), dtype=np.uint8)
        rows[:, : width * 4] = frame.reshape(height, -1)
        filtered, coverage = filter_frame_rgba(
            rows.tobytes(), width, height, bytes_per_line=stride)
        self.assertGreater(coverage, 0.0)
        self.assertEqual(len(filtered), height * stride)
        result = np.frombuffer(filtered, dtype=np.uint8).reshape(height, stride)
        # Padding bytes are untouched
        self.assertTrue((result[:, width * 4:] == 0).all())

    def test_settings_from_app_settings(self):
        class FakeSettings:
            def __init__(self, values):
                self.values = values

            def get(self, key):
                return self.values.get(key)

        settings = HaramFilterSettings.from_app_settings(FakeSettings({
            "haram-filter-blur-amount": 42,
            "haram-filter-grayscale": False,
            "haram-filter-pixelate": True,
            "haram-filter-sensitivity": 0.8,
        }))
        self.assertEqual(settings.blur_amount, 42)
        self.assertFalse(settings.grayscale)
        self.assertTrue(settings.pixelate)
        self.assertAlmostEqual(settings.sensitivity, 0.8)

        defaults = HaramFilterSettings.from_app_settings(None)
        self.assertEqual(defaults.blur_amount, 20)
        self.assertTrue(defaults.grayscale)
        self.assertFalse(defaults.pixelate)


class FakeFrame:
    """Minimal stand-in for a libopenshot frame."""

    def __init__(self, rgba, with_setter=True):
        self._rgba = rgba
        self.written = None
        if with_setter:
            self.SetPixelsBytes = self._set_pixels

    def _set_pixels(self, data):
        self.written = data

    def GetWidth(self):
        return self._rgba.shape[1]

    def GetHeight(self):
        return self._rgba.shape[0]

    def GetBytesPerLine(self):
        return self._rgba.shape[1] * 4

    def GetPixelsBytes(self):
        return self._rgba.tobytes()


class TestHaramFilterService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.qt_test_app import get_or_create_app
        from qt_api import QApplication
        cls.app, cls.created_app = get_or_create_app(lambda: QApplication(sys.argv[:1]))

    def _make_service(self):
        from classes.haram_filter_service import HaramFilterService
        from qt_api import QObject
        win = QObject()
        service = HaramFilterService(win)
        self.addCleanup(service.shutdown)
        return service

    def test_frame_pixel_setter_detection(self):
        from classes.haram_filter_service import frame_pixel_setter
        frame = FakeFrame(np.zeros((4, 4, 4), dtype=np.uint8))
        self.assertIsNotNone(frame_pixel_setter(frame))
        frame_no_setter = FakeFrame(np.zeros((4, 4, 4), dtype=np.uint8), with_setter=False)
        self.assertIsNone(frame_pixel_setter(frame_no_setter))

    def test_is_filterable(self):
        from classes.haram_filter_service import HaramFilterService, FILTERED_SOURCE_KEY
        video = types.SimpleNamespace(id="F1", data={"media_type": "video"})
        image = types.SimpleNamespace(id="F2", data={"media_type": "image"})
        audio = types.SimpleNamespace(id="F3", data={"media_type": "audio"})
        output = types.SimpleNamespace(
            id="F4", data={"media_type": "video", FILTERED_SOURCE_KEY: "F1"})
        self.assertTrue(HaramFilterService.is_filterable(video))
        self.assertTrue(HaramFilterService.is_filterable(image))
        self.assertFalse(HaramFilterService.is_filterable(audio))
        self.assertFalse(HaramFilterService.is_filterable(output))
        self.assertTrue(HaramFilterService.is_filtered_output(output))
        self.assertFalse(HaramFilterService.is_filtered_output(video))

    def test_filter_frame_writes_back_into_source_frame(self):
        service = self._make_service()
        frame_pixels = make_frame(60, 80)
        paint_skin(frame_pixels, 20, 40, 20, 60)
        frame = FakeFrame(frame_pixels)
        settings = HaramFilterSettings()
        result = service._filter_frame(frame, settings, can_write_back=True)
        self.assertIs(result, frame)
        self.assertIsNotNone(frame.written)
        self.assertNotEqual(frame.written, frame_pixels.tobytes())

    def test_filter_frame_without_skin_returns_source_frame(self):
        service = self._make_service()
        frame = FakeFrame(make_frame(32, 32))
        settings = HaramFilterSettings()
        result = service._filter_frame(frame, settings, can_write_back=True)
        self.assertIs(result, frame)
        self.assertIsNone(frame.written)

    def test_maybe_auto_filter_respects_setting(self):
        service = self._make_service()
        video = types.SimpleNamespace(id="F1", data={"media_type": "video"})
        fake_app = types.SimpleNamespace(
            get_settings=lambda: types.SimpleNamespace(
                get=lambda key: {"haram-filter-auto-import": True}.get(key)))
        with patch("classes.haram_filter_service.get_app", return_value=fake_app), \
                patch.object(service, "filter_files") as filter_files:
            service.maybe_auto_filter([video])
            filter_files.assert_called_once()

        fake_app_off = types.SimpleNamespace(
            get_settings=lambda: types.SimpleNamespace(get=lambda key: None))
        with patch("classes.haram_filter_service.get_app", return_value=fake_app_off), \
                patch.object(service, "filter_files") as filter_files:
            service.maybe_auto_filter([video])
            filter_files.assert_not_called()

    def test_maybe_auto_filter_skips_own_imports(self):
        service = self._make_service()
        video = types.SimpleNamespace(id="F1", data={"media_type": "video"})
        service._importing_output = True
        fake_app = types.SimpleNamespace(
            get_settings=lambda: types.SimpleNamespace(
                get=lambda key: {"haram-filter-auto-import": True}.get(key)))
        with patch("classes.haram_filter_service.get_app", return_value=fake_app), \
                patch.object(service, "filter_files") as filter_files:
            service.maybe_auto_filter([video])
            filter_files.assert_not_called()

    def test_output_path_naming(self):
        service = self._make_service()
        fake_app = types.SimpleNamespace(project=None)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("classes.haram_filter_service.get_app", return_value=fake_app), \
                    patch("classes.haram_filter_service.info") as fake_info:
                fake_info.HARAM_FILTER_PATH = temp_dir
                file_data = {"path": "/media/workout.mp4", "media_type": "video"}
                output_path = service._output_path("F1", file_data)
                self.assertEqual(
                    output_path, os.path.join(temp_dir, "workout_filtered.mp4"))

                # A name collision falls back to a file-id suffix
                open(output_path, "wb").close()
                collision_path = service._output_path("F1", file_data)
                self.assertEqual(
                    collision_path,
                    os.path.join(temp_dir, "workout_filtered_F1.mp4"))

                image_data = {"path": "/media/pose.jpg", "media_type": "image"}
                self.assertTrue(
                    service._output_path("F2", image_data).endswith("pose_filtered.png"))


if __name__ == "__main__":
    unittest.main()
