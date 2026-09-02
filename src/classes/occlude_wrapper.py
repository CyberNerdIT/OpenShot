"""
 @file
 @brief Integration with OCCLUDE, blurring immodestly dressed people in
        exported videos (https://github.com/CyberNerdIT/Occlude)

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

import importlib.util
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time

from classes.logger import log

# Machine-readable progress lines emitted by `occlude` when the
# OCCLUDE_MACHINE_PROGRESS env var is set, e.g.:
#   OCCLUDE-PROGRESS {"stage": "Pass 3/3 render", "done": 120, "total": 4000}
PROGRESS_PREFIX = "OCCLUDE-PROGRESS "

# How each OCCLUDE pipeline stage maps onto one overall 0.0-1.0 progress bar.
# Rough wall-clock weights: detection dominates pass 1, the VLM dominates
# pass 2, and SAM2 + re-encode dominate pass 3.
_STAGE_SPANS = [
    ("collect", (0.40, 0.45)),
    ("judge", (0.45, 0.60)),
    ("1/3", (0.00, 0.40)),
    ("3/3", (0.60, 1.00)),
]


def _split_command(command_line):
    """Split a custom command line into an argument list.

    On Windows, posix-mode shlex would eat the backslashes in paths like
    C:\\Python39\\python.exe, so split in non-posix mode there and strip the
    surrounding quotes it leaves on quoted tokens.
    """
    if os.name == "nt":
        args = shlex.split(command_line, posix=False)
        return [
            a[1:-1] if len(a) > 1 and a[0] == '"' and a[-1] == '"' else a
            for a in args
        ]
    return shlex.split(command_line)


def _windows_deps_root():
    """%LOCALAPPDATA%\\OpenShot-OCCLUDE — where install-occlude-deps.ps1
    puts the private occlude environment and bundled ffmpeg (or None off
    Windows)."""
    if os.name != "nt":
        return None
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if not base:
        return None
    return os.path.join(base, "OpenShot-OCCLUDE")


def find_occlude():
    """Locate the OCCLUDE command, or None when it is not installed.

    Resolution order: the OCCLUDE_COMMAND env var (a full command line, for
    custom installs), the `occlude` executable on PATH, the private
    environment created by installer/install-occlude-deps.ps1 on Windows,
    then `python -m occlude` when the package is importable by our
    interpreter. Returns the command as an argument list.
    """
    custom = os.environ.get("OCCLUDE_COMMAND", "").strip()
    if custom:
        return _split_command(custom)
    exe = shutil.which("occlude")
    if exe:
        return [exe]
    deps_root = _windows_deps_root()
    if deps_root:
        venv_exe = os.path.join(deps_root, "venv", "Scripts", "occlude.exe")
        if os.path.exists(venv_exe):
            return [venv_exe]
    try:
        if importlib.util.find_spec("occlude") is not None:
            return [sys.executable, "-m", "occlude"]
    except Exception:
        pass
    return None


def _subprocess_env():
    """Environment for the occlude subprocess: machine progress on, and the
    dependency installer's bin folder (bundled ffmpeg) on PATH."""
    env = dict(os.environ)
    env["OCCLUDE_MACHINE_PROGRESS"] = "1"
    # Force UTF-8 in the child: on Windows a piped stdout defaults to the
    # legacy ANSI code page (cp1252), and occlude's Unicode output (rich
    # banner, tqdm bars) dies with UnicodeEncodeError on it.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    deps_root = _windows_deps_root()
    if deps_root:
        bin_dir = os.path.join(deps_root, "bin")
        if os.path.isdir(bin_dir):
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def blurred_output_path(export_path):
    """Path the blurred copy of an export is written to (always .mp4)."""
    base, _ext = os.path.splitext(export_path)
    return "%s_occluded.mp4" % base


def parse_progress_line(line):
    """Parse one OCCLUDE-PROGRESS stdout line into its payload dict.

    Returns {"stage", "done", "total"} or None for any other line —
    subprocess output must never be able to raise here.
    """
    line = line.strip()
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_PREFIX):])
    except ValueError:
        return None
    if not isinstance(payload, dict) or "stage" not in payload:
        return None
    return payload


def overall_fraction(payload):
    """Map one stage progress payload onto overall 0.0-1.0 completion."""
    stage = str(payload.get("stage", ""))
    done = payload.get("done") or 0
    total = payload.get("total")
    for key, (start, end) in _STAGE_SPANS:
        if key in stage:
            if not total or total <= 0:
                return start
            return start + (end - start) * min(float(done) / total, 1.0)
    return None


def run_occlude(input_path, output_path, progress_callback=None,
                cancel_check=None, status_callback=None):
    """Run OCCLUDE on a video file, blocking until done or cancelled.

    progress_callback(fraction, stage) is invoked as machine progress lines
    arrive (fraction is overall 0.0-1.0); status_callback(line) receives
    every other output line, so a UI can show model downloads and pass
    banners while no frame progress exists yet. cancel_check() is polled a
    few times per second; returning True terminates the subprocess. All run
    on the caller's thread, so a Qt caller can pump events from them.

    Returns (success, message): message is a human-readable error (or the
    cancellation note) when success is False.
    """
    command = find_occlude()
    if not command:
        return False, "OCCLUDE is not installed (pip install occlude)"

    args = command + ["--input", input_path, "--output", output_path]
    env = _subprocess_env()
    log.info("Launching OCCLUDE: %s" % " ".join(args))
    popen_kwargs = {}
    if os.name == "nt":
        # Without this, a console subprocess of the GUI app pops up an empty
        # console window (its output goes to our pipe, not that window).
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, **popen_kwargs)
    except OSError as ex:
        return False, "Could not launch OCCLUDE: %s" % ex

    # A reader thread splits the merged output on both \n and \r (tqdm bars
    # animate with bare carriage returns) and queues decoded lines, so this
    # thread can keep polling cancel_check instead of blocking on a read.
    lines = queue.Queue()

    def _reader():
        try:
            pending = b""
            while True:
                chunk = process.stdout.read1(4096)
                if not chunk:
                    break
                pending += chunk
                *complete, pending = pending.replace(b"\r", b"\n").split(b"\n")
                for raw in complete:
                    if raw.strip():
                        lines.put(raw.decode("utf-8", "replace"))
            if pending.strip():
                lines.put(pending.decode("utf-8", "replace"))
        finally:
            lines.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    tail = []  # last output lines, for the error message on failure
    cancelled = False
    cancel_time = None
    finished = False
    while not finished:
        if not cancelled and cancel_check and cancel_check():
            cancelled = True
            cancel_time = time.time()
            log.info("OCCLUDE cancelled by user, terminating")
            process.terminate()
        elif cancelled and time.time() - cancel_time > 5 and process.poll() is None:
            process.kill()
        try:
            line = lines.get(timeout=0.25)
        except queue.Empty:
            continue
        if line is None:
            finished = True
            continue
        payload = parse_progress_line(line)
        if payload is not None:
            if progress_callback:
                fraction = overall_fraction(payload)
                if fraction is not None:
                    progress_callback(fraction, payload.get("stage"))
        else:
            log.info("OCCLUDE: %s" % line)
            tail = (tail + [line])[-15:]
            if status_callback:
                status_callback(line)

    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    finally:
        process.stdout.close()

    if cancelled:
        return False, "Cancelled"
    if process.returncode != 0:
        return False, "\n".join(tail) or (
            "OCCLUDE exited with code %s" % process.returncode)
    if not os.path.exists(output_path):
        return False, "OCCLUDE finished but wrote no output file"
    return True, ""
