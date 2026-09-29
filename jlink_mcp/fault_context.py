"""Conservative ARMv7-M exception-frame recovery; never scan for a guessed stack."""
import struct

EXC_RETURNS = {0xfffffff1, 0xfffffff9, 0xfffffffd, 0xffffffe1, 0xffffffe9, 0xffffffed}


def decode_fault_flags(faults):
    bits = {
        'CFSR': {0: 'IACCVIOL', 1: 'DACCVIOL', 3: 'MUNSTKERR', 4: 'MSTKERR', 5: 'MLSPERR',
                 7: 'MMARVALID', 8: 'IBUSERR', 9: 'PRECISERR', 10: 'IMPRECISERR', 11: 'UNSTKERR',
                 12: 'STKERR', 13: 'LSPERR', 15: 'BFARVALID', 16: 'UNDEFINSTR', 17: 'INVSTATE',
                 18: 'INVPC', 19: 'NOCP', 24: 'UNALIGNED', 25: 'DIVBYZERO'},
        'HFSR': {1: 'VECTTBL', 30: 'FORCED', 31: 'DEBUGEVT'},
    }
    return {reg: [name for bit, name in mapping.items() if faults[reg] & (1 << bit)]
            for reg, mapping in bits.items() if reg in faults}


def recover_frame(registers, faults, read, in_ram, in_memory, frame_address=None, exc_return=None):
    explicit = frame_address is not None
    exc_return = exc_return if explicit else registers.get('LR')
    if exc_return not in EXC_RETURNS:
        return {'status': 'unavailable', 'reason': 'LR is not an ARMv7-M EXC_RETURN; supply a known frame_address and exc_return'}
    if faults.get('CFSR') is None:
        return {'status': 'unavailable', 'reason': 'CFSR unavailable; cannot check stacking errors'}
    if faults['CFSR'] & ((1 << 4) | (1 << 5) | (1 << 12) | (1 << 13)):
        return {'status': 'unavailable', 'reason': 'Stacking or lazy FP preservation error; frame is not trusted'}
    stack_name = 'PSP' if exc_return & 4 else 'MSP'
    if not explicit:
        exception = registers.get('XPSR', 0) & 0x1ff
        if not exception:
            return {'status': 'unavailable', 'reason': 'Core is not in an exception handler'}
        vtor = int.from_bytes(read(0xe000ed08, 4), 'little')
        vector = vtor + exception * 4
        if not in_memory(vector, 4):
            return {'status': 'unavailable', 'reason': 'Exception vector lies outside configured memory'}
        entry = int.from_bytes(read(vector, 4), 'little') & ~1
        if registers.get('PC', 0) & ~1 != entry:
            return {'status': 'unavailable', 'reason': 'Not halted at handler entry; SP may have moved. Supply a known frame_address and exc_return'}
        frame_address = registers.get(stack_name)
    extended = not bool(exc_return & 0x10)
    offset, frame_size = (72, 104) if extended else (0, 32)
    if frame_address is None or frame_address % 4 or not in_ram(frame_address, frame_size):
        return {'status': 'unavailable', 'reason': 'Frame is unaligned or outside configured RAM'}
    data = read(frame_address + offset, 32)
    if len(data) != 32:
        raise ValueError('Short exception-frame read')
    values = dict(zip(('R0', 'R1', 'R2', 'R3', 'R12', 'LR', 'PC', 'XPSR'), struct.unpack('<8I', data)))
    padding = 4 if values['XPSR'] & (1 << 9) else 0
    valid = bool(values['XPSR'] & (1 << 24)) and in_ram(frame_address, frame_size + padding)
    valid = valid and (bool(values['XPSR'] & 0x1ff) != bool(exc_return & 8))
    return {'status': 'decoded' if valid else 'invalid', 'origin': 'caller supplied' if explicit else 'handler entry',
            'exc_return': exc_return, 'stack': stack_name, 'frame_address': frame_address,
            'extended_fp_frame': extended, 'core_frame_address': frame_address + offset,
            'alignment_padding': padding, 'pre_exception_sp': frame_address + frame_size + padding,
            'registers': values, 'data_hex': data.hex(),
            'note': 'One exception frame, not a call stack. FP payload is not decoded. Caller-supplied frame provenance is not verified.'}
