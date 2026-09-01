"""
 @file
 @brief Skin-region detection and selective blur ("Haram Filter") frame logic
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

 This module ports the filtering idea of the Tahir / Haram Filter browser
 extension (blur + optional grayscale with a configurable blur amount) from
 whole-element CSS filters to per-pixel, region-based video processing:
 instead of blurring an entire frame, exposed-skin regions are detected and
 only those regions are obscured, so the surrounding content (for example the
 instruction in an exercise video) stays legible.

 The implementation is pure numpy so it can run and be unit-tested without
 libopenshot or OpenCV.
"""

from dataclasses import dataclass


# Tahir browser-extension defaults (see HaramFilter background.js): the
# extension installs with blurAmt=20 and grayscale=true, which we mirror here.
DEFAULT_BLUR_AMOUNT = 20
DEFAULT_GRAYSCALE = True
DEFAULT_SENSITIVITY = 0.5

# Settings keys (also used in settings/_default.settings)
SETTING_BLUR_AMOUNT = "haram-filter-blur-amount"
SETTING_GRAYSCALE = "haram-filter-grayscale"
SETTING_PIXELATE = "haram-filter-pixelate"
SETTING_SENSITIVITY = "haram-filter-sensitivity"
SETTING_AUTO_IMPORT = "haram-filter-auto-import"


def _np():
    """Import numpy lazily (matching how other optional numeric helpers in
    this codebase defer the import until actually needed)."""
    import numpy
    return numpy


@dataclass
class HaramFilterSettings:
    """User-facing settings for the skin filter.

    blur_amount and grayscale carry the same meaning (and defaults) as the
    Tahir extension's blurAmt/grayscale settings; the remaining values tune
    the skin detector that the extension did not need (it blurred whole
    elements instead of regions).
    """

    blur_amount: int = DEFAULT_BLUR_AMOUNT
    grayscale: bool = DEFAULT_GRAYSCALE
    pixelate: bool = False
    sensitivity: float = DEFAULT_SENSITIVITY

    @classmethod
    def from_app_settings(cls, settings):
        """Build filter settings from the OpenShot settings store."""
        def _get(key, default):
            if not settings:
                return default
            try:
                value = settings.get(key)
            except Exception:
                return default
            return default if value is None else value

        try:
            blur_amount = int(_get(SETTING_BLUR_AMOUNT, DEFAULT_BLUR_AMOUNT))
        except (TypeError, ValueError):
            blur_amount = DEFAULT_BLUR_AMOUNT
        try:
            sensitivity = float(_get(SETTING_SENSITIVITY, DEFAULT_SENSITIVITY))
        except (TypeError, ValueError):
            sensitivity = DEFAULT_SENSITIVITY
        return cls(
            blur_amount=max(1, min(100, blur_amount)),
            grayscale=bool(_get(SETTING_GRAYSCALE, DEFAULT_GRAYSCALE)),
            pixelate=bool(_get(SETTING_PIXELATE, False)),
            sensitivity=max(0.0, min(1.0, sensitivity)),
        )


def detect_skin_mask(rgb, sensitivity=DEFAULT_SENSITIVITY):
    """Return a boolean (H, W) mask of likely skin pixels.

    Combines three classic, complementary color-space rules so that no
    single lighting condition dominates:

    - YCbCr chrominance box (Chai & Ngan): robust to brightness changes
    - RGB heuristics (Peer et al.): rejects gray/neutral surfaces
    - A red/green dominance check that holds across skin tones

    ``sensitivity`` in [0, 1] widens (1) or narrows (0) the chrominance
    thresholds; 0.5 keeps the published defaults.
    """
    np = _np()
    rgb = np.asarray(rgb)
    r = rgb[..., 0].astype(np.float32)
    g = rgb[..., 1].astype(np.float32)
    b = rgb[..., 2].astype(np.float32)

    # Widen/narrow the YCbCr chrominance box based on sensitivity
    spread = (float(sensitivity) - 0.5) * 2.0  # -1 .. 1
    cb_pad = 6.0 * spread
    cr_pad = 5.0 * spread

    # ITU-R BT.601 full-range YCbCr
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b

    ycbcr_rule = (
        (y > 40.0)
        & (cb >= 77.0 - cb_pad) & (cb <= 127.0 + cb_pad)
        & (cr >= 133.0 - cr_pad) & (cr <= 173.0 + cr_pad)
    )

    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    rgb_rule = (
        (r > 60.0)
        & (g > 30.0)
        & (b > 10.0)
        & ((max_c - min_c) > 10.0)
        & (r >= g)
        & (r > b)
    )

    return ycbcr_rule & rgb_rule


def _box_blur_axis(values, radius, axis):
    """One separable box-blur pass along ``axis`` using a running sum."""
    np = _np()
    if radius <= 0:
        return values
    window = 2 * radius + 1
    moved = np.moveaxis(values, axis, 0)
    padded = np.concatenate(
        [
            np.repeat(moved[:1], radius, axis=0),
            moved,
            np.repeat(moved[-1:], radius, axis=0),
        ],
        axis=0,
    )
    summed = np.cumsum(padded, axis=0, dtype=np.float64)
    summed = np.concatenate([np.zeros_like(summed[:1]), summed], axis=0)
    window_sums = summed[window:] - summed[:-window]
    blurred = (window_sums / float(window)).astype(np.float32)
    return np.moveaxis(blurred, 0, axis)


