"""One disposable subprocess owns the native DLL; timeouts never retry actions."""
import json
import os
import queue
import subprocess
import sys
import threading
import time

import psutil


class DriverWorker:
    def __init__(self, command=None):
        self.command = command or [sys.executable, "-u", "-m", "jlink_mcp.worker_entry"]
        self.process = None
        self.responses = None
        self.reader = None
        self.lock = threading.Lock()

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self._stop()
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8", errors="strict", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.responses = queue.Queue()
        process, responses = self.process, self.responses
        def read_results():
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
                responses.put(EOFError("Native driver process closed its output"))
            except Exception as exc:
                responses.put(exc)
        self.reader = threading.Thread(target=read_results, daemon=True)
        self.reader.start()

    def _stop(self):
        if self.process is None:
            return
        # Windows venv python.exe can be a redirector with a native child.
        # Terminate only this owned process tree, never other MCP servers.
        try:
            root = psutil.Process(self.process.pid)
            owned = root.children(recursive=True) + [root]
            for child in reversed(owned):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(owned, timeout=2)
            if alive:
                raise RuntimeError("Unable to terminate native driver process tree")
        except psutil.NoSuchProcess:
            pass
        self.process.wait(timeout=2)
        if self.reader is not None:
            self.reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()
        self.process = self.reader = self.responses = None

    @staticmethod
    def deadline(method, request):
        if method == "firmware":
            return 120.0
        if method == "capture":
            return 20.0
        if method in ("control", "channel"):
            return max(10.0, getattr(request, "timeout_ms", 1000) / 1000 + 5.0)
        return 20.0

    def _exchange(self, method, request, timeout):
        payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else request
        self.process.stdin.write(json.dumps({"method": method, "request": payload}) + "\n")
        self.process.stdin.flush()
        try:
            result = self.responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("Native driver operation exceeded its wall-clock deadline") from exc
        if isinstance(result, Exception):
            raise result
        return result

    def invoke(self, method, request=None, timeout=None):
        with self.lock:
            started = time.monotonic()
            try:
                self._start()
                return self._exchange(method, request, timeout if timeout is not None else self.deadline(method, request))
            except (TimeoutError, EOFError, OSError, ValueError) as exc:
                self._stop()
                return {"success": False, "error": {"code": "BACKEND_TIMEOUT" if isinstance(exc, TimeoutError) else "BACKEND_LOST",
                                                    "message": str(exc)},
                        "completion": "unknown", "retry_safe": False, "session_lost": True,
                        "meta": {"session_id": None, "cpu_after": "unknown",
                                 "duration_ms": round((time.monotonic() - started) * 1000, 3)},
                        "recovery": "Open a new session explicitly; inspect target state before deciding the next action"}

    def close(self):
        with self.lock:
            try:
                if self.process is not None and self.process.poll() is None:
                    self._exchange("_close", None, 3)
            except (TimeoutError, EOFError, OSError, ValueError):
                pass
            finally:
                self._stop()


worker = DriverWorker()
