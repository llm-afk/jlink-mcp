"""Control ownership, real Thumb decoding, source ranges and exception-frame tests."""
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import TypeAdapter, ValidationError
from jlink_mcp import api_models as m
from jlink_mcp.fault_context import recover_frame
from jlink_mcp.profiles import ProjectProfile
from jlink_mcp.service import DebugService
from jlink_mcp.source_context import SourceIndex, disassemble


class DebugExtensionTests(unittest.TestCase):
    def setUp(self):
        self.s = DebugService()
        self.p = self.s.probe = Mock()
        self.s.session_id = 's'
        self.s.target = dict(architecture='cortex-m')
        self.p.halted.return_value = True
        self.regs = {'R15 (PC)': 0x08000100, 'R14': 0x08000201, 'R13 (SP)': 0x20000100, 'XPSR': 0x01000000}
        self.p.register_read.side_effect = self.regs.__getitem__
        self.p.cpu_halt_reasons.return_value = [SimpleNamespace(HaltReason=1, Index=0)]
        self.p.hardware_breakpoint_set.return_value = 7
        self.p.breakpoint_clear.return_value = self.p.watchpoint_clear.return_value = True
        self.p.watchpoint_set.return_value = 8
        self.s.profile = ProjectProfile(name='test', chip='test', regions=[
            dict(name='code', address=0x08000000, size=0x1000, kind='flash'),
            dict(name='ram', address=0x20000000, size=0x1000, kind='ram')])

    def until(self, **kwargs):
        return self.s.control(m.RunUntilControl(action='run_until', session_id='s', address=0x08000200, **kwargs))

    def test_wait_never_halts_or_resumes(self):
        self.p.halted.return_value = False
        result = self.s.control(m.WaitControl(action='wait', session_id='s', timeout_ms=1))
        self.assertEqual(result['completion'], 'timeout')
        self.p.halt.assert_not_called()
        self.p.restart.assert_not_called()

    def test_wait_reports_native_reason(self):
        result = self.s.control(m.WaitControl(action='wait', session_id='s'))
        self.assertEqual(result['stop']['reasons'][0]['name'], 'code_breakpoint')
        self.assertEqual(result['stop']['registers']['PC'], 0x08000100)

    def test_resume_reports_immediate_stop(self):
        result = self.s.control(m.RunControl(session_id='s', action='resume'))
        self.assertTrue(result['success'])
        self.assertEqual(result['completion'], 'observed_halted')
        self.assertFalse(result['running_observed'])
        self.p.restart.assert_called_once()

    def test_until_hit_cleans_only_temporary(self):
        self.s.breakpoints['old'] = dict(address=1, handle=3)
        self.p.restart.side_effect = lambda: self.regs.update({'R15 (PC)': 0x08000200})
        result = self.until()
        self.assertEqual(result['completion'], 'target_reached')
        self.assertTrue(result['temporary_breakpoint_removed'])
        self.assertEqual(set(self.s.breakpoints), {'old'})
        self.p.breakpoint_clear.assert_called_once_with(7)

    def test_until_other_stop_not_success(self):
        result = self.until()
        self.assertEqual(result['completion'], 'stopped_elsewhere')
        self.assertFalse(result['success'])
        self.assertFalse(self.s.breakpoints)

    def test_until_at_address_without_break_reason_not_a_hit(self):
        self.p.restart.side_effect = lambda: self.regs.update({'R15 (PC)': 0x08000200})
        self.p.cpu_halt_reasons.return_value = [SimpleNamespace(HaltReason=0, Index=-1)]
        self.assertEqual(self.until()['completion'], 'stopped_elsewhere')

    def test_until_already_at_target_does_not_resume(self):
        self.regs['R15 (PC)'] = 0x08000200
        self.assertEqual(self.until()['completion'], 'already_at_target')
        self.p.restart.assert_not_called()
        self.p.hardware_breakpoint_set.assert_not_called()

    def test_until_reuses_user_breakpoint(self):
        self.s.breakpoint(m.BreakpointAdd(session_id='s', action='set', address=0x08000200))
        result = self.until()
        self.assertIn(result['reused_breakpoint_id'], self.s.breakpoints)
        self.p.breakpoint_clear.assert_not_called()

    def test_timeout_policy_halt(self):
        with patch.object(self.s, '_wait_halt', side_effect=[False, True]):
            result = self.until(on_timeout='halt')
        self.assertEqual(result['completion'], 'timeout')
        self.assertTrue(result['temporary_breakpoint_removed'])
        self.p.halt.assert_called_once()

    def test_timeout_policy_running(self):
        with patch.object(self.s, '_wait_halt', return_value=False):
            result = self.until(on_timeout='running')
        self.assertEqual(result['completion'], 'timeout')
        self.p.halt.assert_not_called()
        self.p.breakpoint_clear.assert_called_once_with(7)

    def test_until_submission_and_cleanup_failures_preserve_handle(self):
        self.p.restart.side_effect = OSError('submission uncertain')
        self.p.breakpoint_clear.return_value = False
        result = self.until()
        self.assertFalse(result['success'])
        self.assertEqual(result['completion'], 'unknown')
        self.assertIn(result['remaining_breakpoint_id'], self.s.breakpoints)
        self.assertFalse(result['temporary_breakpoint_removed'])
        self.assertEqual(self.p.restart.call_count, 1)

    def test_data_watchpoint_access_and_cleanup(self):
        for kind, rd, wr in [('read', True, False), ('write', False, True), ('access', True, True)]:
            with self.subTest(kind=kind):
                result = self.s.breakpoint(m.BreakpointAdd(session_id='s', action='set', kind=kind, address=0x20000000, size=4))
                self.p.watchpoint_set.assert_called_with(0x20000000, data_mask=0xffffffff, access_size=32, read=rd, write=wr)
                self.s.breakpoint(m.BreakpointRemove(session_id='s', action='remove', breakpoint_id=result['breakpoint_id']))
                self.p.watchpoint_clear.assert_called_with(8)
        self.p.breakpoint_clear.assert_not_called()

    def test_watchpoint_unknown_variable_mismatch_and_alignment(self):
        self.s.symbols = Mock()
        with self.assertRaisesRegex(Exception, 'verify_image'):
            self.s.breakpoint(m.BreakpointAdd(session_id='s', action='set', kind='write', variable='counter'))
        with self.assertRaises(Exception):
            self.s.breakpoint(m.BreakpointAdd(session_id='s', action='set', kind='write', address=0x20000001, size=4))
        self.p.watchpoint_set.assert_not_called()

    def test_variable_watchpoint_resolves_bounded_type(self):
        self.s.symbols = Mock()
        self.s.symbols.variable.return_value = dict(address=0x20000000, size=4, type=dict(kind='float', size=4))
        self.s.image_match = dict(status='matched')
        result = self.s.breakpoint(m.BreakpointAdd(session_id='s', action='set', kind='write', variable='motor.iq'))
        self.assertEqual(result['size'], 4)
        self.assertEqual(result['address'], 0x20000000)

    def test_failed_watchpoint_clear_keeps_ownership(self):
        self.s.breakpoints['wp'] = dict(kind='write', handle=8, address=0x20000000, size=4)
        self.p.watchpoint_clear.return_value = False
        with self.assertRaises(Exception):
            self.s._clear_breakpoint('wp')
        self.assertIn('wp', self.s.breakpoints)

    def test_context_actual_bytes_without_elf(self):
        self.p.memory_read.return_value = list(bytes.fromhex('01207047'))
        result = self.s.inspect(m.InspectContext(session_id='s', action='context', instructions=1))
        self.assertTrue(result['success'])
        self.assertEqual(result['instructions'][0]['mnemonic'], 'movs')
        self.assertEqual(result['instructions'][0]['bytes'], '0120')
        self.assertEqual(result['instruction_bytes']['source'], 'target memory')

    def test_context_rejects_peripheral_address(self):
        result = self.s.inspect(m.InspectContext(session_id='s', action='context', address=0x40000000))
        self.assertFalse(result['success'])
        self.p.memory_read.assert_not_called()

    def test_context_rejects_running_core(self):
        self.p.halted.return_value = False
        with self.assertRaises(Exception):
            self.s.inspect(m.InspectContext(session_id='s', action='context'))
        self.p.memory_read.assert_not_called()

    def test_halt_reason_failure_does_not_invent_reason(self):
        self.p.cpu_halt_reasons.side_effect = OSError('unsupported')
        result = self.s._stop_context()
        self.assertEqual(result['reasons'], [])
        self.assertTrue(result['errors'])
        self.assertEqual(result['registers']['PC'], 0x08000100)

    def test_contracts_reject_nonsensical_combinations(self):
        invalid = [dict(kind='write', address=0x20000000), dict(kind='execute', variable='counter'),
                   dict(kind='write', symbol='main'), dict(address=1, symbol='main'), dict(kind='write', address=4, size=8)]
        for params in invalid:
            with self.subTest(params=params), self.assertRaises(ValidationError):
                m.BreakpointAdd(session_id='s', action='set', **params)
        with self.assertRaises(ValidationError):
            m.InspectFault(session_id='s', action='fault', frame_address=0x20000000)
        with self.assertRaises(ValidationError):
            TypeAdapter(m.ControlRequest).validate_python(dict(session_id='s', action='wait', on_timeout='halt'))


