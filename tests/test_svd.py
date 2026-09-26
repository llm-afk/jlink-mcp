"""Offline SVD parsing, cache and register access regressions."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

from jlink_mcp.models.svd import PeripheralInfo, RegisterInfo
from jlink_mcp.svd_manager import SVDManager
from jlink_mcp.tools import svd


ROOT = Path(__file__).resolve().parents[1]


class SVDTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manager = object.__new__(SVDManager)
        self.manager._devices = {}
        self.manager._svd_file_map = {}
        self.manager._peripheral_index = {}
        self.manager._register_index = {}
        self.manager._cache_dir = self.directory / "cache"
        self.manager._cache_dir.mkdir()
        self.addCleanup(self.manager.clear_cache)

    def parse(self, peripherals, defaults=""):
        path = self.directory / "Device.svd"
        path.write_text(f"<device><name>Device</name>{defaults}<peripherals>"
                        f"{peripherals}</peripherals></device>", encoding="utf-8")
        return self.manager._parse_svd_file(path)

    def test_bundled_dma_register_inheritance(self):
        for name in ("N32H473", "N32H474", "N32H475"):
            with self.subTest(device=name):
                device = self.manager._parse_svd_file(
                    ROOT / "jlink_mcp" / "tool" / "SVD_V1.5.6" / f"{name}.svd")
                dma = next(p for p in device.peripherals if p.name == "DMA1")
                source = next(r for r in dma.registers if r.name == "DMA_CHCFG1")
                inherited = next(r for r in dma.registers if r.name == "DMA_CHCFG2")
                self.assertEqual(len(inherited.fields), 14)
                self.assertEqual(inherited.fields, source.fields)
                self.assertIsNot(inherited.fields[0], source.fields[0])
                self.assertEqual(inherited.access, "read-write")
                self.assertEqual(inherited.reset_value, 0)
                self.assertEqual(inherited.address_offset, 0x14)

    def test_recursive_peripheral_inheritance_merges_local_registers(self):
        device = self.parse("""
            <peripheral derivedFrom="Middle"><name>Child</name><baseAddress>0x5000</baseAddress>
              <registers><register><name>CTRL</name><description>child control</description>
                <fields><field><name>MODE</name><bitWidth>2</bitWidth></field></fields>
              </register></registers></peripheral>
            <peripheral derivedFrom="Base"><name>Middle</name><baseAddress>0x4000</baseAddress>
              <registers><register><name>EXTRA</name><addressOffset>4</addressOffset></register></registers>
            </peripheral>
            <peripheral><name>Base</name><baseAddress>0x3000</baseAddress><groupName>GPIO</groupName>
              <registers><register><name>CTRL</name><addressOffset>0</addressOffset>
                <fields><field><name>MODE</name><bitOffset>3</bitOffset><bitWidth>1</bitWidth></field></fields>
              </register></registers></peripheral>
            """, "<size>16</size><access>read-write</access><resetValue>0x12</resetValue>")
        child, middle, base = device.peripherals
        self.assertEqual(child.group_name, "GPIO")
        self.assertEqual(child.base_address, 0x5000)
        self.assertEqual([r.name for r in child.registers], ["CTRL", "EXTRA"])
        ctrl = child.registers[0]
        self.assertEqual((ctrl.size, ctrl.access, ctrl.reset_value), (16, "read-write", 0x12))
        self.assertEqual(ctrl.description, "child control")
        self.assertEqual((ctrl.fields[0].bit_offset, ctrl.fields[0].bit_width), (3, 2))
        self.assertEqual(base.registers[0].fields[0].bit_width, 1)
        self.assertEqual(middle.registers[0].fields[0].bit_width, 1)

    def test_register_forward_chain_and_qualified_reference(self):
        device = self.parse("""
            <peripheral><name>P</name><baseAddress>0</baseAddress><registers>
              <register derivedFrom="R2"><name>R3</name><addressOffset>8</addressOffset><access>read-only</access></register>
              <register derivedFrom="Q.R1"><name>R2</name><addressOffset>4</addressOffset></register>
            </registers></peripheral>
            <peripheral><name>Q</name><baseAddress>0x1000</baseAddress><size>8</size><registers>
              <register><name>R1</name><addressOffset>0</addressOffset><resetValue>1</resetValue>
                <fields><field><name>EN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field></fields>
              </register></registers></peripheral>
            """)
        register = device.peripherals[0].registers[0]
        self.assertEqual((register.address_offset, register.size, register.reset_value), (8, 8, 1))
        self.assertEqual(register.access, "read-only")
        self.assertEqual(register.fields[0].access, "read-only")

    def test_cycles_and_missing_references_fail_explicitly(self):
        cases = [
            '<peripheral derivedFrom="B"><name>A</name></peripheral><peripheral derivedFrom="A"><name>B</name></peripheral>',
            '<peripheral derivedFrom="Missing"><name>A</name></peripheral>',
            '<peripheral><name>A</name><registers><register derivedFrom="Y"><name>X</name></register><register derivedFrom="X"><name>Y</name></register></registers></peripheral>',
            '<peripheral><name>A</name><registers><register derivedFrom="Missing"><name>X</name></register></registers></peripheral>',
        ]
        for xml in cases:
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                self.parse(xml)

    def source(self, directory, address):
        directory.mkdir(exist_ok=True)
        source = directory / "Same.svd"
        source.write_text(f"<device><name>Same</name><peripherals><peripheral><name>P</name>"
                          f"<baseAddress>{address}</baseAddress></peripheral></peripherals></device>",
                          encoding="utf-8")
        return source

    def test_cache_isolates_sources_and_rejects_changed_content(self):
        a = self.source(self.directory / "a", 0x4000)
        b = self.source(self.directory / "b", 0x5000)
        self.manager._svd_file_map["Same"] = a
        self.manager._save_to_cache("Same", self.manager._parse_svd_file(a))
        cache_a = self.manager._get_cache_path("Same")
        self.assertEqual(self.manager._load_from_cache("Same").peripherals[0].base_address, 0x4000)
        self.manager._svd_file_map["Same"] = b
        self.assertNotEqual(cache_a, self.manager._get_cache_path("Same"))
        self.assertIsNone(self.manager._load_from_cache("Same"))
        self.manager._svd_file_map["Same"] = a
        timestamp = a.stat().st_mtime_ns
        self.source(a.parent, 0x6000)
        os.utime(a, ns=(timestamp, timestamp))
        self.assertIsNone(self.manager._load_from_cache("Same"))

    def test_corrupt_cache_falls_back_to_parsing(self):
        source = self.source(self.directory / "a", 0x4000)
        self.manager._svd_file_map["Same"] = source
        path = self.manager._get_cache_path("Same")
        path.write_bytes(b"not json or pickle")
        self.assertEqual(self.manager.get_device("Same").peripherals[0].base_address, 0x4000)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], self.manager.CACHE_VERSION)

    def test_unwritable_cache_does_not_block_initialization_or_query(self):
        with patch.object(SVDManager, "_initialized", False), patch.object(Path, "mkdir", side_effect=PermissionError):
            manager = object.__new__(SVDManager)
            manager.__init__()
        self.assertIsNone(manager._cache_dir)
        self.assertIsNotNone(manager.get_device("GD32C10x"))
        self.addCleanup(manager.clear_cache)

    def test_failed_cache_write_and_clear_are_optional(self):
        source = self.source(self.directory / "a", 0x4000)
        self.manager._svd_file_map["Same"] = source
        with patch("jlink_mcp.svd_manager.tempfile.NamedTemporaryFile", side_effect=PermissionError):
            self.assertIsNotNone(self.manager.get_device("Same"))
        self.manager._save_to_cache("Same", self.manager._devices["Same"])
        with patch.object(Path, "unlink", side_effect=PermissionError):
            self.manager.clear_cache_dir()

    def test_alias_does_not_reload_existing_device(self):
        source = self.source(self.directory / "a", 0x4000)
        self.manager._svd_file_map["Same"] = source
        first = self.manager.get_device("Same")
        with patch.object(self.manager, "_load_from_cache", side_effect=AssertionError("reloaded")):
            self.assertIs(self.manager.get_device("same"), first)

    def test_concurrent_first_queries_share_one_loaded_device(self):
        source = self.source(self.directory / "a", 0x4000)
        self.manager._svd_file_map["Same"] = source
        with patch.object(self.manager, "_parse_svd_file", wraps=self.manager._parse_svd_file) as parser:
            with ThreadPoolExecutor(max_workers=4) as workers:
                devices = list(workers.map(self.manager.get_device, ["Same", "same"] * 4))
        self.assertEqual(parser.call_count, 1)
        self.assertTrue(all(device is devices[0] for device in devices))


class RegisterAccessTests(unittest.TestCase):
    def setUp(self):
        self.jlink = Mock()
        self.jlink.halted.return_value = True
        manager = patch.object(svd, "jlink_manager")
        self.manager = manager.start()
        self.addCleanup(manager.stop)
        self.manager.get_jlink.return_value = self.jlink

    def test_named_register_uses_svd_width(self):
        for width in (8, 16, 32):
            with self.subTest(width=width), patch.object(svd, "svd_manager") as definitions:
                definitions.is_available.return_value = True
                definitions.get_register.return_value = RegisterInfo(name="R", address_offset=0, size=width)
                definitions.get_peripheral.return_value = PeripheralInfo(name="P", base_address=0x4000)
                self.jlink.memory_read.return_value = [0x12]
                result = svd.read_register_with_fields("Device", "P", "R")
                self.assertTrue(result["success"], result)
                self.assertEqual(result["raw_value"], 0x12)
                self.jlink.memory_read.assert_called_with(0x4000, 1, nbits=width)

    def test_address_access_uses_width_and_rejects_short_transfers(self):
        for width in (8, 16, 32):
            with self.subTest(width=width):
                self.jlink.memory_read.return_value = [0x12]
                self.assertTrue(svd.read_register_by_address(0x4000, width)["success"])
                self.jlink.memory_read.assert_called_with(0x4000, 1, nbits=width)
                self.jlink.memory_write.return_value = width // 8
                self.assertTrue(svd.write_register_by_address(0x4000, 0x12, width)["success"])
                self.jlink.memory_write.assert_called_with(0x4000, [0x12], nbits=width)
                self.jlink.memory_read.return_value = []
                self.assertFalse(svd.read_register_by_address(0x4000, width)["success"])
                self.jlink.memory_write.return_value = 0
                self.assertFalse(svd.write_register_by_address(0x4000, 0x12, width)["success"])

    def test_invalid_access_never_reaches_hardware(self):
        for address, width in ((-1, 8), (1, 16), (2, 32), (0, 24)):
            with self.subTest(address=address, width=width):
                self.assertFalse(svd.read_register_by_address(address, width)["success"])
                self.assertFalse(svd.write_register_by_address(address, 0x12, width)["success"])
        for value in (-1, 256):
            self.assertFalse(svd.write_register_by_address(0, value, 8)["success"])
        self.manager.get_jlink.assert_not_called()
        self.jlink.memory_read.assert_not_called()
        self.jlink.memory_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
