"""Shared byte-oriented contracts over pylink's unit-oriented API."""
import time

from .exceptions import JLinkMCPError, JLinkErrorCode


def validate_span(address, size, width=8):
    if width not in (8, 16, 32):
        raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, 'width 必须为 8/16/32')
    unit = width // 8
    if size <= 0 or address < 0 or address + size > 0x100000000:
        raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, '地址或长度超出 32 位地址空间')
    if address % unit or size % unit:
        raise JLinkMCPError(JLinkErrorCode.INVALID_PARAMETER, f'地址和字节长度必须按 {unit} 字节对齐；非整字数据请用 width=8')
    return unit


def read_bytes(jlink, address, size, width=8):
    unit = validate_span(address, size, width)
    values = jlink.memory_read(address, size // unit, nbits=width)
    if len(values) != size // unit:
        raise JLinkMCPError(JLinkErrorCode.READ_FAILED, f'短读：期望 {size // unit} 单元，实际 {len(values)}')
    return b''.join(int(v).to_bytes(unit, 'little') for v in values)


def write_bytes(jlink, address, data, width=8):
    unit = validate_span(address, len(data), width)
    values = [int.from_bytes(data[i:i+unit], 'little') for i in range(0, len(data), unit)]
    # pylink memory_write returns the DLL byte count, even with nbits set.
    written = jlink.memory_write(address, values, nbits=width)
    if written != len(data):
        raise JLinkMCPError(JLinkErrorCode.WRITE_FAILED, f'短写：期望 {len(data)} 字节，实际 {written}；不要自动重试有副作用的寄存器')
    return written


def ensure_halted(jlink):
    """Never reset as a fallback and never consume registers without a stop."""
    if not jlink.halted():
        jlink.halt()
    deadline = time.monotonic() + 1.0
    while not jlink.halted():
        if time.monotonic() >= deadline:
            raise JLinkMCPError(JLinkErrorCode.HALT_FAILED, '无法稳定暂停；请检查看门狗/低功耗。仅在允许复位时使用 reset_target("halt")')
        time.sleep(0.005)


def register_name(name):
    return {'PC': 'R15 (PC)', 'R15': 'R15 (PC)', 'SP': 'R13 (SP)',
            'R13': 'R13 (SP)', 'LR': 'R14'}.get(name.upper(), name.upper())
