import csv
import ctypes
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError
from jlink_mcp import hss
from jlink_mcp.api_models import HssCapture
from jlink_mcp.service import DebugService, ServiceError


class HssTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "signals.csv"
        self.regions = [SimpleNamespace(kind="ram", address=0x2000, size=256)]
        self.fields = [dict(address=0x2002, size=2, type=dict(kind="signed")),
                       dict(address=0x2000, size=2, type=dict(kind="unsigned"))]
        self.req = HssCapture(session_id="s", action="hss", count=2, items=["b", "a"], output_path=str(self.path))
        self.probe = Mock()
        self.probe.halted.return_value = False
        self.driver = Mock(max_blocks=1, max_hz=10000)
        self.driver.start.return_value = self.driver.stop.return_value = 0
        patcher = patch.object(hss, "NativeHss", return_value=self.driver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_capture(self):
        return hss.capture(self.probe, self.req, self.fields, self.regions)

    def test_fragments_request_order_signed_and_clock_wrap(self):
        data = struct.pack("<IHhIHh", 0xfffffff0, 31, -4, 984, 32, -5)
        fragments = iter([data[:5], data[5:]])
        def read(buf, size):
            part = next(fragments)
            ctypes.memmove(buf, part, len(part))
            return len(part)
        self.driver.read.side_effect = read
        result = self.run_capture()
        self.assertTrue(result["complete"])
        self.assertEqual((result["blocks"], result["actual_hz"]), (1, 1000))
        with self.path.open() as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [["timestamp_us", "b", "a"], [str(0xfffffff0), "-4", "31"],
                                [str(0xfffffff0 + 1000), "-5", "32"]])
        self.driver.stop.assert_called_once()

    def test_failed_read_stops_and_preserves_only_complete_frames(self):
        def one(buf, size):
            ctypes.memmove(buf, bytes(9), 9)
            self.driver.read.side_effect = None
            self.driver.read.return_value = -1
            return 9
        self.driver.read.side_effect = one
        result = self.run_capture()
        self.assertFalse(result["success"])
        self.assertEqual(result["sample_count"], 1)
        self.driver.stop.assert_called_once()

    def test_no_start_on_existing_file_or_invalid_limit(self):
        self.path.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.run_capture()
        self.driver.max_hz = 100
        with self.assertRaises(hss.Unsupported):
            self.run_capture()
        self.assertEqual(self.path.read_text(), "keep")
        self.driver.start.assert_not_called()

    def test_no_coalescing_across_gaps_or_region_boundaries(self):
        regions = [SimpleNamespace(kind="ram", address=start, size=2) for start in (0x2000, 0x2002)]
        self.assertEqual(hss.layout(self.fields, regions)[0], [(0x2000, 2), (0x2002, 2)])
        self.fields[0]["address"] += 2
        self.assertEqual(hss.layout(self.fields, self.regions)[0], [(0x2000, 2), (0x2004, 2)])

    def test_invalid_requests(self):
        for opts in [dict(items=["a", "a"]), dict(items=["a"], count=12, rate_hz=1),
                     dict(items=["a"], duration_ms=500, count=10), dict(items=["a"], rate_hz=1, duration_ms=5)]:
            with self.assertRaises(ValidationError):
                HssCapture(session_id="s", action="hss", **opts)

    def test_duration_roundtrips_through_worker(self):
        req = HssCapture(session_id="s", action="hss", items=["x"], rate_hz=5000, duration_ms=500)
        self.assertEqual(HssCapture.model_validate(req.model_dump()).sample_count, 2500)

    def test_window_is_in_probe_time_even_when_actual_rate_is_lower(self):
        self.req = self.req.model_copy(update={"count": None, "duration_ms": 10})
        packets = b"".join(struct.pack("<IHH", tick, 11, 22) for tick in (51, 3051, 6051, 9051, 12051))
        def read(buf, size):
            ctypes.memmove(buf, packets, len(packets))
            return len(packets)
        self.driver.read.side_effect = read
        result = self.run_capture()
        self.assertTrue(result["complete"])
        self.assertEqual((result["sample_count"], result["captured_span_us"]), (4, 9000))
        self.assertEqual(result["actual_hz"], 333.33)

    def test_preconditions_before_start(self):
        service = DebugService()
        service.probe = self.probe
        self.probe.halted.return_value = True
        with self.assertRaisesRegex(ServiceError, "running"):
            service.read(self.req)
        self.probe.halted.return_value = False
        with patch("jlink_mcp.service.rtt.rtt_get_status", return_value={"started": True}):
            with self.assertRaisesRegex(ServiceError, "RTT"):
                service.read(self.req)
        with self.assertRaises(ServiceError):
            service.read(self.req)
        self.driver.start.assert_not_called()

    def test_stop_failure_is_reported(self):
        self.driver.read.return_value = -1
        self.driver.stop.return_value = -1
        with self.assertRaises(hss.StopError):
            self.run_capture()