class ExceptionFrameTests(unittest.TestCase):
    def setUp(self):
        self.regs = dict(PC=0x08000100, LR=0xfffffff9, MSP=0x20000100, PSP=0x20000200, XPSR=0x01000003)
        self.words = [1, 2, 3, 4, 12, 0x08000301, 0x08000200, 0x01000000]
        self.reads = []
        self.faults = {'CFSR': 0}

    def read(self, addr, size):
        self.reads.append((addr, size))
        if addr == 0xe000ed08:
            return struct.pack('<I', 0x08000000)
        if addr == 0x0800000c:
            return struct.pack('<I', 0x08000101)
        return struct.pack('<8I', *self.words)

    def recover(self, **kwargs):
        return recover_frame(self.regs, self.faults, self.read,
                             lambda a, n: 0x20000000 <= a and a+n <= 0x20001000,
                             lambda a, n: 0x08000000 <= a and a+n <= 0x08001000, **kwargs)

    def test_undefined_instruction_escalation_flags(self):
        from jlink_mcp.fault_context import decode_fault_flags
        self.assertEqual(decode_fault_flags({'CFSR': 1 << 16, 'HFSR': 1 << 30}),
                         {'CFSR': ['UNDEFINSTR'], 'HFSR': ['FORCED']})

    def test_msp_entry_frame_and_padding(self):
        self.words[-1] |= 1 << 9
        result = self.recover()
        self.assertEqual(result['status'], 'decoded')
        self.assertEqual(result['registers']['PC'], 0x08000200)
        self.assertEqual(result['pre_exception_sp'], 0x20000124)

    def test_psp_extended_frame_skips_fp_payload(self):
        self.regs['LR'] = 0xffffffed
        result = self.recover()
        self.assertEqual(result['status'], 'decoded')
        self.assertEqual(result['stack'], 'PSP')
        self.assertIn((0x20000248, 32), self.reads)
        self.assertTrue(result['extended_fp_frame'])

    def test_moved_sp_requires_explicit_frame(self):
        self.regs['PC'] += 2
        self.assertEqual(self.recover()['status'], 'unavailable')
        result = self.recover(frame_address=0x20000100, exc_return=0xfffffff9)
        self.assertEqual(result['status'], 'decoded')
        self.assertEqual(result['origin'], 'caller supplied')

    def test_reject_invalid_frame_bounds_and_thumb_bit(self):
        result = self.recover(frame_address=0x40000000, exc_return=0xfffffff9)
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(self.reads)
        self.words[-1] = 0
        self.assertEqual(self.recover()['status'], 'invalid')

    def test_stacking_error_or_unknown_lr_never_scans_stack(self):
        self.faults['CFSR'] = 1 << 12
        self.assertEqual(self.recover()['status'], 'unavailable')
        self.assertFalse(self.reads)
        self.faults['CFSR'] = 0
        self.regs['LR'] = 0x08000501
        self.assertEqual(self.recover()['status'], 'unavailable')
        self.assertFalse(self.reads)


