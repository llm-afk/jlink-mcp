import unittest
from unittest.mock import Mock, patch
from jlink_mcp.tools import rtt
from jlink_mcp.jlink_manager import JLinkManager


class RTTTests(unittest.TestCase):
    def setUp(self):
        with patch.object(JLinkManager, "_initialized", False):
            self.manager = object.__new__(JLinkManager)
            self.manager.__init__()
        self.link = Mock()
        self.link.serial_number = 123
        self.link.rtt_read.return_value = []
        self.manager._jlink = self.link
        self.manager.add_cleanup_callback(rtt._cleanup_session)
        self.manager_patch = patch.object(rtt, "jlink_manager", self.manager)
        self.manager_patch.start()
        rtt._clear_state()

    def tearDown(self):
        rtt._clear_state()
        self.manager_patch.stop()

    def start(self, **kwargs):
        self.assertTrue(rtt.rtt_start(**kwargs)["success"])

    def test_disconnect_stops_rtt_and_clears_session(self):
        self.start()
        self.manager.disconnect()
        self.link.rtt_stop.assert_called_once_with()
        self.assertFalse(rtt.rtt_get_status()["started"])
        self.assertFalse(rtt.rtt_read()["success"])
        new_link = Mock(serial_number=456)
        self.manager._jlink = new_link
        self.start()
        new_link.rtt_start.assert_called_once_with(None)

    def test_new_session_does_not_reuse_rtt_state(self):
        self.start()
        self.manager._session_id += 1
        self.assertFalse(rtt.rtt_get_status()["started"])
        self.assertFalse(rtt.rtt_write("hello")["success"])
        self.link.rtt_write.assert_not_called()

    def test_stop_failure_still_clears_state(self):
        self.start()
        self.link.rtt_stop.side_effect = OSError("disconnected")
        self.assertFalse(rtt.rtt_stop()["success"])
        self.assertFalse(rtt.rtt_get_status()["started"])

    def test_utf8_split_across_reads_is_preserved(self):
        self.start(timeout_ms=0)
        encoded = "中文".encode("utf-8")
        self.link.rtt_read.side_effect = [list(encoded[:2]), list(encoded[2:4]), list(encoded[4:])]
        results = [rtt.rtt_read() for _ in range(3)]
        self.assertEqual("".join(result["data"] for result in results), "中文")
        self.assertEqual("".join(result["data_hex"] for result in results), encoded.hex())

    def test_decoders_are_separate_per_buffer(self):
        self.start(timeout_ms=0)
        self.link.rtt_read.side_effect = [[0xE4], b"A", [0xB8, 0xAD]]
        self.assertEqual(rtt.rtt_read(0)["data"], "")
        self.assertEqual(rtt.rtt_read(1)["data"], "A")
        self.assertEqual(rtt.rtt_read(0)["data"], "中")

    def test_default_buffer_uses_start_config(self):
        self.start(buffer_index=3, timeout_ms=0, block_address=0x20000000)
        rtt.rtt_read()
        self.link.rtt_read.assert_called_once_with(3, 1024)
        self.link.rtt_write.return_value = 1
        self.assertTrue(rtt.rtt_write("A")["success"])
        self.link.rtt_write.assert_called_once_with(3, b"A")
        self.link.rtt_start.assert_called_once_with(0x20000000)

    def test_continuous_read_polls_until_data(self):
        self.start(timeout_ms=100)
        self.link.rtt_read.side_effect = [[], [65]]
        with patch.object(rtt.time, "sleep") as sleep:
            result = rtt.rtt_read()
        self.assertEqual(result["data"], "A")
        self.assertEqual(self.link.rtt_read.call_count, 2)
        sleep.assert_called_once()

    def test_empty_read_obeys_timeout(self):
        self.start(timeout_ms=10)
        with patch.object(rtt.time, "monotonic", side_effect=[0.0, 0.011]), patch.object(rtt.time, "sleep") as sleep:
            self.assertEqual(rtt.rtt_read()["bytes_read"], 0)
        self.link.rtt_read.assert_called_once()
        sleep.assert_not_called()

    def test_once_mode_does_not_poll(self):
        self.start(read_mode="once")
        with patch.object(rtt.time, "sleep") as sleep:
            self.assertEqual(rtt.rtt_read()["bytes_read"], 0)
        sleep.assert_not_called()
        self.link.rtt_read.assert_called_once()

    def test_invalid_parameters_do_not_access_driver(self):
        self.assertFalse(rtt.rtt_start(timeout_ms=-1)["success"])
        self.assertFalse(rtt.rtt_start(read_mode="unknown")["success"])
        self.link.rtt_start.assert_not_called()
        self.start(timeout_ms=0)
        self.assertFalse(rtt.rtt_read(size=-1)["success"])
        self.assertFalse(rtt.rtt_read(timeout_ms=60001)["success"])
        self.link.rtt_read.assert_not_called()
