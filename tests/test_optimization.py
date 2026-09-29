"""Portable ELF/DWARF and profile regression fixtures; no compiler or probe."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError
from jlink_mcp import api_models as m
from jlink_mcp.dwarf_variables import decode
from jlink_mcp.image_match import compare_image
from jlink_mcp.profiles import ProjectProfile, load_profile
from jlink_mcp.service import DebugService
from jlink_mcp.symbols import SymbolFile


def dwarf_elf():
    # DWARF v4, direct strings, ref4, constant member offsets, exprloc globals.
    abbrev = bytes([
        1, 0x11, 1, 0, 0,
        2, 0x24, 0, 3, 8, 0x0b, 0x0b, 0x3e, 0x0b, 0, 0,
        3, 0x13, 1, 3, 8, 0x0b, 0x0b, 0, 0,
        4, 0x0d, 0, 3, 8, 0x49, 0x13, 0x38, 0x0b, 0, 0,
        5, 0x01, 1, 0x49, 0x13, 0, 0,
        6, 0x21, 0, 0x2f, 0x0b, 0, 0,
        7, 0x34, 0, 3, 8, 0x49, 0x13, 2, 0x18, 0, 0,
        8, 0x0f, 0, 0x0b, 0x0b, 0, 0, 0])
    body = bytearray(b'\x01')
    def add(data):
        offset = len(body) + 11
        body.extend(data)
        return offset
    u32 = add(b'\x02uint32_t\0\x04\x07')
    flt = add(b'\x02float\0\x04\x04')
    pointer = add(b'\x08\x04')
    array = add(b'\x05' + struct.pack('<I', u32) + b'\x06\x03\0')
    obj = add(b'\x03State\0\x18')
    for name, typ, off in [('value', flt, 0), ('items', array, 4), ('ptr', pointer, 20)]:
        add(b'\x04' + name.encode() + b'\0' + struct.pack('<I', typ) + bytes([off]))
    add(b'\0')
    for name, typ, address in [('state', obj, 0x20000000), ('counter', u32, 0x20000018)]:
        add(b'\x07' + name.encode() + b'\0' + struct.pack('<I', typ) + b'\x05\x03' + struct.pack('<I', address))
    add(b'\0')
    debug = struct.pack('<IHI B', len(body) + 7, 4, 0, 4) + body
    names = b'\0.shstrtab\0.text\0.data\0.bss\0.debug_abbrev\0.debug_info\0'
    data = bytearray(b'\0' * 84)
    headers = [b'\0' * 40]
    for name, content, kind, flags, address in [
        ('.shstrtab', names, 3, 0, 0), ('.text', b'CODE', 1, 6, 0x1000),
        ('.data', b'DATA', 1, 3, 0x20000000), ('.bss', b'\0' * 4, 8, 3, 0x20000004),
        ('.debug_abbrev', abbrev, 1, 0, 0), ('.debug_info', debug, 1, 0, 0)]:
        offset = len(data)
        data.extend(content)
        headers.append(struct.pack('<10I', names.index(name.encode()), kind, flags, address, offset, len(content), 0, 0, 1, 0))
        if name == '.text':
            text_offset = offset
    shoff = len(data)
    data.extend(b''.join(headers))
    data[:52] = struct.pack('<16sHHIIIIIHHHHHH', b'\x7fELF\x01\x01\x01' + b'\0' * 9,
                            2, 40, 1, 0, 52, shoff, 0, 52, 32, 1, 40, len(headers), 1)
    data[52:84] = struct.pack('<8I', 1, text_offset, 0x1000, 0x08000000, 8, 8, 5, 1)
    return data


def profile(**kwargs):
    return ProjectProfile(name='test', chip='GD32C103CB', regions=[
        dict(name='code', address=0x08000000, size=4, kind='flash'),
        dict(name='params', address=0x08000004, size=4, kind='flash', mutable=True),
        dict(name='ram', address=0x20000000, size=1024, kind='ram', mutable=True)], **kwargs)


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.elf'
        self.path.write_bytes(dwarf_elf())
        self.elf = SymbolFile(str(self.path))

    def test_real_dwarf_struct_array_float_pointer(self):
        info = self.elf.variable('state')
        raw = struct.pack('<f5I', 1.25, 10, 20, 30, 40, 0x20000100)
        value = decode(info['type'], raw)
        self.assertEqual(value['value'], 1.25)
        self.assertEqual(value['items'], [10, 20, 30, 40])
        self.assertEqual(value['ptr']['address'], 0x20000100)
        self.assertFalse(value['ptr']['dereferenced'])
        self.assertEqual(self.elf.variable('state.items[2]')['address'], 0x2000000c)

    def test_reject_expressions_and_bounds(self):
        for expression in ('state.items[4]', 'state.missing', 'state.ptr[0]', 'state->value', 'state()', '*state', 'state.items[-1]', 'local'):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.elf.variable(expression)

    def test_ambiguous_globals_rejected(self):
        self.elf.variable('counter')
        self.elf._variables.variables['counter'] *= 2
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.elf.variable('counter')

    def test_lma_mapping_ignores_writable_and_bss(self):
        sections, skipped = self.elf.readonly_sections()
        self.assertEqual(sections, [{'name': '.text', 'address': 0x08000000, 'data': b'CODE'}])
        self.assertEqual(skipped, [])
        self.path.write_bytes(b'changed after loading')
        self.assertEqual(self.elf.readonly_sections()[0], sections)

    def test_match_and_mismatch(self):
        read = Mock(return_value=b'CODE')
        result = compare_image(self.elf, profile(), read)
        self.assertEqual(result['status'], 'matched')
        read.assert_called_once_with(0x08000000, 4)
        result = compare_image(self.elf, profile(), lambda *_: b'CoDE')
        self.assertEqual(result['status'], 'mismatch')
        self.assertEqual(result['mismatched_bytes'], 1)
        self.assertEqual(result['ranges'][0]['first_mismatches'][0]['address'], 0x08000001)

    def test_mutable_exclusion_partial_and_unknown(self):
        elf = Mock(sha256='test')
        elf.readonly_sections.return_value = ([{'name': 'ro', 'address': 0x08000000, 'data': b'CODEPARM'}], [])
        read = Mock(return_value=b'CODE')
        self.assertEqual(compare_image(elf, profile(), read)['status'], 'partial')
        read.assert_called_once_with(0x08000000, 4)
        elf.readonly_sections.return_value = ([], [])
        self.assertEqual(compare_image(elf, profile(), read)['status'], 'unknown')

    def test_failed_or_short_read_never_match(self):
        for read in (Mock(return_value=b'C'), Mock(side_effect=OSError('lost'))):
            result = compare_image(self.elf, profile(), read)
            self.assertEqual(result['status'], 'unknown')
            self.assertFalse(result['success'])

    def test_profile_relative_paths_and_validation(self):
        path = Path(self.temp.name) / 'profile.json'
        config = profile(elf_path='test.elf').model_dump()
        path.write_text(json.dumps(config))
        loaded, identity = load_profile(path)
        self.assertEqual(loaded.elf_path, str(self.path))
        self.assertEqual(len(identity['sha256']), 64)
        for mutate in (lambda x: x.update(typo=1), lambda x: x['regions'].append(x['regions'][0]),
                       lambda x: x['regions'][0].update(address=0xffffffff)):
            cfg = json.loads(json.dumps(config))
            mutate(cfg)
            with self.assertRaises(ValidationError):
                ProjectProfile.model_validate(cfg)

    def service(self):
        s = DebugService()
        s.profile, s.symbols = profile(), self.elf
        s.probe = Mock()
        s.probe.halted.return_value = False
        return s

    def test_typed_reads_guard_and_explicit_override(self):
        s = self.service()
        s.probe.memory_read.return_value = list(struct.pack('<I', 42))
        def read(**kwargs):
            return s.read(m.ReadRequest(session_id='test', items=[dict(kind='variable', name='counter', **kwargs)]))
        blocked = read()
        self.assertEqual(blocked['items'][0]['error']['code'], 'IMAGE_NOT_MATCHED')
        s.probe.memory_read.assert_not_called()
        allowed = read(require_match=False)
        self.assertTrue(allowed['success'])
        self.assertEqual(allowed['items'][0]['data']['value'], 42)
        self.assertEqual(allowed['items'][0]['data']['elf_match'], 'unknown')
        s.image_match = {'status': 'matched'}
        self.assertTrue(read()['success'])
        s.probe.halt.assert_not_called()

    def test_verification_failure_invalidates_previous_match(self):
        s = self.service()
        s.image_match = {'status': 'matched'}
        s.probe.memory_read.side_effect = OSError('read failed')
        result = s.firmware(m.FirmwareVerifyImage(session_id='test', action='verify_image'))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(s.image_match['status'], 'unknown')
        s.probe.halt.assert_not_called()

    def test_variable_outside_profile_rejected(self):
        s = self.service()
        s.profile = ProjectProfile(name='empty', chip='test')
        with self.assertRaisesRegex(Exception, 'outside configured'):
            s._plan_read(m.VariableItem(kind='variable', name='counter', require_match=False))

    def test_decode_nonfinite_bool_enum_and_length(self):
        self.assertEqual(decode({'kind': 'float', 'size': 4}, struct.pack('<f', float('nan'))), 'NaN')
        self.assertTrue(decode({'kind': 'bool', 'size': 1}, b'\1'))
        self.assertEqual(decode({'kind': 'enum', 'size': 1, 'signed': True, 'values': {'BAD': -1}}, b'\xff'), {'value': -1, 'names': ['BAD']})
        with self.assertRaises(ValueError):
            decode({'kind': 'unsigned', 'size': 4}, b'\0')

    def test_maximum_scalar_array(self):
        schema = {'kind': 'array', 'size': 4096, 'shape': [4096], 'element': {'kind': 'unsigned', 'size': 1}}
        self.assertEqual(len(decode(schema, b'\0' * 4096)), 4096)

    def test_preflight_observes_without_writing(self):
        s = self.service()
        s.profile = profile(debug_checks=[dict(name='freeze', address=0xe0042004, mask=256, expected=256, description='watchdog')])
        s.probe.memory_read.return_value = [1024]
        result = s.inspect(m.InspectPreflight(session_id='test', action='preflight'))
        self.assertFalse(result['checks'][0]['satisfied'])
        s.probe.memory_write.assert_not_called()
        s.probe.halt.assert_not_called()

    def test_profile_conflict_and_missing_serial_precede_connection(self):
        path = Path(self.temp.name) / 'profile.json'
        path.write_text(profile().model_dump_json())
        with patch('jlink_mcp.service.jlink_manager') as manager, patch('jlink_mcp.service.gdb_server_manager') as gdb, patch('jlink_mcp.service.connection.connect_device') as connect:
            manager.is_connected = gdb.is_running = False
            s = DebugService()
            result = s.invoke('session', m.OpenSession(action='open', profile_path=str(path), chip='wrong', serial_number='1'))
            self.assertEqual(result['error']['code'], 'PROFILE_MISMATCH')
            result = s.invoke('session', m.OpenSession(action='open', profile_path=str(path)))
            self.assertFalse(result['success'])
            connect.assert_not_called()

    def test_profile_open_inherits_target(self):
        path = Path(self.temp.name) / 'profile.json'
        path.write_text(profile(architecture='cortex-m', elf_path='test.elf').model_dump_json())
        with patch('jlink_mcp.service.jlink_manager') as manager, patch('jlink_mcp.service.gdb_server_manager') as gdb, patch('jlink_mcp.service.connection') as conn:
            manager.is_connected = gdb.is_running = False
            conn.connect_device.return_value = {'success': True}
            conn.get_connection_status.return_value = {'success': True, 'data': {'target_connected': True, 'device_serial': 123}}
            s = DebugService()
            result = s.invoke('session', m.OpenSession(action='open', profile_path=str(path), serial_number='123'))
            self.assertTrue(result['success'], result)
            self.assertEqual(s.target['chip'], 'GD32C103CB')
            self.assertEqual(s.symbols.path, str(self.path))
            conn.connect_device.assert_called_once_with('123', 'SWD', 'GD32C103CB', None)

    def test_reset_and_write_invalidate_match(self):
        s = self.service()
        s.image_match = {'status': 'matched'}
        s._invalidate()
        self.assertEqual(s.image_match['status'], 'unknown')
        s.image_match = {'status': 'matched'}
        with self.assertRaises(Exception):
            s.write(m.WriteRequest(session_id='test', item=dict(kind='memory', address=0x20000000, data_hex='01')))
        self.assertEqual(s.image_match['status'], 'unknown')

    def test_malformed_types_are_bounded(self):
        from types import SimpleNamespace
        from jlink_mcp.dwarf_variables import byte_size, describe, array_shape, member_offset
        def die(tag, **attrs):
            obj = Mock(tag=tag, offset=1, attributes={k: SimpleNamespace(value=v, form='DW_FORM_data1') for k, v in attrs.items()})
            obj.iter_children.return_value = []
            return obj
        cyclic = die('DW_TAG_array_type')
        cyclic.get_DIE_from_attribute.return_value = cyclic
        with self.assertRaisesRegex(ValueError, 'complex'):
            byte_size(cyclic)
        for typ in (die('DW_TAG_union_type', DW_AT_byte_size=4),
                    die('DW_TAG_base_type', DW_AT_byte_size=4, DW_AT_encoding=4, DW_AT_endianity=1)):
            with self.assertRaises(ValueError):
                describe(typ)
        with self.assertRaises(ValueError):
            member_offset(die('DW_TAG_member', DW_AT_bit_size=1, DW_AT_data_member_location=0))
        with self.assertRaises(ValueError):
            array_shape(die('DW_TAG_array_type', DW_AT_ordering=1))
