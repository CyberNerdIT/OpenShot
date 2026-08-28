import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

import validate_arm64_architecture as validator


def write_pe(path, machine):
    data = bytearray(0x80)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, machine)
    with open(path, "wb") as stream:
        stream.write(data)


class Arm64ArchitectureValidatorTests(unittest.TestCase):
    def test_payload_scan_accepts_arm64_and_rejects_amd64(self):
        with tempfile.TemporaryDirectory() as root:
            write_pe(os.path.join(root, "native.dll"), validator.IMAGE_FILE_MACHINE_ARM64)
            results, failures = validator.scan_payload_architecture(root)
            self.assertEqual(len(results), 1)
            self.assertEqual(failures, [])

            write_pe(os.path.join(root, "foreign.pyd"), validator.IMAGE_FILE_MACHINE_AMD64)
            results, failures = validator.scan_payload_architecture(root)
            self.assertEqual(len(results), 2)
            self.assertEqual(len(failures), 1)

    def test_required_native_host_fails_closed(self):
        oracle = {
            "checked": True,
            "process_machine": validator.IMAGE_FILE_MACHINE_UNKNOWN,
            "native_machine": validator.IMAGE_FILE_MACHINE_AMD64,
            "is_wow_or_emulated": False,
            "native_arm64_ok": False,
            "reason": None,
        }
        with mock.patch.object(validator, "read_native_process_oracle", return_value=oracle):
            with mock.patch.object(sys, "argv", ["validator", "--require-native-arm64"]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(validator.main(), 1)


if __name__ == "__main__":
    unittest.main()
