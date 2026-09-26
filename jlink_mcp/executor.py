"""Run blocking tools in order without blocking the MCP event loop."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, Callable


class SerialToolExecutor:
    """Keep one device's calls on one thread, including connection cleanup.

    Cancellation can discard a queued call, but cannot interrupt a running DLL
    call. The worker remains occupied until that call finishes, so a cancelled
    operation never overlaps a later operation on the same device.
    """

    def __init__(self, name: str):
        self._name = name
        self._executor: ThreadPoolExecutor | None = None
        self._pending: set[Future] = set()
        self._closed = False

    async def run(self, function: Callable[..., Any], /, *args, **kwargs) -> Any:
        if self._closed:
            raise RuntimeError("Tool executor is shutting down")
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self._name)
        future = self._executor.submit(partial(function, *args, **kwargs))
        self._pending.add(future)
        try:
            return await asyncio.wrap_future(future)
        finally:
            self._pending.discard(future)

    async def aclose(self, cleanup: Callable[[], Any] | None = None) -> None:
        """Cancel queued calls and clean up after any running call finishes."""
        if self._closed:
            return
        self._closed = True
        for future in tuple(self._pending):
            future.cancel()
        executor = self._executor
        if executor is None:
            # Cleanup may still be needed if the library was used directly.
            if cleanup is None:
                return
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self._name)
        try:
            if cleanup is not None:
                await asyncio.wrap_future(executor.submit(cleanup))
        finally:
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
