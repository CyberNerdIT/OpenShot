"""
 @file
 @brief Background service that produces skin-filtered ("Haram Filter") media files
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

 The service reads a project file frame by frame with libopenshot, runs the
 skin filter from classes.haram_filter over each frame, writes the filtered
 result to a new media file, and imports that file into the project. The
 source file is never modified, so the original stays available.
"""

import copy
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from qt_api import QObject, pyqtSignal, pyqtSlot
from qt_api import QImage

import openshot

from classes import info
from classes.app import get_app
from classes.assets import get_assets_path
from classes.haram_filter import HaramFilterSettings, filter_frame_rgba, SETTING_AUTO_IMPORT
from classes.logger import log
from classes.path_utils import absolute_media_path
from classes.query import File

# Frame methods (in preference order) that can replace a frame's RGBA pixel
# buffer in place. Availability depends on the libopenshot build, so each is
# probed at runtime.
PIXEL_SETTER_NAMES = ("SetPixelsBytes", "AddPixelsBytes", "SetPixels")

# Data key stored on imported filtered files, so they are never re-filtered
FILTERED_SOURCE_KEY = "haram_filter_source"


class _FilterJobCanceled(RuntimeError):
    """Raised when a queued/running filter job is canceled."""


def frame_pixel_setter(frame):
    """Return the first available pixel write-back method on a frame."""
    for name in PIXEL_SETTER_NAMES:
        setter = getattr(frame, name, None)
        if callable(setter):
            return setter
    return None


