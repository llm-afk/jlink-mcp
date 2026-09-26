"""Hardware-facing regressions using mocks only; no J-Link DLL is loaded."""

import types
import unittest
from unittest.mock import Mock, call, patch

import pylink

from jlink_mcp.exceptions import JLinkErrorCode
from jlink_mcp.tools import debug, flash, memory


class HardwareToolsTests(unittest.TestCase):
    def setUp(self):
        self.jlink = Mock()
        self.jlink.halted.return_value = True
        self.manager_patch = patch.object(memory.jlink_manager, "get_jlink", return_value=self.jlink)
        self.get_jlink = self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)

    def test_range_erase_never_calls_hardware(self):
        for options in (
            {"start_address": 0x08010000, "end_address": 0x08010400},
            {"start_address": 0x08010000},
            {"end_address": 0x08010400},
            {"start_address": 0x08010000, "end_address": 0x08010400, "chip_erase": True},
            {},
        ):
            with self.subTest(options=options):
                result = flash.erase_flash(**options)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"]["code"], JLinkErrorCode.INVALID_PARAMETER.code)
                self.assertIn("erase_sector", result["error"]["detail"])
        self.get_jlink.assert_not_called()
        self.jlink.erase.assert_not_called()

    def test_explicit_chip_erase_is_allowed(self):
        result = flash.erase_flash(chip_erase=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["erase_type"], "chip")
        self.jlink.erase.assert_called_once_with()

    def test_sector_erase_rejects_empty_or_short_readback(self):
        for readback in ([], [0xFF] * 3):
            with self.subTest(length=len(readback)):
                self.jlink.memory_read.return_value = readback
                result = flash.erase_sector(0x08000000, page_size=4)
                self.assertFalse(result["success"])

    def test_sector_erase_requires_erased_contents(self):
        self.jlink.memory_read.return_value = [0xFF] * 4
        self.assertTrue(flash.erase_sector(0x08000000, page_size=4)["success"])
        self.jlink.memory_read.return_value = [0xFF, 0x00, 0xFF, 0xFF]
        self.assertFalse(flash.erase_sector(0x08000000, page_size=4)["success"])

    def test_invalid_sector_ranges_do_not_touch_hardware(self):
        for address, count, page_size in (
            (0x08000000, 0, 1024), (0x08000000, True, 1024),
            (0x08000000, 1.0, 1024), (0x08000000, 1, 0),
            (0x08000000, 1, True), (0x08000000, 1, 1024.0),
            (0x08000000, 1, 3), (0xFFFFFFFF, 2, 1024),
            (0x08000000, 1 << 32, 1024), (True, 1, 1024),
            (0x100000000, 1, 1024),
        ):
            with self.subTest(address=address, count=count, page_size=page_size):
                result = flash.erase_sector(address, count, page_size)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"]["code"], JLinkErrorCode.INVALID_PARAMETER.code)
        self.get_jlink.assert_not_called()

    def test_verify_counts_missing_bytes_and_bounds_details(self):
        result = flash._build_verify_result(bytes(200), b"", 0x08000000)
        self.assertFalse(result["matched"])
        self.assertEqual(result["mismatch_count"], 200)
        self.assertEqual(len(result["mismatches"]), 100)
        self.assertIsNone(result["mismatches"][0]["actual"])
        self.assertTrue(result["truncated"])

    def test_program_rejects_short_readback(self):
        self.jlink.memory_read.return_value = [0x12]
        result = flash.program_flash(0x08000000, "1234")
        self.assertFalse(result["success"])
        self.assertEqual(result["bytes_programmed"], 2)

    def test_program_verification_mismatch_is_failure(self):
        self.jlink.memory_read.return_value = [0x00]
        result = flash.program_flash(0x08000000, "FF")
        self.assertFalse(result["success"])
        self.assertEqual(result["bytes_programmed"], 1)
        self.assertFalse(result["verify_result"]["matched"])
        self.assertEqual(result["error"]["code"], JLinkErrorCode.VERIFY_FAILED.code)

    def test_flash_readback_is_chunked(self):
        self.jlink.memory_read.side_effect = lambda address, count, nbits: [0xFF] * count
        data = flash._read_flash_bytes(self.jlink, 0x08000000, 65540)
        self.assertEqual(data, b"\xff" * 65540)
        self.assertEqual(self.jlink.memory_read.call_args_list, [
            call(0x08000000, 65536, nbits=8), call(0x08010000, 4, nbits=8)
        ])

    def test_empty_firmware_is_rejected_before_hardware(self):
        result = flash.program_flash(0x08000000, "   ")
        self.assertFalse(result["success"])
        self.get_jlink.assert_not_called()

    def test_ambiguous_firmware_source_is_rejected_before_file_access(self):
        with patch("builtins.open") as open_file:
            result = flash.program_flash(0x08000000, data="1234", file_path="firmware.bin")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], JLinkErrorCode.INVALID_PARAMETER.code)
        open_file.assert_not_called()
        self.get_jlink.assert_not_called()

    def test_memory_read_honors_width_and_keeps_byte_contract(self):
        for width, values in ((8, [0x12, 0x34, 0x56, 0x78]),
                              (16, [0x3412, 0x7856]), (32, [0x78563412])):
            with self.subTest(width=width):
                self.jlink.memory_read.return_value = values
                result = memory.read_memory(0x20000000, 4, width)
                self.assertTrue(result["success"])
                self.assertEqual(result["data"], [0x12, 0x34, 0x56, 0x78])
                self.jlink.memory_read.assert_called_with(0x20000000, 4 // (width // 8), nbits=width)

    def test_memory_write_honors_width_and_byte_order(self):
        self.jlink.memory_write.return_value = 4
        for width, values in ((8, [0x12, 0x34, 0x56, 0x78]),
                              (16, [0x3412, 0x7856]), (32, [0x78563412])):
            with self.subTest(width=width):
                result = memory.write_memory(0x20000000, "12345678", width)
                self.assertTrue(result["success"])
                self.assertEqual(result["bytes_written"], 4)
                self.jlink.memory_write.assert_called_with(0x20000000, values, nbits=width)

    def test_invalid_memory_access_fails_before_hardware(self):
        for address, size, width in ((0x20000001, 4, 32), (0x20000000, 3, 32),
                                      (0x20000000, 4, 0), (-4, 4, 32)):
            with self.subTest(address=address, size=size, width=width):
                self.assertFalse(memory.read_memory(address, size, width)["success"])
                self.assertFalse(memory.write_memory(address, "00" * size, width)["success"])
        self.get_jlink.assert_not_called()

    def test_memory_read_and_write_reject_incomplete_transfers(self):
        self.jlink.memory_read.return_value = []
        self.assertFalse(memory.read_memory(0x20000000, 4, 32)["success"])
        self.jlink.memory_write.return_value = 2
        result = memory.write_memory(0x20000000, "12345678", 32)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], JLinkErrorCode.WRITE_FAILED.code)

    def test_memory_access_rejects_invalid_types_and_address_overflow(self):
        for address, size, width in (
            (0x20000000, True, 8), (0x20000000, 4.0, 32),
            (0x20000000, "4", 32), (0x20000000, 4, 32.0),
            (0x20000000, 4, True), (True, 4, 32),
            (0x100000000, 4, 32), (0xFFFFFFFC, 8, 32),
        ):
            with self.subTest(address=address, size=size, width=width):
                result = memory.read_memory(address, size, width)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"]["code"], JLinkErrorCode.INVALID_PARAMETER.code)
        self.get_jlink.assert_not_called()
        self.assertEqual(memory._validate_memory_access(0xFFFFFFFC, 4, 32), 4)

    def test_write_memory_rejects_address_overflow(self):
        result = memory.write_memory(0xFFFFFFFC, "00" * 8, 32)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], JLinkErrorCode.INVALID_PARAMETER.code)
        self.get_jlink.assert_not_called()

    def test_core_reset_restores_previous_strategy(self):
        self.jlink.set_reset_strategy.return_value = 7
        self.assertTrue(debug.reset_target("core")["success"])
        self.assertTrue(debug.reset_target("normal")["success"])
        self.assertEqual(self.jlink.method_calls, [
            call.set_reset_strategy(pylink.JLinkResetStrategyCortexM3.CORE),
            call.reset(ms=0, halt=True), call.set_reset_strategy(7),
            call.reset(ms=0, halt=False)
        ])

    def test_core_reset_restores_strategy_even_on_failure(self):
        self.jlink.set_reset_strategy.return_value = 7
        self.jlink.reset.side_effect = RuntimeError("reset failed")
        self.assertFalse(debug.reset_target("core")["success"])
        self.jlink.set_reset_strategy.assert_has_calls([
            call(pylink.JLinkResetStrategyCortexM3.CORE), call(7)
        ])

    def test_unsupported_core_strategy_does_not_fall_back_to_reset(self):
        self.jlink.set_reset_strategy.side_effect = RuntimeError("unsupported")
        self.assertFalse(debug.reset_target("core")["success"])
        self.jlink.reset.assert_not_called()

    def test_invalid_reset_type_does_not_touch_hardware(self):
        result = debug.reset_target("typo")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], JLinkErrorCode.INVALID_PARAMETER.code)
        self.get_jlink.assert_not_called()

    def test_register_aliases(self):
        self.jlink.register_read.side_effect = [-1, 0x20001000, 0x08000101]
        result = memory.read_registers(["PC", "sp", "LR"])
        self.assertTrue(result["success"])
        self.assertEqual(result["registers"][0]["value"], 0xFFFFFFFF)
        self.assertEqual(self.jlink.register_read.call_args_list, [
            call("R15 (PC)"), call("R13 (SP)"), call("R14")
        ])
        self.assertTrue(memory.write_register("pc", 0x08000000)["success"])
        self.jlink.register_write.assert_called_once_with("R15 (PC)", 0x08000000)

    def test_partial_register_read_reports_failed_names(self):
        self.jlink.register_read.side_effect = [1, ValueError("unknown register")]
        result = memory.read_registers(["R0", "bad"])
        self.assertFalse(result["success"])
        self.assertEqual(result["registers"], [{"name": "R0", "value": 1}])
        self.assertEqual(result["errors"][0]["name"], "bad")

    def test_non_alias_register_name_preserves_driver_case(self):
        self.assertTrue(memory.write_register("xPSR", 1)["success"])
        self.jlink.register_write.assert_called_once_with("xPSR", 1)

    def test_register_write_rejects_non_u32_values(self):
        for value in (-1, 0x100000000, True, 1.0, "1"):
            with self.subTest(value=value):
                result = memory.write_register("R0", value)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"]["code"], JLinkErrorCode.INVALID_PARAMETER.code)
        self.get_jlink.assert_not_called()
        self.assertTrue(memory.write_register("R0", 0xFFFFFFFF)["success"])

    def test_empty_register_request_is_rejected_without_halt(self):
        self.assertFalse(memory.read_registers([])["success"])
        self.get_jlink.assert_not_called()


