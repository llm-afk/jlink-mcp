"""Offline address/source mapping and bounded Cortex-M disassembly."""
import ntpath
import posixpath

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS


def disassemble(data, address, count):
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    return [dict(address=i.address, size=i.size, bytes=i.bytes.hex(), mnemonic=i.mnemonic, operands=i.op_str)
            for i in decoder.disasm(data, address, count=count)]


def _text(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value)


def source_path(cu, program, index):
    version = program['version']
    files, directories = program['file_entry'], program['include_directory']
    file_index = index if version >= 5 else index - 1
    if not 0 <= file_index < len(files):
        raise ValueError('Invalid DWARF line-table file index')
    entry = files[file_index]
    directory = entry['dir_index']
    parts = []
    comp_dir = cu.get_top_DIE().attributes.get('DW_AT_comp_dir')
    if comp_dir:
        parts.append(_text(comp_dir.value))
    if version >= 5 or directory:
        directory_index = directory if version >= 5 else directory - 1
        if not 0 <= directory_index < len(directories):
            raise ValueError('Invalid DWARF line-table directory index')
        parts.append(_text(directories[directory_index]))
    parts.append(_text(entry.name))
    path = ntpath if any('\\' in p or ntpath.splitdrive(p)[0] for p in parts) else posixpath
    return path.normpath(path.join(*parts))


class SourceIndex:
    def __init__(self, elf):
        self.ranges = []
        self.errors = []
        if not elf.has_dwarf_info():
            return
        dwarf = elf.get_dwarf_info()
        for cu in dwarf.iter_CUs():
            try:
                program = dwarf.line_program_for_CU(cu)
                if program is None:
                    continue
                previous = None
                for entry in program.get_entries():
                    state = entry.state
                    if state is None:
                        continue
                    if previous is not None and previous.address < state.address and previous.line:
                        self.ranges.append((previous.address, state.address,
                                            {'file': source_path(cu, program, previous.file),
                                             'line': previous.line, 'column': previous.column}))
                    previous = None if state.end_sequence else state
            except Exception as exc:
                self.errors.append({'cu_offset': cu.cu_offset, 'error': str(exc)})

    def locate(self, address):
        found = []
        for start, end, source in self.ranges:
            if start <= address < end and source not in found:
                found.append(source)
        return found
