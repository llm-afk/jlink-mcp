"""Real DWARF union fixtures and typed writes over simulated RAM; no hardware."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import TypeAdapter, ValidationError
from jlink_mcp import api_models as m
from jlink_mcp.dwarf_variables import decode, describe, encode, member_offset
from jlink_mcp.service import DebugService
from jlink_mcp.symbols import SymbolFile
from test_optimization import dwarf_elf, profile


class UnionWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'union.elf'
        path.write_bytes(dwarf_elf(unions=True))
        self.symbols = SymbolFile(str(path))
        self.ram = bytearray(b'\xa5' * 1024)
        self.probe = Mock()
        self.probe.halted.return_value = True
        self.probe.memory_read.side_effect = self.read_memory
        self.probe.memory_write.side_effect = self.write_memory
        self.service = DebugService()
        self.service.probe = self.probe
        self.service.session_id = 'test'
        self.service.target = {'architecture': 'cortex-m'}
        self.service.profile, self.service.symbols = profile(), self.symbols
        self.service.image_match = {'status': 'matched'}
        for p in (patch('jlink_mcp.service.jlink_manager') , patch('jlink_mcp.service.gdb_server_manager')):
            manager = p.start()
            self.addCleanup(p.stop)
            manager.get_jlink.return_value = self.probe
            manager.is_running = False

    def read_memory(self, address, count, nbits=8):
        start, width = address - 0x20000000, nbits // 8
        assert 0 <= start <= len(self.ram) - count * width
        return [int.from_bytes(self.ram[start+i*width:start+(i+1)*width], 'little') for i in range(count)]

    def write_memory(self, address, values, nbits=8):
        raw = b''.join(v.to_bytes(nbits // 8, 'little') for v in values)
        start = address - 0x20000000
        assert 0 <= start <= len(self.ram) - len(raw)
        self.ram[start:start+len(raw)] = raw
        return len(raw)

    def write(self, name, value, **options):
        return self.service.invoke('write', m.WriteRequest(session_id='test',
            item=dict(kind='variable', name=name, value=value), **options))

    def match_image(self):
        def target_read(address, count, nbits=8):
            if address == 0x08000000 and count == 1 and nbits == 32:
                return [int.from_bytes(b'CODE', 'little')]
            return self.read_memory(address, count, nbits)
        self.probe.memory_read.side_effect = target_read
        matched = self.service.invoke('firmware', m.FirmwareVerifyImage(action='verify_image', session_id='test'))
        self.assertEqual(matched['status'], 'matched')
        return self.service.image_match

    def test_named_union_paths_offsets_and_array_index(self):
        for name, address, size in [('packet.payload.u32', 0x20000044, 4),
                                     ('packet.payload.real', 0x20000044, 4),
                                     ('packet.payload.pair.b', 0x20000048, 4),
                                     ('packet.payload.bytes[15]', 0x20000053, 1),
                                     ('payloads[1].pair.a', 0x20000090, 4)]:
            with self.subTest(name=name):
                plan = self.symbols.variable(name)
                self.assertEqual((plan['address'], plan['size']), (address, size))

    def test_whole_union_and_parent_preserve_opaque_bytes(self):
        info = self.symbols.variable('packet')
        raw = struct.pack('<I', 9) + bytes(range(16)) + struct.pack('<I', 77)
        value = decode(info['type'], raw)
        self.assertEqual(value, {'tag': 9, 'payload': {'data_hex': bytes(range(16)).hex(), 'active_member': None}, 'trailer': 77})
        self.assertIn('pair', info['type']['fields'][1]['type']['members'])

    def test_union_array_decode_has_no_inferred_active_member(self):
        schema = self.symbols.variable('payloads')['type']
        result = decode(schema, bytes(range(32)))
        self.assertEqual(result[1], {'data_hex': bytes(range(16, 32)).hex(), 'active_member': None})

    def test_union_offset_defaults_only_for_union(self):
        member = SimpleNamespace(attributes={})
        self.assertEqual(member_offset(member, union=True), 0)
        with self.assertRaises(ValueError):
            member_offset(member)
        for offset in (-1, 1, 65537):
            member.attributes = {'DW_AT_data_member_location': SimpleNamespace(value=offset, form='DW_FORM_data1')}
            with self.assertRaises(ValueError):
                member_offset(member, union=True)
        member.attributes = {'DW_AT_bit_size': SimpleNamespace(value=1)}
        with self.assertRaises(ValueError):
            member_offset(member, union=True)

    def test_union_paths_reject_unknown_member_and_out_of_bounds(self):
        for name in ('packet.payload.nope', 'packet.payload.bytes[16]', 'payloads[2].u32', 'packet.payload.u32.nope'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.symbols.variable(name)

    def test_selected_member_cannot_exceed_union_storage(self):
        path = Path(self.temp.name) / 'undersized.elf'
        raw = dwarf_elf(unions=True)
        raw = raw.replace(b'\x09Payload\0\x10', b'\x09Payload\0\x02')
        path.write_bytes(raw)
        symbols = SymbolFile(str(path))
        with self.assertRaisesRegex(ValueError, 'bounds'):
            symbols.variable('packet.payload.u32')
        self.probe.memory_write.assert_not_called()

    def test_opaque_union_rejects_ambiguous_or_anonymous_member_lists(self):
        def member(name):
            return SimpleNamespace(tag='DW_TAG_member', attributes={
                'DW_AT_name': SimpleNamespace(value=name)})
        for members in ([member(b'u32'), member(b'u32')], [member(b'')], []):
            die = SimpleNamespace(tag='DW_TAG_union_type', attributes={
                'DW_AT_name': SimpleNamespace(value=b'Payload'),
                'DW_AT_byte_size': SimpleNamespace(value=16, form='DW_FORM_data1')},
                iter_children=lambda: iter(members))
            with self.subTest(members=members), self.assertRaises(ValueError):
                describe(die)

    def test_cached_union_choices_and_restrictions_are_isolated(self):
        original = self.symbols.variable('readonly_packet.payload')
        changed = self.symbols.variable('readonly_packet.payload')
        changed['type']['members'].clear()
        changed['write_restrictions'].clear()
        self.assertEqual(self.symbols.variable('readonly_packet.payload'), original)
        self.assertEqual(self.symbols.variable('packet.payload.u32')['write_restrictions'], [])

    def test_read_union_leaf_uses_typed_decoder(self):
        self.ram[0x44:0x48] = struct.pack('<f', 1.25)
        result = self.service.invoke('read', m.ReadBatch(action='read', session_id='test',
            items=[dict(kind='variable', name='packet.payload.real')]))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['items'][0]['data']['value'], 1.25)

    def test_scalar_write_preserves_union_tail_and_neighbors(self):
        proof = self.match_image()
        original = self.ram[:]
        result = self.write('packet.payload.u32', 0x12345678, verify=True)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['verification'], 'matched')
        self.assertEqual(result['variable']['elf_sha256'], self.symbols.sha256)
        expected = original[:]
        expected[0x44:0x48] = b'\x78\x56\x34\x12'
        self.assertEqual(self.ram, expected)
        self.probe.memory_write.assert_called_once_with(0x20000044, [0x12345678], nbits=32)
        self.assertEqual(result['elf_match_after'], 'matched')
        self.assertIs(self.service.image_match, proof)
        self.assertTrue(self.write('packet.payload.flag', True, verify=True)['success'])
        self.assertIs(self.service.image_match, proof)

    def test_unverified_typed_write_expires_flash_evidence(self):
        self.match_image()
        result = self.write('packet.payload.u32', 7)
        self.assertEqual(result['elf_match_after'], 'unknown')
        blocked = self.write('packet.payload.u32', 8)
        self.assertFalse(blocked['submitted'])
        self.assertEqual(blocked['error']['code'], 'IMAGE_NOT_MATCHED')
        self.probe.memory_write.assert_called_once()

    def test_preflight_rejection_preserves_evidence_and_never_submits(self):
        proof = self.match_image()
        for name, value in [('readonly_packet.payload.u32', 1), ('packet.payload.u32', -1)]:
            result = self.write(name, value, verify=True)
            self.assertEqual(result['completion'], 'not_submitted')
            self.assertFalse(result['submitted'])
            self.assertIs(self.service.image_match, proof)
        self.probe.memory_write.assert_not_called()

    def test_late_speed_change_invalidates_verified_ram_evidence(self):
        self.match_image()
        self.service.target['requested_speed_khz'] = self.probe.speed = 10000
        def change_speed(*args, **kwargs):
            self.probe.speed = 2000
            return self.write_memory(*args, **kwargs)
        self.probe.memory_write.side_effect = change_speed
        result = self.write('packet.payload.u32', 5, verify=True)
        self.assertEqual(result['error']['code'], 'SPEED_MISMATCH')
        self.assertEqual(result['completion'], 'unknown')
        self.assertEqual(self.service.image_match['status'], 'unknown')
        self.probe.memory_write.assert_called_once()

    def test_array_write_encodes_exact_length_without_padding(self):
        result = self.write('packet.payload.bytes', list(range(16)))
        self.assertTrue(result['success'], result)
        self.assertEqual(self.ram[0x44:0x54], bytes(range(16)))
        self.assertEqual(self.ram[0x43], 0xa5)
        self.assertEqual(self.ram[0x54], 0xa5)
        self.probe.memory_read.assert_not_called()

    def test_nested_signed_float_bool_enum_writes(self):
        for name, value, expected in [('packet.payload.pair.a', -17, struct.pack('<i', -17)),
                                     ('packet.payload.pair.b', 1.5, struct.pack('<f', 1.5)),
                                     ('packet.payload.flag', True, b'\x01'),
                                     ('packet.payload.mode', 'BAD', b'\xff' * 4)]:
            with self.subTest(name=name):
                self.service.image_match = {'status': 'matched'}
                result = self.write(name, value)
                self.assertTrue(result['success'], result)
                offset = result['variable']['address'] - 0x20000000
                self.assertEqual(self.ram[offset:offset+len(expected)], expected)

    def test_invalid_values_never_partially_write(self):
        for name, value in [('packet.payload.u32', -1), ('packet.payload.u32', 2**32),
                             ('packet.payload.u32', True), ('packet.payload.u32', 1.0),
                             ('packet.payload.flag', 1), ('packet.payload.mode', 'missing'),
                             ('packet.payload.real', float('nan')), ('packet.payload.real', 1e100),
                             ('packet.payload.bytes', [1] * 15), ('packet.payload.bytes', [0] * 15 + [256]),
                             ('packet.payload', 1), ('packet.payload.pair', 1)]:
            with self.subTest(name=name, value=value):
                result = self.write(name, value)
                self.assertFalse(result['success'], result)
                self.probe.memory_write.assert_not_called()
        self.assertEqual(self.ram, bytearray(b'\xa5' * 1024))

    def test_match_and_halt_cannot_be_bypassed(self):
        self.service.image_match = {'status': 'unknown'}
        self.assertEqual(self.write('packet.payload.u32', 1)['error']['code'], 'IMAGE_NOT_MATCHED')
        self.service.image_match = {'status': 'matched'}
        self.assertEqual(self.write('packet.payload.u32', 1, consistency='live')['error']['code'], 'REQUIRES_HALT')
        self.probe.halted.return_value = False
        self.assertEqual(self.write('packet.payload.u32', 1)['error']['code'], 'REQUIRES_HALT')
        self.probe.halt.assert_not_called()
        self.probe.memory_write.assert_not_called()

    def test_const_parents_array_elements_and_atomic_rejected(self):
        for name, value in [('readonly_packet.payload.u32', 1), ('readonly_bytes', [1,2,3,4]), ('atomic_counter', 1)]:
            with self.subTest(name=name):
                self.assertTrue(self.symbols.variable(name)['write_restrictions'])
                self.assertEqual(self.write(name, value)['error']['code'], 'READ_ONLY')
                self.probe.memory_write.assert_not_called()

    def test_flash_and_outside_profile_rejected(self):
        for address, error in [(0x08000000, 'RAM_REQUIRED'), (0x40000000, 'OUTSIDE_PROFILE')]:
            plan = dict(address=address, size=4, type=dict(kind='unsigned', size=4))
            with patch.object(self.symbols, 'variable', return_value=plan):
                self.assertEqual(self.write('packet.payload.u32', 1)['error']['code'], error)
        self.probe.memory_write.assert_not_called()

    def test_verification_mismatch_does_not_retry(self):
        self.match_image()
        self.probe.memory_read.side_effect = None
        self.probe.memory_read.return_value = [0]
        result = self.write('packet.payload.u32', 1, verify=True)
        self.assertFalse(result['success'])
        self.assertEqual(result['verification'], 'mismatch')
        self.assertEqual(result['elf_match_after'], 'unknown')
        self.probe.memory_write.assert_called_once()

    def test_write_timeout_is_unknown_and_not_retried(self):
        self.probe.memory_write.side_effect = TimeoutError('timeout')
        result = self.write('packet.payload.u32', 1)
        self.assertFalse(result['success'])
        self.assertEqual(result['completion'], 'unknown')
        self.assertFalse(result['retry_safe'])
        self.probe.memory_write.assert_called_once()

    def test_cpu_resume_during_write_marks_failure(self):
        def resumed(*args, **kwargs):
            self.probe.halted.return_value = False
            return 4
        self.probe.memory_write.side_effect = resumed
        result = self.write('packet.payload.u32', 1)
        self.assertFalse(result['success'])
        self.assertFalse(result['state_preserved'])
        self.probe.halt.assert_not_called()
        self.probe.memory_write.assert_called_once()

    def test_request_and_schema_keep_typed_scope_strict(self):
        request = dict(session_id='test', item=dict(kind='variable', name='packet.payload.bytes', value=[1,2]))
        self.assertIsInstance(m.WriteRequest.model_validate(request).item, m.VariableWrite)
        for extra in (dict(require_match=False), dict(address=0x20000044), dict(width=32)):
            with self.assertRaises(ValidationError):
                m.WriteRequest.model_validate(dict(session_id='test', item=dict(request['item'], **extra)))
        for bad in ([1] * 4097, {'u32': 1}, [[1]], None):
            with self.assertRaises(ValidationError):
                m.WriteRequest(session_id='test', item=dict(kind='variable', name='packet', value=bad))
        schema = TypeAdapter(m.WriteRequest).json_schema()
        self.assertIn('VariableWrite', schema['$defs'])
        json.dumps(schema)

    def test_codec_scalar_width_range_and_unknown_kind(self):
        for size in (1, 2, 4, 8):
            for kind, value in [('signed', -(1 << (size*8-1))), ('unsigned', (1 << (size*8))-1)]:
                schema = dict(kind=kind, size=size)
                self.assertEqual(decode(schema, encode(schema, value)), value)
        for schema, value in [(dict(kind='mystery', size=4), 1), (dict(kind='unsigned', size=65537), 1),
                               (dict(kind='float', size=2), 1), (dict(kind='float', size=4), 1 << 10000)]:
            with self.assertRaises(ValueError):
                encode(schema, value)


if __name__ == '__main__':
    unittest.main()
