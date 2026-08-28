"""
 @file
 @brief Unit tests for the Windows native Arm64 process/payload
        architecture oracle (design-spec.md G2/G3/G8/G11,
        design-amendment-A1)
 @author OpenShot Studios, LLC

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.
"""

import ctypes
import importlib.util
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

IMAGE_FILE_MACHINE_UNKNOWN = 0x0
IMAGE_FILE_MACHINE_ARM64 = 0xAA64

VALIDATOR_PATH = Path(__file__).resolve().parents[2] / "ci" / "validate_arm64_architecture.py"
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_arm64_architecture", VALIDATOR_PATH
)
if VALIDATOR_SPEC is None or VALIDATOR_SPEC.loader is None:
    raise RuntimeError("Unable to load validator from %s" % VALIDATOR_PATH)
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)


def read_native_process_oracle():
    """
    Query IsWow64Process2 for the current process, per design-amendment-A1:
      - pNativeMachine == IMAGE_FILE_MACHINE_ARM64 (0xAA64) is required for a
        native Arm64 host.
      - pProcessMachine == IMAGE_FILE_MACHINE_UNKNOWN (0x0) is required for a
        process running natively (not under WOW64/emulation). Any nonzero
        pProcessMachine means WOW/emulated execution.

    Returns a dict with the exact observed values. Never fabricates a pass;
    reports "unavailable" with a reason when the platform/API is absent.
    """
    result = {
        "available": False,
        "process_machine": None,
        "native_machine": None,
        "is_wow_or_emulated": None,
        "native_arm64_ok": False,
        "reason": None,
    }

    if not sys.platform.startswith("win"):
        result["reason"] = "IsWow64Process2 is a Windows-only API."
        return result

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.IsWow64Process2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        kernel32.IsWow64Process2.restype = ctypes.c_int

        process_machine = ctypes.c_ushort(0)
        native_machine = ctypes.c_ushort(0)
        handle = kernel32.GetCurrentProcess()
        ok = kernel32.IsWow64Process2(
            handle, ctypes.byref(process_machine), ctypes.byref(native_machine)
        )
        if not ok:
            result["reason"] = (
                "IsWow64Process2 failed (GetLastError=%s); requires "
                "Windows 10 1809 (build 17763) or later."
                % ctypes.get_last_error()
            )
            return result

        result["available"] = True
        result["process_machine"] = process_machine.value
        result["native_machine"] = native_machine.value
        result["is_wow_or_emulated"] = (
            process_machine.value != IMAGE_FILE_MACHINE_UNKNOWN
        )
        result["native_arm64_ok"] = (
            native_machine.value == IMAGE_FILE_MACHINE_ARM64
            and process_machine.value == IMAGE_FILE_MACHINE_UNKNOWN
        )
        return result
    except (AttributeError, OSError) as exc:
        result["reason"] = "IsWow64Process2 call failed: %r" % exc
        return result


class NativeArm64ProcessOracleTests(unittest.TestCase):
    """
    These tests exercise the exact design-amendment-A1 semantics against the
    real, running interpreter. They deliberately do not assert host
    architecture: on this AMD64 development/CI host, native_arm64_ok is
    expected to be False (native_machine == AMD64, not ARM64), and that is
    not a release claim. The tests only assert internal consistency of the
    oracle's derived fields, which is true on every host.
    """

    def test_oracle_reports_consistent_wow_state(self):
        oracle = read_native_process_oracle()
        if not oracle["available"]:
            self.skipTest("IsWow64Process2 unavailable: %s" % oracle["reason"])
        self.assertEqual(
            oracle["is_wow_or_emulated"],
            oracle["process_machine"] != IMAGE_FILE_MACHINE_UNKNOWN,
        )

    def test_native_arm64_ok_requires_both_fields(self):
        oracle = read_native_process_oracle()
        if not oracle["available"]:
            self.skipTest("IsWow64Process2 unavailable: %s" % oracle["reason"])
        expected = (
            oracle["native_machine"] == IMAGE_FILE_MACHINE_ARM64
            and oracle["process_machine"] == IMAGE_FILE_MACHINE_UNKNOWN
        )
        self.assertEqual(oracle["native_arm64_ok"], expected)

    def test_nonzero_process_machine_always_fails_native_arm64_ok(self):
        oracle = read_native_process_oracle()
        if not oracle["available"]:
            self.skipTest("IsWow64Process2 unavailable: %s" % oracle["reason"])
        if oracle["process_machine"] != IMAGE_FILE_MACHINE_UNKNOWN:
            self.assertFalse(oracle["native_arm64_ok"])

    def test_validator_rejects_non_arm64_host_when_required(self):
        observed = {
            "checked": True,
            "process_machine": IMAGE_FILE_MACHINE_UNKNOWN,
            "native_machine": 0x8664,
            "is_wow_or_emulated": False,
            "native_arm64_ok": False,
            "reason": None,
        }
        with mock.patch.object(validator, "read_native_process_oracle", return_value=observed):
            with mock.patch.object(sys, "argv", ["validator", "--require-native-arm64"]):
                with redirect_stdout(StringIO()):
                    self.assertEqual(validator.main(), 1)

    def test_validator_accepts_native_arm64_host_when_required(self):
        observed = {
            "checked": True,
            "process_machine": IMAGE_FILE_MACHINE_UNKNOWN,
            "native_machine": IMAGE_FILE_MACHINE_ARM64,
            "is_wow_or_emulated": False,
            "native_arm64_ok": True,
            "reason": None,
        }
        with mock.patch.object(validator, "read_native_process_oracle", return_value=observed):
            with mock.patch.object(sys, "argv", ["validator", "--require-native-arm64"]):
                with redirect_stdout(StringIO()):
                    self.assertEqual(validator.main(), 0)


if __name__ == "__main__":
    unittest.main()
