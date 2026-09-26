import io
import subprocess
import unittest
from unittest.mock import Mock, patch
import pylink

from jlink_mcp.exceptions import JLinkMCPError, GDBServerError
from jlink_mcp.jlink_manager import JLinkManager
from jlink_mcp.gdb_server import GDBServerManager
from jlink_mcp.models.device import TargetInterface


def connection_manager():
    with patch.object(JLinkManager, "_initialized", False):
        manager = object.__new__(JLinkManager)
        manager.__init__()
    link = Mock()
    link.serial_number = 123
    link.opened.return_value = False
    manager._jlink = link
    manager._device_serial = "123"
    manager._device_name = "STM32F407VG"
    manager._target_interface = TargetInterface.JTAG
    manager._connected = True
    return manager, link


class FakeProcess:
    def __init__(self, output=b"Waiting for GDB connection...\n", returncode=None):
        self.stdout = io.BytesIO(output)
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminate()


class ConnectionTests(unittest.TestCase):
    def test_disconnect_cleans_up_after_connection_loss(self):
        manager, link = connection_manager()
        class LostLink:
            close = Mock()
            @property
            def serial_number(self):
                raise OSError("probe unplugged")
        link = LostLink()
        manager._jlink = link
        callback = Mock()
        manager.add_cleanup_callback(callback)
        self.assertFalse(manager.is_connected)
        manager.disconnect()
        link.close.assert_called_once_with()
        callback.assert_called_once_with(link)
        self.assertIsNone(manager._jlink)
        self.assertIsNone(manager._device_name)
        self.assertFalse(manager._connected)

    def test_transfer_requires_explicit_consent_and_blocks_mcp(self):
        manager, link = connection_manager()
        with self.assertRaises(JLinkMCPError):
            manager.reserve_for_gdb()
        link.close.assert_not_called()
        session = manager.reserve_for_gdb(True)
        self.assertEqual(session["device"], "STM32F407VG")
        self.assertEqual(session["serial_number"], "123")
        link.close.assert_called_once_with()
        with self.assertRaises(JLinkMCPError):
            manager.connect(chip_name="STM32F407VG")
        with self.assertRaises(JLinkMCPError):
            manager.get_jlink()
        manager.disconnect()
        self.assertEqual(manager._reservation, "GDB Server")
        manager.release_gdb_reservation()
        self.assertIsNone(manager._reservation)
        self.assertIsNone(manager._jlink)

    def test_failed_close_cancels_transfer(self):
        manager, link = connection_manager()
        link.close.side_effect = OSError("close failed")
        with self.assertRaises(JLinkMCPError):
            manager.reserve_for_gdb(True)
        self.assertIsNone(manager._reservation)
        self.assertIs(manager._jlink, link)
        self.assertTrue(manager._close_failed)
        self.assertFalse(manager.is_connected)
        with patch.object(manager, "_connect") as connect:
            with self.assertRaises(JLinkMCPError):
                manager.connect(chip_name="STM32F407VG")
            connect.assert_not_called()
        with self.assertRaises(JLinkMCPError):
            manager.reserve_for_gdb(True)
        with self.assertRaises(JLinkMCPError):
            manager.disconnect()
        self.assertIs(manager._jlink, link)

    def test_explicit_disconnect_recovers_retained_failed_close(self):
        manager, link = connection_manager()
        link.close.side_effect = [OSError("close failed"), None]
        with self.assertRaises(JLinkMCPError):
            manager.reserve_for_gdb(True)
        self.assertIs(manager._jlink, link)
        manager.disconnect()
        self.assertEqual(link.close.call_count, 2)
        self.assertIsNone(manager._jlink)
        self.assertFalse(manager._close_failed)
        with patch.object(manager, "_connect") as connect:
            manager.connect(chip_name="STM32F407VG")
            connect.assert_called_once()

    def test_noop_close_retry_does_not_release_still_open_driver(self):
        manager, link = connection_manager()
        link.close.side_effect = [OSError("close failed"), None]
        link.opened.return_value = True
        with self.assertRaises(JLinkMCPError):
            manager.reserve_for_gdb(True)
        with self.assertRaises(JLinkMCPError):
            manager.disconnect()
        self.assertIs(manager._jlink, link)
        self.assertTrue(manager._close_failed)
        with self.assertRaises(JLinkMCPError):
            manager.get_target_info()
        self.assertEqual(link.close.call_count, 2)

    def test_real_pylink_close_refcount_failure_does_not_release_driver(self):
        manager, _ = connection_manager()
        # 使用真实 PyLink close/opened 实现，DLL 完全替换为 mock；不初始化探针。
        link = object.__new__(pylink.JLink)
        link._finalize = Mock()
        link._open_refcount = 1
        link._coresight_configured = True
        link._lock = None
        link._dll = Mock()
        link._dll.JLINKARM_GetSN.return_value = 123
        link._dll.JLINKARM_IsOpen.return_value = 1
        link._dll.JLINKARM_Close.side_effect = OSError("DLL close failed")
        manager._jlink = link
        with self.assertRaises(JLinkMCPError):
            manager.reserve_for_gdb(True)
        self.assertEqual(link._open_refcount, 0)
        with self.assertRaises(JLinkMCPError):
            manager.disconnect()
        link._dll.JLINKARM_Close.assert_called_once()
        self.assertIs(manager._jlink, link)
        self.assertTrue(manager._close_failed)

    def test_target_device_id_is_unknown_instead_of_core_id(self):
        manager, link = connection_manager()
        link.core_name.return_value = "Cortex-M4"
        link.core_id.return_value = 0x410FC241
        link.device = None
        info = manager.get_target_info()
        self.assertEqual(info.core_id, 0x410FC241)
        self.assertIsNone(info.device_id)


