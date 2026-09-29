"""Offline ELF symbol index. No target access and no guessed C types."""
import hashlib
import io
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from .dwarf_variables import DwarfVariables
from .source_context import SourceIndex


class SymbolFile:
    def __init__(self, path: str):
        self.path = str(Path(path).resolve(strict=True))
        if Path(self.path).stat().st_size > 128 * 1024 * 1024:
            raise ValueError("ELF exceeds 128 MiB")
        self._content = Path(self.path).read_bytes()
        self.sha256 = hashlib.sha256(self._content).hexdigest()
        self._stream = io.BytesIO(self._content)
        self.elf = ELFFile(self._stream)
        self._variables = None
        self._sources = None
        self.symbols = {}
        elf = self.elf
        if elf.elfclass != 32 or not elf.little_endian or elf["e_type"] != "ET_EXEC":
            raise ValueError("Only linked 32-bit little-endian executable ELF is supported")
        self.machine = elf["e_machine"]
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for symbol in section.iter_symbols():
                if not symbol.name or symbol["st_shndx"] in ("SHN_UNDEF", "SHN_COMMON"):
                    continue
                kind = symbol["st_info"]["type"]
                if kind not in ("STT_OBJECT", "STT_FUNC"):
                    continue
                entry = {"name": symbol.name, "address": symbol["st_value"],
                         "size": symbol["st_size"], "kind": kind}
                candidates = self.symbols.setdefault(symbol.name, [])
                if entry not in candidates:
                    candidates.append(entry)

    def resolve(self, name: str) -> dict:
        entries = self.symbols.get(name, [])
        if len(entries) != 1:
            raise ValueError(f"Symbol {name!r} is {'ambiguous' if entries else 'not found'}")
        return dict(entries[0])

    def identity(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "machine": self.machine,
                "target_match": "unknown", "types": "bounded static DWARF globals and ELF symbols"}

    def variable(self, expression):
        if self._variables is None:
            self._variables = DwarfVariables(self.elf)
        return self._variables.resolve(expression)

    def locate(self, address):
        address &= ~1
        functions = []
        for entries in self.symbols.values():
            for entry in entries:
                start = entry["address"] & ~1
                if entry["kind"] == "STT_FUNC" and start <= address < start + entry["size"]:
                    functions.append({**entry, "offset": address - start})
        if self._sources is None:
            self._sources = SourceIndex(self.elf)
        return {"address": address, "functions": functions, "sources": self._sources.locate(address),
                "source_errors": self._sources.errors, "note": "ELF mapping; source file contents are not verified"}

    def executable_span(self, address):
        for section in self.elf.iter_sections():
            if section["sh_flags"] & 4 and section["sh_addr"] <= address < section["sh_addr"] + section["sh_size"]:
                return section["sh_addr"] + section["sh_size"] - address
        return 0

    def readonly_sections(self):
        """Map immutable allocated PROGBITS through PT_LOAD file offsets to LMA.

        RAM initialization and NOBITS are deliberately excluded. Sections that
        lack a unique load address are reported as skipped, never guessed.
        """
        sections, skipped = [], []
        segments = [s for s in self.elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        for section in self.elf.iter_sections():
            flags, size = section["sh_flags"], section["sh_size"]
            if not flags & 2 or flags & 1 or section["sh_type"] != "SHT_PROGBITS" or not size:
                continue
            candidates = {s["p_paddr"] + section["sh_offset"] - s["p_offset"] for s in segments
                          if s["p_offset"] <= section["sh_offset"] and
                          section["sh_offset"] + size <= s["p_offset"] + s["p_filesz"]}
            if len(candidates) != 1:
                skipped.append({"section": section.name, "size": size, "reason": "no unique PT_LOAD address"})
                continue
            sections.append({"name": section.name, "address": candidates.pop(), "data": section.data()})
        return sections, skipped
