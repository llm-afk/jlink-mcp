"""A tiny real ELF fixture exercises parsing without a compiler or board."""
import struct
import tempfile
import unittest
from pathlib import Path

from jlink_mcp.symbols import SymbolFile


def elf_fixture(duplicate=False):
    names = b"\x00counter\x00checkpoint\x00"
    records = [b"\0" * 16,
               struct.pack("<IIIBBH", 1, 0x20000000, 4, 0x11, 0, 1),
               struct.pack("<IIIBBH", 9, 0x08000101, 8, 0x12, 0, 1)]
    if duplicate:
        records.append(struct.pack("<IIIBBH", 1, 0x20000004, 4, 0x11, 0, 1))
    symbols = b"".join(records)
    section_offset = 52 + len(names) + len(symbols)
    ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    header = struct.pack("<16sHHIIIIIHHHHHH", ident, 2, 40, 1, 0, 0, section_offset, 0, 52, 0, 0, 40, 3, 0)
    string_section = struct.pack("<IIIIIIIIII", 0, 3, 0, 0, 52, len(names), 0, 0, 1, 0)
    symbol_section = struct.pack("<IIIIIIIIII", 0, 2, 0, 0, 52 + len(names), len(symbols), 1, 1, 4, 16)
    return header + names + symbols + b"\0" * 40 + string_section + symbol_section


class SymbolTests(unittest.TestCase):
    def test_object_and_function_identity(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fixture.elf"
            path.write_bytes(elf_fixture())
            symbols = SymbolFile(str(path))
            self.assertEqual(symbols.resolve("counter")["size"], 4)
            self.assertEqual(symbols.resolve("checkpoint")["address"], 0x08000101)
            self.assertEqual(symbols.identity()["target_match"], "unknown")
            self.assertEqual(len(symbols.sha256), 64)
            with self.assertRaises(ValueError):
                symbols.resolve("missing")

    def test_ambiguous_static_symbol_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fixture.elf"
            path.write_bytes(elf_fixture(duplicate=True))
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                SymbolFile(str(path)).resolve("counter")

    def test_relocatable_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fixture.o"
            data = bytearray(elf_fixture())
            data[16:18] = struct.pack("<H", 1)
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "executable ELF"):
                SymbolFile(str(path))