class SourceContextTests(unittest.TestCase):
    def test_invalid_line_file_index_not_negative_indexing(self):
        from jlink_mcp.source_context import source_path
        with self.assertRaises(ValueError):
            source_path(Mock(), {'version': 4, 'file_entry': [Mock()], 'include_directory': []}, 0)

    def test_thumb_instruction_boundaries(self):
        result = disassemble(bytes.fromhex('01207047'), 0x08000000, 2)
        self.assertEqual([r['address'] for r in result], [0x08000000, 0x08000002])
        self.assertEqual(result[1]['mnemonic'], 'bx')

    def test_line_sequences_do_not_bridge_gaps(self):
        cu = Mock()
        cu.get_top_DIE.return_value.attributes = {'DW_AT_comp_dir': SimpleNamespace(value=b'C:\\build')}
        def state(addr, line, end=False):
            return SimpleNamespace(state=SimpleNamespace(address=addr, line=line, column=1, file=1, end_sequence=end))
        header = {'version': 4, 'include_directory': [b'..\\src'],
                  'file_entry': [SimpleNamespace(name=b'main.c')]}
        class Entry:
            name = b'main.c'
            def __getitem__(self, key):
                return 1
        header['file_entry'] = [Entry()]
        program = Mock()
        program.get_entries.return_value = [state(0x1000, 10), state(0x1004, 11, True), state(0x2000, 20), state(0x2004, 21, True)]
        class Program:
            def __getitem__(self, key):
                return header[key]
            def get_entries(self):
                return program.get_entries()
        dwarf = Mock()
        dwarf.iter_CUs.return_value = [cu]
        dwarf.line_program_for_CU.return_value = Program()
        elf = Mock()
        elf.get_dwarf_info.return_value = dwarf
        index = SourceIndex(elf)
        self.assertEqual(index.locate(0x1002)[0]['line'], 10)
        self.assertEqual(index.locate(0x1002)[0]['file'], r'C:\src\main.c')
        self.assertEqual(index.locate(0x1800), [])
        self.assertEqual(index.locate(0x2004), [])
