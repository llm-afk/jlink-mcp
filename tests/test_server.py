import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import TypeAdapter, ValidationError

from jlink_mcp import server
from jlink_mcp.executor import SerialToolExecutor
from jlink_mcp.models.parameters import Address


class AddressTests(unittest.TestCase):
    def test_accepts_decimal_and_hex_addresses(self):
        adapter = TypeAdapter(Address)
        for value in (134217728, "134217728", "0x08000000", " 0X08000000 "):
            with self.subTest(value=value):
                self.assertEqual(adapter.validate_python(value), 0x08000000)

    def test_rejects_invalid_addresses(self):
        adapter = TypeAdapter(Address)
        for value in (True, False, 1.5, -1, 0x100000000, "-1", "garbage", "0x", ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                adapter.validate_python(value)


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hardware = SerialToolExecutor("test-hardware")
        self.metadata = SerialToolExecutor("test-metadata")
        self.patcher = patch.multiple(server, hardware_executor=self.hardware, metadata_executor=self.metadata)
        self.patcher.start()

    async def asyncTearDown(self):
        await self.hardware.aclose()
        await self.metadata.aclose()
        self.patcher.stop()

    async def test_mcp_validates_hex_address_before_hardware_dispatch(self):
        read = Mock(return_value={"success": True, "data": [0, 0, 0, 0]})
        with patch.object(server, "_read_memory", read):
            await server.mcp.call_tool("read_memory", {"address": "0x20000000", "size": 4})
        read.assert_called_once_with(0x20000000, 4, 32)

    async def test_invalid_address_does_not_reach_hardware(self):
        with patch.object(server, "_write_memory") as write:
            with self.assertRaisesRegex(Exception, "地址|validation"):
                await server.mcp.call_tool("write_memory", {"address": True, "data": "00"})
        write.assert_not_called()

    async def test_numeric_and_erase_flags_are_not_coerced(self):
        with patch.object(server, "_write_register") as write:
            for value in (True, 4.0, "4"):
                with self.subTest(value=value), self.assertRaisesRegex(Exception, "validation"):
                    await server.mcp.call_tool("write_register", {"register_name": "R0", "value": value})
        write.assert_not_called()
        with patch.object(server, "_erase_flash") as erase:
            with self.assertRaisesRegex(Exception, "validation"):
                await server.mcp.call_tool("erase_flash", {"chip_erase": 1})
        erase.assert_not_called()

    async def test_tool_schemas_and_new_arguments(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(len(tools), 44)
        self.assertIn("block_address", tools["rtt_start"].inputSchema["properties"])
        self.assertIn("transfer_connection", tools["start_gdb_server"].inputSchema["properties"])
        with patch.object(server, "_rtt_start", return_value={"success": True}) as start:
            await server.mcp.call_tool("rtt_start", {"block_address": "0x20001000"})
        start.assert_called_once_with(0, "continuous", 1000, 0x20001000)

    async def test_shutdown_cleans_both_executors_and_connection(self):
        with patch.object(server, "_cleanup_hardware") as cleanup:
            async with server.server_lifespan(server.mcp):
                await self.hardware.run(lambda: None)
                await self.metadata.run(lambda: None)
        cleanup.assert_called_once_with()
        with self.assertRaises(RuntimeError):
            await self.hardware.run(lambda: None)
        with self.assertRaises(RuntimeError):
            await self.metadata.run(lambda: None)


class StdioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_server_initialization_and_offline_tools(self):
        async def exercise():
            with tempfile.TemporaryDirectory(prefix="jlink-mcp-test-") as directory:
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "jlink_mcp"],
                    cwd=Path(__file__).resolve().parents[1],
                    env={"LOCALAPPDATA": directory, "XDG_CACHE_HOME": directory, "PYTHONUTF8": "1"},
                )
                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual(len(tools.tools), 44)
                        await session.send_ping()
                        result = await session.call_tool("get_connection_status", {})
                        self.assertFalse(result.isError)
                        data = json.loads(result.content[0].text)
                        self.assertTrue(data["success"])
                        self.assertFalse(data["data"]["connected"])
                        result = await session.call_tool("list_svd_devices", {})
                        data = json.loads(result.content[0].text)
                        self.assertTrue(data["success"])
                        self.assertEqual(data["count"], 4)
                        result = await session.call_tool("get_usage_guidance", {"category": "memory"})
                        self.assertFalse(result.isError)
                        data = json.loads(result.content[0].text)
                        self.assertIn("内存操作", data["tools"])
        await asyncio.wait_for(exercise(), timeout=30)
