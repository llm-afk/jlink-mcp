import sys
import unittest
from unittest.mock import patch

from jlink_mcp.worker import DriverWorker


FAKE_DRIVER = '''
import sys, json, time
for line in sys.stdin:
    data = json.loads(line)
    method = data['method']
    if method == 'hang':
        time.sleep(60)
    if method == 'die':
        break
    print(json.dumps({'success': True, 'value': data['request']}), flush=True)
    if method == '_close':
        break
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = DriverWorker(command=[sys.executable, "-u", "-c", FAKE_DRIVER])
        self.addCleanup(self.worker.close)

    def test_timeout_kills_owner_and_does_not_retry(self):
        self.assertTrue(self.worker.invoke("echo", 1)["success"])
        pid = self.worker.process.pid
        result = self.worker.invoke("hang", timeout=0.05)
        self.assertEqual(result["error"]["code"], "BACKEND_TIMEOUT")
        self.assertTrue(result["session_lost"])
        self.assertFalse(result["retry_safe"])
        self.assertIsNone(self.worker.process)
        self.assertEqual(self.worker.invoke("echo", 2)["value"], 2)
        self.assertNotEqual(self.worker.process.pid, pid)

    def test_crash_is_not_success(self):
        result = self.worker.invoke("die")
        self.assertEqual(result["error"]["code"], "BACKEND_LOST")
        self.assertIsNone(self.worker.process)

    def test_graceful_close_releases_worker(self):
        self.worker.invoke("echo", None)
        self.worker.close()
        self.assertIsNone(self.worker.process)

    def test_worker_entry_unicode_with_legacy_console_encoding(self):
        command = [sys.executable, "-u", "-c", (
            "from jlink_mcp import worker_entry; "
            "worker_entry.service.invoke = lambda *args: "
            "{'success': True, 'value': '\\u4e2d\\u6587\\u6d4b\\u8bd5\\U0001f680'}; "
            "worker_entry.main()"
        )]
        worker = DriverWorker(command=command)
        self.addCleanup(worker.close)
        with patch.dict("os.environ", {"PYTHONIOENCODING": "cp936"}):
            result = worker.invoke("discover")
            self.assertTrue(result["success"], result)
            self.assertEqual(result["value"], "中文测试🚀")