class PyLinkBoundaryTests(unittest.TestCase):
    def test_real_pylink_translates_access_width_to_dll(self):
        # Use the actual PyLink methods against a Python object standing in for
        # its DLL. Constructing pylink.JLink() would load a native DLL, so avoid it.
        dll = Mock()
        backend = types.SimpleNamespace(_dll=dll, target_connected=lambda: True)
        adapter = types.SimpleNamespace(
            memory_read=lambda *args, **kwargs: pylink.JLink.memory_read(backend, *args, **kwargs),
            memory_write=lambda *args, **kwargs: pylink.JLink.memory_write(backend, *args, **kwargs),
        )
        expected = b"\x12\x34\x56\x78"
        for width in (8, 16, 32):
            unit_size = width // 8

            def read(address, byte_count, buffer, access):
                self.assertEqual((address, byte_count, access), (0x20000000, 4, unit_size))
                for index in range(4 // unit_size):
                    buffer[index] = int.from_bytes(expected[index * unit_size:(index + 1) * unit_size], "little")
                return byte_count

            def write(address, byte_count, buffer, access):
                self.assertEqual((address, byte_count, access), (0x20000000, 4, unit_size))
                self.assertEqual(b"".join(value.to_bytes(unit_size, "little") for value in buffer), expected)
                return byte_count

            with self.subTest(width=width):
                dll.JLINKARM_ReadMemEx.side_effect = read
                dll.JLINKARM_WriteMemEx.side_effect = write
                self.assertEqual(memory._read_memory_bytes(adapter, 0x20000000, 4, width), expected)
                self.assertEqual(memory._write_memory_bytes(adapter, 0x20000000, expected, width), 4)


if __name__ == "__main__":
    unittest.main()