class HaramFilterService(QObject):
    """Runs skin-filter jobs in the background (modeled on ProxyService)."""

    filter_generated = pyqtSignal(str, str, str)  # file_id, output_path, error
    file_job_changed = pyqtSignal(str)
    queue_changed = pyqtSignal()
    job_updated = pyqtSignal(str, str, int)
    job_finished = pyqtSignal(str, str)
    ACTIVE_STATES = ("queued", "running", "canceling")
    CACHE_CLEAR_INTERVAL = 120
    CACHE_MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._jobs = {}
        self._importing_output = False
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="haram-filter",
        )
        self.filter_generated.connect(self._on_filter_generated)

    def shutdown(self):
        if getattr(self, "_executor", None):
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def filter_files(self, files):
        """Queue skin-filter jobs for the given project files."""
        files = [f for f in (files or []) if getattr(f, "id", None)]
        files = [f for f in files if self.is_filterable(f)]
        if not files:
            return

        os.makedirs(self._output_root(), exist_ok=True)
        submitted = 0
        skipped = 0
        for file_obj in files:
            file_id = str(file_obj.id or "")
            if not file_id:
                continue
            if self.get_active_job_for_file(file_id):
                log.info("Haram Filter filter_files file_id=%s skipped: job already active", file_id)
                skipped += 1
                continue
            snapshot = copy.deepcopy(file_obj.data or {})
            output_path = self._output_path(file_id, snapshot)
            with self._lock:
                self._jobs[file_id] = {
                    "id": file_id,
                    "status": "queued",
                    "progress": 0,
                    "cancel_requested": False,
                    "output_path": output_path,
                }
            future = self._executor.submit(self._build_filtered_file, file_id, snapshot)
            with self._lock:
                if file_id in self._jobs:
                    self._jobs[file_id]["future"] = future
            future.add_done_callback(lambda fut, fid=file_id: self._emit_filter_result(fid, fut))
            self._emit_job_change(file_id)
            submitted += 1

        if submitted:
            status = "Haram Filter: filtering {} item(s)".format(submitted)
            if skipped:
                status += ", skipped {}".format(skipped)
            self._show_status(status)
        elif skipped:
            self._show_status("Haram Filter: skipped {} item(s)".format(skipped), 3000)

    def maybe_auto_filter(self, files):
        """Filter newly imported files when the auto-import preference is on."""
        if self._importing_output:
            # A filtered output is being imported; never re-filter it
            return
        app = get_app()
        settings = app.get_settings() if app else None
        try:
            enabled = bool(settings and settings.get(SETTING_AUTO_IMPORT))
        except Exception:
            enabled = False
        if not enabled:
            return
        self.filter_files([f for f in (files or []) if self.is_filterable(f)])

    def cancel_for_files(self, files):
        files = [f for f in (files or []) if getattr(f, "id", None)]
        canceled = 0
        for file_obj in files:
            if self.cancel_job(file_obj.id):
                canceled += 1
        if canceled:
            self._show_status("Haram Filter: canceled {} item(s)".format(canceled), 3000)
            return True
        return False

    def cancel_job(self, file_id):
        file_id = str(file_id or "")
        if not file_id:
            return False
        with self._lock:
            job = self._jobs.get(file_id)
            if not job:
                return False
            status = str(job.get("status") or "")
            future = job.get("future")
            if status == "queued" and future and future.cancel():
                self._finalize_job(file_id, "canceled")
                return True
            if status in ("queued", "running"):
                job["cancel_requested"] = True
                job["status"] = "canceling"
            elif status == "canceling":
                return True
            else:
                return False
        self._emit_job_change(file_id)
        return True

    @staticmethod
    def is_filterable(file_obj):
        """Return whether a project file can be skin-filtered."""
        data = getattr(file_obj, "data", {}) if file_obj else {}
        if not isinstance(data, dict):
            return False
        if data.get(FILTERED_SOURCE_KEY):
            # Already the output of a filter job
            return False
        media_type = str(data.get("media_type", "") or "").strip().lower()
        return media_type in ("video", "image")

    @staticmethod
    def is_filtered_output(file_obj):
        data = getattr(file_obj, "data", {}) if file_obj else {}
        return isinstance(data, dict) and bool(data.get(FILTERED_SOURCE_KEY))

    def has_filtered_output(self, file_obj):
        """Return whether the project already has a filtered copy of a file."""
        file_id = str(getattr(file_obj, "id", "") or "")
        if not file_id:
            return False
        for other in File.filter():
            data = getattr(other, "data", {}) or {}
            if str(data.get(FILTERED_SOURCE_KEY) or "") == file_id:
                return True
        return False

    def get_active_job_for_file(self, file_id):
        file_id = str(file_id or "")
        if not file_id:
            return None
        with self._lock:
            job = self._job_snapshot(self._jobs.get(file_id))
        if not job or job.get("status") not in self.ACTIVE_STATES:
            return None
        return job

    # ------------------------------------------------------------------
    # Job implementation (worker thread)
    # ------------------------------------------------------------------

    def _build_filtered_file(self, file_id, file_data):
        source_path = absolute_media_path(file_data.get("path"))
        if not source_path or not os.path.exists(source_path):
            raise RuntimeError("source file not found")
        media_type = str(file_data.get("media_type", "")).lower()
        if media_type not in ("video", "image"):
            raise RuntimeError("only video and image files can be filtered")

        self._mark_running(file_id)
        settings = HaramFilterSettings.from_app_settings(
            get_app().get_settings() if get_app() else None)
        output_path = self._reserved_output_path(file_id, file_data)
        try:
            if media_type == "image":
                self._filter_image(source_path, output_path, settings, file_id)
            else:
                self._filter_video(source_path, output_path, settings, file_id)
        except Exception:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            raise
        return output_path

    def _filter_image(self, source_path, output_path, settings, file_id):
        clip = openshot.Clip(source_path)
        try:
            clip.Open()
            frame = clip.Reader().GetFrame(1)
            filtered, _coverage = self._filtered_frame_bytes(frame, settings)
            width = int(frame.GetWidth())
            height = int(frame.GetHeight())
            bytes_per_line = int(frame.GetBytesPerLine())
            image = QImage(
                filtered,
                width,
                height,
                bytes_per_line,
                QImage.Format_RGBA8888_Premultiplied,
            ).copy()
            if not image.save(output_path, "PNG"):
                raise RuntimeError("unable to save filtered image")
            self._update_progress(file_id, 100)
        finally:
            try:
                clip.Close()
            except Exception:
                pass

    def _filter_video(self, source_path, output_path, settings, file_id):
        clip = openshot.Clip(source_path)
        clip.Open()
        try:
            reader = clip.Reader()
            self._configure_cache(clip, reader)
            source_reader = json.loads(reader.Json())

            width = int(source_reader.get("width", 0) or 0)
            height = int(source_reader.get("height", 0) or 0)
            if width <= 0 or height <= 0:
                raise RuntimeError("invalid source dimensions")
            max_frame = int(source_reader.get("video_length", 0) or 0)
            if max_frame <= 0:
                raise RuntimeError("invalid source frame count")

            # Determine if this libopenshot build lets us write filtered
            # pixels back into the source frame (which keeps its audio).
            can_write_back = frame_pixel_setter(openshot.Frame()) is not None
            include_audio = bool(source_reader.get("has_audio")) and can_write_back
            if source_reader.get("has_audio") and not can_write_back:
                log.warning(
                    "Haram Filter: this libopenshot build has no frame pixel "
                    "write-back method; the filtered file will not include audio")

            fps = source_reader.get("fps", {"num": 30, "den": 1})
            pixel_ratio = source_reader.get("pixel_ratio", {"num": 1, "den": 1})
            writer = openshot.FFmpegWriter(output_path)
            writer.SetVideoOptions(
                True,
                "libx264",
                openshot.Fraction(int(fps.get("num", 30)), int(fps.get("den", 1))),
                width,
                height,
                openshot.Fraction(int(pixel_ratio.get("num", 1)), int(pixel_ratio.get("den", 1))),
                False,
                False,
                22,
            )
            writer.PrepareStreams()
            if include_audio:
                channels = int(source_reader.get("channels", 2) or 2)
                channel_layout = openshot.LAYOUT_STEREO if channels > 1 else openshot.LAYOUT_MONO
                writer.SetAudioOptions(
                    True,
                    "aac",
                    int(source_reader.get("sample_rate", 48000) or 48000),
                    2 if channel_layout == openshot.LAYOUT_STEREO else 1,
                    channel_layout,
                    192000,
                )
                writer.PrepareStreams()

            writer.Open()
            try:
                for frame_number in range(1, max_frame + 1):
                    self._raise_if_canceled(file_id)
                    frame = reader.GetFrame(frame_number)
                    output_frame = self._filter_frame(frame, settings, can_write_back)
                    writer.WriteFrame(output_frame)
                    if frame_number % self.CACHE_CLEAR_INTERVAL == 0:
                        self._clear_cache(clip, reader)
                    if (frame_number == 1 or frame_number == max_frame
                            or frame_number % max(1, min(12, max_frame // 40 or 1)) == 0):
                        self._update_progress(
                            file_id,
                            int((float(frame_number) / float(max_frame)) * 100.0))
            finally:
                writer.Close()
                self._clear_cache(clip, reader)
        finally:
            try:
                clip.Close()
            except Exception:
                pass

    def _filter_frame(self, frame, settings, can_write_back):
        """Return a frame whose skin regions are obscured."""
        filtered, coverage = self._filtered_frame_bytes(frame, settings)
        if coverage <= 0.0:
            return frame

        if can_write_back:
            setter = frame_pixel_setter(frame)
            if setter is not None:
                setter(filtered)
                return frame

        # Fallback: round-trip through a PNG + QtImageReader (video only)
        width = int(frame.GetWidth())
        height = int(frame.GetHeight())
        bytes_per_line = int(frame.GetBytesPerLine())
        image = QImage(
            filtered,
            width,
            height,
            bytes_per_line,
            QImage.Format_RGBA8888_Premultiplied,
        ).copy()
        temp_path = os.path.join(
            self._output_root(), ".haram-filter-frame.png")
        if not image.save(temp_path, "PNG"):
            raise RuntimeError("unable to save intermediate filtered frame")
        image_reader = openshot.QtImageReader(temp_path)
        try:
            image_reader.Open()
            return image_reader.GetFrame(1)
        finally:
            try:
                image_reader.Close()
            except Exception:
                pass

    @staticmethod
    def _filtered_frame_bytes(frame, settings):
        width = int(frame.GetWidth())
        height = int(frame.GetHeight())
        bytes_per_line = int(frame.GetBytesPerLine())
        pixels = frame.GetPixelsBytes()
        if not pixels or width <= 0 or height <= 0 or bytes_per_line <= 0:
            raise RuntimeError("unable to read frame pixels")
        return filter_frame_rgba(pixels, width, height, bytes_per_line, settings)

    # ------------------------------------------------------------------
    # Result handling (GUI thread)
    # ------------------------------------------------------------------

    def _emit_filter_result(self, file_id, future):
        error_text = ""
        output_path = ""
        try:
            if future.cancelled():
                raise _FilterJobCanceled()
            output_path = future.result()
        except _FilterJobCanceled:
            error_text = "__canceled__"
        except Exception as ex:
            error_text = str(ex)
            log.warning("Haram Filter failed for %s: %s", file_id, error_text, exc_info=1)
        self.filter_generated.emit(str(file_id or ""), str(output_path or ""), error_text)

    @pyqtSlot(str, str, str)
    def _on_filter_generated(self, file_id, output_path, error_text):
        file_id = str(file_id or "")
        if error_text == "__canceled__":
            self._finalize_job(file_id, "canceled")
            return
        if error_text:
            self._finalize_job(file_id, "failed")
            self._show_status("Haram Filter: {}".format(error_text), 5000)
            return
        try:
            self._import_filtered_file(file_id, output_path)
        except Exception as ex:
            log.warning("Haram Filter import failed for %s: %s", output_path, ex, exc_info=1)
        self._finalize_job(file_id, "completed")
        self._show_status("Haram Filter: ready", 3000)

    def _import_filtered_file(self, file_id, output_path):
        """Add the filtered file to the project and tag it with its source."""
        files_model = getattr(self.win, "files_model", None)
        if files_model is None or not output_path or not os.path.exists(output_path):
            return
        self._importing_output = True
        try:
            files_model.add_files(
                [output_path],
                quiet=True,
                prevent_image_seq=True,
                prevent_recent_folder=True,
            )
        finally:
            self._importing_output = False
        imported = File.get(path=output_path)
        if imported:
            imported.data[FILTERED_SOURCE_KEY] = str(file_id or "")
            imported.save()

    # ------------------------------------------------------------------
    # Paths and bookkeeping
    # ------------------------------------------------------------------

    def _output_root(self):
        app = get_app()
        project = getattr(app, "project", None) if app else None
        current_filepath = getattr(project, "current_filepath", None) if project else None
        if current_filepath:
            current_abs = os.path.abspath(str(current_filepath))
            backup_abs = os.path.abspath(info.BACKUP_FILE)
            recovery_abs = os.path.abspath(info.RECOVERY_PATH) + os.sep
            if current_abs == backup_abs or current_abs.startswith(recovery_abs):
                return info.HARAM_FILTER_PATH
            return os.path.join(get_assets_path(current_filepath), "filtered")
        return info.HARAM_FILTER_PATH

    def _output_path(self, file_id, file_data):
        output_root = self._output_root()
        os.makedirs(output_root, exist_ok=True)
        source_path = absolute_media_path((file_data or {}).get("path"))
        source_stem = os.path.splitext(os.path.basename(source_path or ""))[0] or "media"
        media_type = str((file_data or {}).get("media_type", "")).lower()
        extension = ".png" if media_type == "image" else ".mp4"
        default_name = "{}_filtered{}".format(source_stem, extension)
        default_path = os.path.join(output_root, default_name)
        if not os.path.exists(default_path):
            return default_path
        return os.path.join(
            output_root,
            "{}_filtered_{}{}".format(source_stem, str(file_id or ""), extension))

    def _reserved_output_path(self, file_id, file_data):
        with self._lock:
            job = self._jobs.get(str(file_id or ""))
            reserved = job.get("output_path") if isinstance(job, dict) else None
        return reserved or self._output_path(file_id, file_data)

    def _show_status(self, text, timeout=5000):
        status_bar = getattr(self.win, "statusBar", None)
        if status_bar and hasattr(status_bar, "showMessage"):
            status_bar.showMessage(str(text), int(timeout))

    def _configure_cache(self, clip, reader):
        for cache_object in (
            getattr(clip, "GetCache", lambda: None)(),
            getattr(reader, "GetCache", lambda: None)(),
        ):
            if not cache_object:
                continue
            try:
                cache_object.SetMaxBytes(int(self.CACHE_MAX_BYTES))
            except Exception:
                log.debug("Haram Filter cache max-bytes update failed", exc_info=1)
            try:
                cache_object.Clear()
            except Exception:
                log.debug("Haram Filter cache clear failed", exc_info=1)

    @staticmethod
    def _clear_cache(clip, reader):
        for cache_object in (
            getattr(reader, "GetCache", lambda: None)(),
            getattr(clip, "GetCache", lambda: None)(),
        ):
            if not cache_object:
                continue
            try:
                cache_object.Clear()
            except Exception:
                log.debug("Haram Filter cache clear failed", exc_info=1)

    def _mark_running(self, file_id):
        with self._lock:
            job = self._jobs.get(str(file_id or ""))
            if not job:
                return
            if job.get("cancel_requested"):
                job["status"] = "canceling"
            else:
                job["status"] = "running"
            job["progress"] = max(1, int(job.get("progress", 0)))
        self._emit_job_change(file_id)

    def _update_progress(self, file_id, progress):
        with self._lock:
            job = self._jobs.get(str(file_id or ""))
            if not job:
                return
            if job.get("status") not in self.ACTIVE_STATES:
                return
            job["progress"] = max(0, min(100, int(progress)))
        self._emit_job_change(file_id)

    def _raise_if_canceled(self, file_id):
        with self._lock:
            job = self._jobs.get(str(file_id or ""))
            if job and job.get("cancel_requested"):
                raise _FilterJobCanceled()

    def _finalize_job(self, file_id, status):
        file_id = str(file_id or "")
        with self._lock:
            job = self._jobs.pop(file_id, None)
        if status == "completed" or job is not None:
            self.job_finished.emit(file_id, status)
        self._emit_job_change(file_id)

    def _emit_job_change(self, file_id):
        file_id = str(file_id or "")
        if not file_id:
            return
        job = self.get_active_job_for_file(file_id)
        if job:
            self.job_updated.emit(file_id, str(job.get("status") or ""), int(job.get("progress", 0)))
        self.file_job_changed.emit(file_id)
        self.queue_changed.emit()

    @staticmethod
    def _job_snapshot(job):
        if not isinstance(job, dict):
            return None
        return {
            "id": str(job.get("id") or ""),
            "status": str(job.get("status") or ""),
            "progress": int(job.get("progress", 0)),
            "cancel_requested": bool(job.get("cancel_requested")),
        }
