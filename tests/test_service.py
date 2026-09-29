"""Offline behavioral tests: never attach a real probe."""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import TypeAdapter, ValidationError

from jlink_mcp import api_models as m
from jlink_mcp.service import DebugService
from jlink_mcp.models.svd import RegisterInfo, PeripheralInfo


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.s = DebugService()
        self.probe = Mock()
        self.probe.halted.return_value = True
        self.probe.memory_read.return_value = [1, 2, 3, 4]
        self.probe.memory_write.return_value = 4
        self.probe.register_read.return_value = 123
        self.s.probe, self.s.session_id = self.probe, "test"
        self.s.target = {"chip": "GD32C103CB", "architecture": "cortex-m", "svd_device": "GD32C10x"}
        p = patch("jlink_mcp.service.jlink_manager")
        self.manager = p.start()
        self.addCleanup(p.stop)
        self.manager.get_jlink.return_value = self.probe
        p = patch("jlink_mcp.service.gdb_server_manager")
        self.gdb = p.start()
        self.addCleanup(p.stop)
        self.gdb.is_running = False

    def read(self, items=None, **kwargs):
        return self.s.invoke("read", m.ReadRequest(session_id="test", items=items or [
            {"kind": "memory", "address": 0x20000000, "size": 4}], **kwargs))

    def test_live_memory_never_halts(self):
        self.probe.halted.return_value = False
        result = self.read()
        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["cpu_after"], "running")
        self.probe.halt.assert_not_called()

    def test_cpu_register_live_is_rejected(self):
        result = self.read([{"kind": "register", "name": "PC"}])
        self.assertFalse(result["success"])
        self.probe.register_read.assert_not_called()
        self.probe.halt.assert_not_called()

    def test_halted_precondition_never_auto_halts(self):
        self.probe.halted.return_value = False
        result = self.read(consistency="halted")
        self.assertEqual(result["error"]["code"], "REQUIRES_HALT")
        self.probe.memory_read.assert_not_called()
        self.probe.halt.assert_not_called()

    def test_stale_session_never_accesses_driver(self):
        result = self.s.invoke("control", m.RunControl(session_id="old", action="halt"))
        self.assertEqual(result["error"]["code"], "STALE_SESSION")
        self.assertFalse(self.probe.mock_calls)

    def test_backend_conflict(self):
        self.gdb.is_running = True
        self.assertEqual(self.read()["error"]["code"], "BACKEND_BUSY")
        self.probe.memory_read.assert_not_called()

    def test_connection_replacement_invalidates_snapshots(self):
        self.s.snapshots["old"] = {}
        self.manager.get_jlink.return_value = Mock()
        self.assertEqual(self.read()["error"]["code"], "SESSION_LOST")
        self.assertFalse(self.s.snapshots)

    def test_partial_read_preserves_good_items(self):
        self.probe.memory_read.side_effect = [[1, 2, 3, 4], OSError("unavailable")]
        result = self.read([{"kind": "memory", "address": a, "size": 4} for a in (0, 4)])
        self.assertFalse(result["success"])
        self.assertEqual(result["items"][0]["data"]["data_hex"], "01020304")
        self.assertFalse(result["items"][1]["success"])

    def test_width_is_actual_bus_width(self):
        self.probe.memory_read.return_value = [0x04030201]
        result = self.read([{"kind": "memory", "address": 0, "size": 4, "width": 32}])
        self.assertEqual(result["items"][0]["data"]["data_hex"], "01020304")
        self.probe.memory_read.assert_called_once_with(0, 1, nbits=32)

    def test_invalid_span_never_reads(self):
        self.assertFalse(self.read([{"kind": "memory", "address": 1, "size": 4, "width": 32}])["success"])
        self.probe.memory_read.assert_not_called()

    def test_total_limit_precedes_reads(self):
        result = self.read([{"kind": "memory", "address": a, "size": 65536} for a in (0, 65536)])
        self.assertEqual(result["error"]["code"], "LIMIT")
        self.probe.memory_read.assert_not_called()

    def test_halted_snapshot_discards_data_after_state_change(self):
        def reading(*args, **kwargs):
            self.probe.halted.return_value = False
            return [1, 2, 3, 4]
        self.probe.memory_read.side_effect = reading
        result = self.read(consistency="halted")
        self.assertFalse(result["success"])
        self.assertNotIn("data", result["items"][0])

    def test_write_mismatch_is_failure_without_retry(self):
        request = m.WriteRequest(session_id="test", verify=True, item={
            "kind": "memory", "address": 0x20000000, "data_hex": "00000000"})
        result = self.s.invoke("write", request)
        self.assertFalse(result["success"])
        self.assertEqual(result["verification"], "mismatch")
        self.probe.memory_write.assert_called_once()

    def test_write_timeout_does_not_retry(self):
        self.probe.memory_write.side_effect = TimeoutError("timeout")
        result = self.s.invoke("write", m.WriteRequest(session_id="test", item={
            "kind": "memory", "address": 0, "data_hex": "01020304"}))
        self.assertFalse(result["success"])
        self.probe.memory_write.assert_called_once()

    def test_short_read_error_can_be_serialized(self):
        self.probe.memory_read.return_value = []
        result = self.read()
        self.assertFalse(result["success"])
        self.assertEqual(result["items"][0]["error"]["code"], 200)
        json.dumps(result)

    def test_close_still_releases_disconnected_session(self):
        self.manager.get_jlink.side_effect = OSError("unplugged")
        with patch("jlink_mcp.service.connection.disconnect_device", return_value={"success": True}):
            result = self.s.invoke("session", m.CloseSession(session_id="test", action="close"))
        self.assertTrue(result["success"])
        self.assertIsNone(self.s.session_id)

    def test_failed_breakpoint_clear_keeps_ownership(self):
        self.s.breakpoints["b"] = {"handle": 1, "address": 0x08000000}
        self.probe.breakpoint_clear.return_value = False
        result = self.s.invoke("breakpoint", m.BreakpointRemove(session_id="test", action="remove", breakpoint_id="b"))
        self.assertFalse(result["success"])
        self.assertIn("b", self.s.breakpoints)

    def test_open_checks_target_and_creates_new_identity(self):
        self.s.session_id = self.s.probe = self.s.target = None
        self.manager.is_connected = False
        actual = {"success": True, "data": {"target_connected": True, "device_serial": "123"}}
        with patch("jlink_mcp.service.connection.connect_device", return_value={"success": True}), patch(
                "jlink_mcp.service.connection.get_connection_status", return_value=actual):
            result = self.s.invoke("session", m.OpenSession(action="open", chip="GD32C103CB", serial_number="123"))
        self.assertTrue(result["success"])
        self.assertNotEqual(result["session_id"], "test")
        self.assertEqual(self.s.target["actual_serial_number"], "123")

    def test_probe_only_connection_is_not_target_success(self):
        self.s.session_id = self.s.probe = None
        self.manager.is_connected = False
        with patch("jlink_mcp.service.connection.connect_device", return_value={"success": True}), patch(
                "jlink_mcp.service.connection.get_connection_status", return_value={"success": True, "data": {"target_connected": False}}), patch(
                "jlink_mcp.service.connection.disconnect_device") as disconnect:
            result = self.s.invoke("session", m.OpenSession(action="open", chip="GD32C103CB", serial_number="123"))
        self.assertEqual(result["error"]["code"], "TARGET_NOT_CONNECTED")
        disconnect.assert_called_once()

    def test_halt_timeout_never_resets(self):
        self.probe.halted.return_value = False
        result = self.s.invoke("control", m.RunControl(session_id="test", action="halt", timeout_ms=1))
        self.assertEqual(result["completion"], "timeout")
        self.probe.reset.assert_not_called()
        self.probe.halt.assert_called_once()

    def test_step_on_unknown_arch_rejected(self):
        self.s.target["architecture"] = "unknown"
        result = self.s.invoke("control", m.RunControl(session_id="test", action="step"))
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_ARCH")
        self.probe.step.assert_not_called()

    def test_reset_invalidates_snapshots_even_on_failure(self):
        self.s.snapshots["old"] = {}
        with patch("jlink_mcp.service.debug.reset_target", return_value={"success": False}):
            self.s.invoke("control", m.ResetControl(session_id="test", action="reset"))
        self.assertFalse(self.s.snapshots)

    def test_snapshot_diff_and_invalidation(self):
        snapshot = self.s.invoke("capture", m.Snapshot(session_id="test", action="snapshot", items=[
            {"kind": "memory", "address": 0, "size": 4}]))
        self.probe.memory_read.return_value = [4, 3, 2, 1]
        req = m.Diff(session_id="test", action="diff", snapshot_id=snapshot["snapshot_id"])
        self.assertEqual(len(self.s.invoke("capture", req)["changes"]), 1)
        self.s._invalidate()
        self.assertEqual(self.s.invoke("capture", req)["error"]["code"], "STALE_SNAPSHOT")

    def test_samples_write_evidence_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "samples.jsonl"
            req = m.Sample(session_id="test", action="sample", count=2, interval_ms=10,
                           output_path=str(path), items=[{"kind": "memory", "address": 0, "size": 4}])
            result = self.s.invoke("capture", req)
            self.assertTrue(result["success"])
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["session_id"], "test")
            self.assertFalse(self.s.invoke("capture", req)["success"])
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_backup_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "backup.bin"
            path.write_bytes(b"original")
            result = self.s.invoke("firmware", m.FirmwareBackup(session_id="test", action="backup",
                                    address=0, size=4, output_path=str(path)))
            self.assertFalse(result["success"])
            self.assertEqual(path.read_bytes(), b"original")
            self.probe.memory_read.assert_not_called()

    def test_page_erase_never_rounds_address(self):
        result = self.s.invoke("firmware", m.FirmwareErasePages(session_id="test", action="erase_pages",
                                                               address=0x08000001, page_size=1024))
        self.assertEqual(result["error"]["code"], "ALIGNMENT")
        self.probe.flash.assert_not_called()

    def test_chip_confirmation_must_match(self):
        result = self.s.invoke("firmware", m.FirmwareEraseChip(session_id="test", action="erase_chip", confirm_chip="wrong"))
        self.assertEqual(result["error"]["code"], "TARGET_MISMATCH")
        self.probe.erase.assert_not_called()

    def test_program_always_verifies_and_expires_captures(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "image.bin"
            path.write_bytes(b"1234")
            self.s.snapshots["old"] = {}
            with patch("jlink_mcp.service.flash.program_flash", return_value={"success": True}) as program:
                result = self.s.invoke("firmware", m.FirmwareImage(session_id="test", action="program", address=0, file_path=str(path)))
                self.assertTrue(result["success"])
                self.assertTrue(program.call_args.kwargs["verify"])
                self.assertFalse(self.s.snapshots)

    def test_elf_is_never_flashed_as_bin(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "image.elf"
            path.write_bytes(b"1234")
            result = self.s.invoke("firmware", m.FirmwareImage(session_id="test", action="program", address=0, file_path=str(path)))
            self.assertFalse(result["success"])
            self.probe.flash.assert_not_called()

    def test_svd_read_side_effect_requires_opt_in(self):
        info = RegisterInfo(name="STATUS", address_offset=0, read_action="clear")
        peripheral = PeripheralInfo(name="ADC", base_address=0x40000000)
        with patch("jlink_mcp.service.svd_manager.get_register", return_value=info), patch(
                "jlink_mcp.service.svd_manager.get_peripheral", return_value=peripheral):
            result = self.read([{"kind": "peripheral", "name": "ADC.STATUS"}])
        self.assertFalse(result["success"])
        self.probe.memory_read.assert_not_called()

    def test_breakpoints_owned_and_deduplicated(self):
        self.probe.hardware_breakpoint_set.return_value = 3
        req = m.BreakpointAdd(session_id="test", action="set", address=0x8000101)
        a, b = self.s.invoke("breakpoint", req), self.s.invoke("breakpoint", req)
        self.assertEqual(a["breakpoint_id"], b["breakpoint_id"])
        self.probe.hardware_breakpoint_set.assert_called_once_with(0x8000100, thumb=True)
        result = self.s.invoke("breakpoint", m.BreakpointRemove(session_id="test", action="remove", breakpoint_id=a["breakpoint_id"]))
        self.assertTrue(result["success"])
        self.probe.breakpoint_clear.assert_called_once_with(3)

    def test_all_calls_are_serialized(self):
        active = 0
        maximum = 0
        def read(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.01)
            active -= 1
            return [1, 2, 3, 4]
        self.probe.memory_read.side_effect = read
        threads = [threading.Thread(target=self.read) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(maximum, 1)


class SchemaTests(unittest.TestCase):
    def test_invalid_actions_and_unused_options_are_rejected(self):
        for value in ({"session_id": "s", "action": "run_until"},
                      {"session_id": "s", "action": "resume", "reset": True}):
            with self.assertRaises(ValidationError):
                TypeAdapter(m.ControlRequest).validate_python(value)

    def test_sampling_limits(self):
        with self.assertRaises(ValidationError):
            m.Sample(action="sample", session_id="s", items=[{"kind": "register", "name": "PC"}], count=128, interval_ms=1000)

    def test_exactly_ten_tools_and_valid_schemas(self):
        from jlink_mcp.server import mcp
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual({t.name for t in tools}, {"discover", "session", "read", "write", "control",
                         "breakpoint", "inspect", "capture", "firmware", "channel"})
        import jsonschema
        for tool in tools:
            jsonschema.Draft202012Validator.check_schema(tool.inputSchema)

    def test_mcp_dispatch_validation_and_state(self):
        from jlink_mcp.server import mcp
        from jlink_mcp.worker import worker
        self.addCleanup(worker.close)
        async def run():
            # The request is rejected before any actual probe access.
            return await mcp.call_tool("read", {"request": {"session_id": "stale", "items": [
                {"kind": "memory", "address": 0, "size": 4}]}})
        response = asyncio.run(run())
        content = response[0] if isinstance(response, tuple) else response
        result = json.loads(content[0].text)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "STALE_SESSION")


if __name__ == "__main__":
    unittest.main()