def box_blur(values, radius, passes=3):
    """Separable box blur repeated ``passes`` times (approximates gaussian)."""
    np = _np()
    result = np.asarray(values, dtype=np.float32)
    if radius <= 0:
        return result
    for _ in range(max(1, int(passes))):
        result = _box_blur_axis(result, radius, axis=0)
        result = _box_blur_axis(result, radius, axis=1)
    return result


def _binary_box(mask, radius):
    """Box-sum of a boolean mask, used for dilation/erosion."""
    np = _np()
    return box_blur(mask.astype(np.float32), radius, passes=1)


def dilate(mask, radius):
    """Grow a boolean mask by ``radius`` pixels (box structuring element)."""
    if radius <= 0:
        return mask
    return _binary_box(mask, radius) > (0.5 / float((2 * radius + 1) ** 2))


def erode(mask, radius):
    """Shrink a boolean mask by ``radius`` pixels (box structuring element)."""
    if radius <= 0:
        return mask
    return _binary_box(mask, radius) > (1.0 - 0.5 / float((2 * radius + 1) ** 2))


def clean_mask(mask, width, height):
    """Remove speck noise, close small holes, and grow the mask slightly so
    blurred regions fully cover detected skin (including a safety margin)."""
    speck_radius = max(1, int(round(min(width, height) / 320.0)))
    grow_radius = max(2, int(round(min(width, height) / 90.0)))
    cleaned = erode(mask, speck_radius)
    cleaned = dilate(cleaned, speck_radius + grow_radius)
    return cleaned


def feather_mask(mask, radius):
    """Convert a boolean mask into a soft 0..1 alpha with feathered edges."""
    np = _np()
    alpha = mask.astype(np.float32)
    if radius > 0:
        alpha = box_blur(alpha, radius, passes=2)
    return np.clip(alpha, 0.0, 1.0)


def pixelate(rgb, block):
    """Mosaic ``rgb`` (H, W, C) by averaging square blocks of ``block`` px."""
    np = _np()
    rgb = np.asarray(rgb, dtype=np.float32)
    block = max(2, int(block))
    height, width = rgb.shape[0], rgb.shape[1]
    pad_h = (-height) % block
    pad_w = (-width) % block
    padded = np.pad(rgb, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    ph, pw = padded.shape[0], padded.shape[1]
    blocks = padded.reshape(ph // block, block, pw // block, block, -1)
    means = blocks.mean(axis=(1, 3), keepdims=True)
    mosaic = np.broadcast_to(means, blocks.shape).reshape(ph, pw, -1)
    return mosaic[:height, :width]


def filter_frame_rgba(pixel_bytes, width, height, bytes_per_line=None,
                      settings=None):
    """Filter one RGBA frame, obscuring detected skin regions only.

    ``pixel_bytes`` is a raw RGBA8888 buffer (as returned by libopenshot's
    ``Frame.GetPixelsBytes()``). Returns ``(filtered_bytes, coverage)`` where
    coverage is the fraction (0..1) of pixels that were obscured. The buffer
    is returned unchanged (same object) when no skin is detected, and row
    padding from ``bytes_per_line`` is preserved.
    """
    np = _np()
    settings = settings or HaramFilterSettings()
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        return pixel_bytes, 0.0
    stride = int(bytes_per_line or width * 4)

    flat = np.frombuffer(pixel_bytes, dtype=np.uint8)
    if flat.size < height * stride:
        raise ValueError("pixel buffer smaller than height * bytes_per_line")
    rows = flat[: height * stride].reshape(height, stride)
    rgba = rows[:, : width * 4].reshape(height, width, 4)

    skin = detect_skin_mask(rgba[..., :3], settings.sensitivity)
    skin = clean_mask(skin, width, height)
    coverage = float(skin.mean())
    if coverage <= 0.0:
        return pixel_bytes, 0.0

    # Blur radius scales with both the user's blur amount (Tahir's px value)
    # and the frame size, so 1080p and 360p sources obscure equally well.
    scale = max(0.5, min(width, height) / 720.0)
    radius = max(2, int(round(settings.blur_amount * scale * 0.75)))

    # Only process the bounding box around detected skin (plus a margin for
    # the blur kernel and feather), leaving the rest of the frame untouched.
    row_hits = np.flatnonzero(skin.any(axis=1))
    col_hits = np.flatnonzero(skin.any(axis=0))
    margin = radius * 2
    top = max(0, int(row_hits[0]) - margin)
    bottom = min(height, int(row_hits[-1]) + 1 + margin)
    left = max(0, int(col_hits[0]) - margin)
    right = min(width, int(col_hits[-1]) + 1 + margin)

    region = rgba[top:bottom, left:right, :3].astype(np.float32)
    region_mask = skin[top:bottom, left:right]
    if settings.pixelate:
        obscured = pixelate(region, radius * 2)
    else:
        obscured = box_blur(region, radius, passes=3)

    if settings.grayscale:
        gray = (
            0.299 * obscured[..., 0]
            + 0.587 * obscured[..., 1]
            + 0.114 * obscured[..., 2]
        )
        obscured = np.repeat(gray[..., None], 3, axis=2)

    alpha = feather_mask(region_mask, max(1, radius // 3))[..., None]
    composited = region * (1.0 - alpha) + obscured * alpha

    output_rows = rows.copy()
    output_rgba = output_rows[:, : width * 4].reshape(height, width, 4)
    output_rgba[top:bottom, left:right, :3] = np.clip(
        composited + 0.5, 0.0, 255.0).astype(np.uint8)
    # Alpha channel is intentionally untouched (frame integrity)

    if flat.size > height * stride:
        tail = flat[height * stride:]
        return output_rows.tobytes() + tail.tobytes(), coverage
    return output_rows.tobytes(), coverage