class GDBTests(unittest.TestCase):
    def setUp(self):
        self.manager, self.link = connection_manager()
        self.manager_patch = patch("jlink_mcp.gdb_server.jlink_manager", self.manager)
        self.manager_patch.start()
        with patch.object(GDBServerManager, "_initialized", False):
            self.gdb = object.__new__(GDBServerManager)
            self.gdb.__init__()
        self.find_patch = patch.object(self.gdb, "_find_jlink_gdbserver_exe", return_value="fake-gdb.exe")
        self.find_patch.start()

    def tearDown(self):
        self.gdb.stop()
        self.find_patch.stop()
        self.manager_patch.stop()

    def test_inherits_device_and_interface_and_drains_output(self):
        process = FakeProcess(b"x" * 70000 + b"Waiting for GDB connection...\n")
        with patch("jlink_mcp.gdb_server.subprocess.Popen", return_value=process) as spawn:
            self.gdb.start(transfer_connection=True)
        command = spawn.call_args.args[0]
        self.assertEqual(command[command.index("-device") + 1], "STM32F407VG")
        self.assertEqual(command[command.index("-if") + 1], "jtag")
        self.assertEqual(command[command.index("-LocalhostOnly") + 1], "1")
        self.assertEqual(command[command.index("-select") + 1], "USB=123")
        self.gdb._reader.join(timeout=1)
        self.assertLessEqual(len(self.gdb._output_tail), 16384)
        self.assertTrue(self.gdb.is_running)
        self.link.close.assert_called_once_with()
        self.assertEqual(self.manager._reservation, "GDB Server")
        self.gdb.stop()
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdout.closed)
        self.assertIsNone(self.manager._reservation)
        self.assertIsNone(self.manager._jlink)

    def test_remote_host_maps_to_explicit_server_option(self):
        with patch("jlink_mcp.gdb_server.subprocess.Popen", return_value=FakeProcess()) as spawn:
            self.gdb.start(host="0.0.0.0", transfer_connection=True)
        command = spawn.call_args.args[0]
        self.assertEqual(command[command.index("-LocalhostOnly") + 1], "0")

    def test_explicit_device_can_start_without_mcp_connection(self):
        self.manager.disconnect()
        with patch("jlink_mcp.gdb_server.subprocess.Popen", return_value=FakeProcess()) as spawn:
            self.gdb.start(device="STM32F407VG")
        command = spawn.call_args.args[0]
        self.assertEqual(command[command.index("-if") + 1], "swd")
        self.assertNotIn("-select", command)
        self.assertEqual(self.manager._reservation, "GDB Server")

    def test_missing_device_without_connection_never_spawns(self):
        self.manager.disconnect()
        with patch("jlink_mcp.gdb_server.subprocess.Popen") as spawn:
            with self.assertRaises(GDBServerError):
                self.gdb.start()
        spawn.assert_not_called()

    def test_readiness_marker_may_span_output_chunks(self):
        stream = Mock()
        stream.read1.side_effect = [b"Listening on TCP/IP port 2331\\nWaiting for G", b"DB connection...", b""]
        self.gdb._drain_output(stream)
        self.assertTrue(self.gdb._ready.is_set())

    def test_listening_before_target_connection_is_not_ready(self):
        self.gdb._drain_output(io.BytesIO(b"Listening on TCP/IP port 2331\\nConnecting to target..."))
        self.assertFalse(self.gdb._ready.is_set())

    def test_bad_host_and_implicit_transfer_never_spawn(self):
        with patch("jlink_mcp.gdb_server.subprocess.Popen") as spawn:
            with self.assertRaises(GDBServerError):
                self.gdb.start(host="192.168.1.2", transfer_connection=True)
            with self.assertRaises(GDBServerError):
                self.gdb.start()
        spawn.assert_not_called()
        self.link.close.assert_not_called()

    def test_live_process_without_readiness_is_not_success(self):
        self.gdb._process = FakeProcess(b"")
        with self.assertRaises(GDBServerError):
            self.gdb._wait_until_ready(timeout=0)
        self.assertFalse(self.gdb._running)

    def test_start_failure_releases_reservation_without_reconnecting(self):
        with patch("jlink_mcp.gdb_server.subprocess.Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(GDBServerError):
                self.gdb.start(transfer_connection=True)
        self.assertIsNone(self.manager._reservation)
        self.assertIsNone(self.manager._jlink)
        self.assertFalse(self.gdb.is_running)

    def test_reservation_failure_does_not_release_other_owner(self):
        self.manager._reservation = "GDB Server"
        with patch("jlink_mcp.gdb_server.subprocess.Popen") as spawn:
            with self.assertRaises(GDBServerError):
                self.gdb.start(transfer_connection=True)
        spawn.assert_not_called()
        self.assertEqual(self.manager._reservation, "GDB Server")

    def test_unstoppable_process_keeps_reservation_and_handle(self):
        process = FakeProcess()
        with patch("jlink_mcp.gdb_server.subprocess.Popen", return_value=process):
            self.gdb.start(transfer_connection=True)
        with patch.object(process, "wait", side_effect=subprocess.TimeoutExpired("fake", 2)), \
                patch.object(process, "terminate"), patch.object(process, "kill"):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.gdb.stop()
        self.assertIs(self.gdb._process, process)
        self.assertEqual(self.manager._reservation, "GDB Server")

    def test_early_exit_reports_failure_and_cleans_up(self):
        process = FakeProcess(b"ERROR: target unavailable\n", returncode=1)
        with patch("jlink_mcp.gdb_server.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(GDBServerError, "target unavailable"):
                self.gdb.start(transfer_connection=True)
        self.assertTrue(process.stdout.closed)
        self.assertIsNone(self.manager._reservation)

    def test_later_exit_releases_reservation_on_status_query(self):
        process = FakeProcess()
        with patch("jlink_mcp.gdb_server.subprocess.Popen", return_value=process):
            self.gdb.start(transfer_connection=True)
        process.returncode = 1
        status = self.gdb.get_status()
        self.assertFalse(status.running)
        self.assertIsNone(status.device_name)
        self.assertIsNone(self.manager._reservation)
