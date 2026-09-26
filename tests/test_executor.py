import asyncio
import threading
import unittest

from jlink_mcp.executor import SerialToolExecutor


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.executor = SerialToolExecutor("test-device")
        self.release = threading.Event()

    async def asyncTearDown(self):
        self.release.set()
        await self.executor.aclose()

    async def wait_started(self, started):
        self.assertTrue(await asyncio.to_thread(started.wait, 2))

    async def test_blocking_calls_leave_loop_responsive_and_use_one_thread(self):
        started = threading.Event()
        second_started = threading.Event()
        thread_ids = []

        def first():
            thread_ids.append(threading.get_ident())
            started.set()
            if not self.release.wait(3):
                raise TimeoutError("test worker was not released")

        def second():
            thread_ids.append(threading.get_ident())
            second_started.set()
            return 42

        first_task = asyncio.create_task(self.executor.run(first))
        await self.wait_started(started)
        second_task = asyncio.create_task(self.executor.run(second))
        # This completes while the worker is blocked, without releasing it.
        await asyncio.wait_for(asyncio.sleep(0.01), 1)
        self.assertFalse(second_started.is_set())
        self.release.set()
        self.assertEqual(await second_task, 42)
        await first_task
        self.assertEqual(thread_ids[0], thread_ids[1])
        self.assertNotEqual(thread_ids[0], threading.get_ident())

    async def test_cancelled_queued_call_never_touches_device(self):
        started = threading.Event()
        calls = []

        def blocking():
            started.set()
            self.release.wait(3)

        active = asyncio.create_task(self.executor.run(blocking))
        await self.wait_started(started)
        queued = asyncio.create_task(self.executor.run(calls.append, "write"))
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.release.set()
        await active
        await self.executor.run(lambda: None)
        self.assertEqual(calls, [])

    async def test_cancelled_running_call_keeps_device_reserved_until_finished(self):
        started = threading.Event()
        finished = threading.Event()

        def blocking():
            started.set()
            self.release.wait(3)
            finished.set()

        active = asyncio.create_task(self.executor.run(blocking))
        await self.wait_started(started)
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        later = asyncio.create_task(self.executor.run(finished.is_set))
        await asyncio.sleep(0.01)
        self.assertFalse(later.done())
        self.release.set()
        self.assertTrue(await later)

    async def test_exception_does_not_poison_worker_and_cleanup_uses_same_thread(self):
        def fail():
            raise ValueError("probe failed")

        with self.assertRaisesRegex(ValueError, "probe failed"):
            await self.executor.run(fail)
        worker_id = await self.executor.run(threading.get_ident)
        cleanup_threads = []
        await self.executor.aclose(lambda: cleanup_threads.append(threading.get_ident()))
        self.assertEqual(cleanup_threads, [worker_id])
        await self.executor.aclose(lambda: self.fail("cleanup repeated"))
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            await self.executor.run(lambda: None)

    async def test_shutdown_cancels_queued_work_and_then_cleans_up(self):
        started = threading.Event()
        calls = []

        def blocking():
            started.set()
            self.release.wait(3)
            calls.append("finished")

        active = asyncio.create_task(self.executor.run(blocking))
        await self.wait_started(started)
        queued = asyncio.create_task(self.executor.run(calls.append, "unwanted"))
        await asyncio.sleep(0)
        close = asyncio.create_task(self.executor.aclose(lambda: calls.append("cleanup")))
        await asyncio.sleep(0)
        self.release.set()
        await active
        await close
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertEqual(calls, ["finished", "cleanup"])
