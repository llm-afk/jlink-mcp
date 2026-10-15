"""Real two-CU DWARF fixtures for LTO globals; no compiler or target needed."""
import io
import struct
import unittest

from elftools.elf.elffile import ELFFile

from jlink_mcp.dwarf_variables import DwarfVariables, attribute_owner, decode


def inherited_elf(duplicate_address=None):
    # All attributes use direct strings, ref4/ref_addr, and bounded expressions.
    definitions = [
        (1, 0x11, 1, ()),
        (2, 0x24, 0, ((3, 8), (0x0b, 0x0b), (0x3e, 0x0b))),
        (3, 0x13, 1, ((3, 8), (0x0b, 0x0b))),
        (4, 0x0d, 0, ((3, 8), (0x49, 0x13), (0x38, 0x0b))),
        (5, 0x01, 1, ((0x49, 0x13),)),
        (6, 0x21, 0, ((0x2f, 0x0b),)),
        (7, 0x34, 0, ((3, 8), (0x49, 0x13), (2, 0x18))),
        (8, 0x34, 0, ((3, 8), (0x49, 0x13), (0x3c, 0x19))),
        (9, 0x34, 0, ((3, 8), (0x49, 0x13))),
        (10, 0x34, 0, ((0x31, 0x10), (2, 0x18))),
        (11, 0x34, 0, ((0x47, 0x10), (2, 0x18))),
        (12, 0x34, 0, ((0x47, 0x10),)),
        (13, 0x34, 0, ((0x31, 0x10),)),
        (14, 0x34, 0, ((0x31, 0x10), (3, 8), (0x49, 0x13), (2, 0x18))),
        (15, 0x34, 0, ((0x31, 0x10), (0x47, 0x10), (2, 0x18))),
        (16, 0x2e, 1, ((3, 8),)),
        (17, 0x39, 1, ((3, 8),)),
        (18, 0x34, 0, ((3, 8), (0x49, 0x13), (2, 0x17))),
        (19, 0x34, 0, ((3, 8), (0x49, 0x13), (0x3c, 0x19), (2, 0x18))),
        (20, 0x34, 0, ((0x31, 0x10), (3, 8), (2, 0x18))),
        (21, 0x34, 0, ((3, 8), (2, 0x18))),
    ]
    abbrev = bytearray()
    for number, tag, children, attrs in definitions:
        abbrev.extend((number, tag, children))
        for name, form in attrs:
            abbrev.extend((name, form))
        abbrev.extend((0, 0))
    abbrev.append(0)

    def ref(offset):
        return struct.pack('<I', offset)

    def location(address):
        return b'\x05\x03' + ref(address)

    def unit(body):
        return struct.pack('<IHIB', len(body) + 7, 4, 0, 4) + body

    first = bytearray(b'\x01')

    def first_add(data):
        offset = len(first) + 11
        first.extend(data)
        return offset

    u32 = first_add(b'\x02uint32_t\0\x04\x07')
    flt = first_add(b'\x02float\0\x04\x04')
    array = first_add(b'\x05' + ref(u32) + b'\x06\x01\0')
    state = first_add(b'\x03State\0\x0c')
    first_add(b'\x04value\0' + ref(flt) + b'\0')
    first_add(b'\x04items\0' + ref(array) + b'\x04\0')
    abstract = first_add(b'\x09state\0' + ref(state))
    declaration = first_add(b'\x08counter\0' + ref(u32))
    bridge = first_add(b'\x0c' + ref(declaration))
    direct = first_add(b'\x07direct\0' + ref(u32) + location(0x20000080))
    tail = abstract
    for _ in range(33):
        tail = first_add(b'\x0d' + ref(tail))
    first.append(0)
    first_unit = unit(first)
    second_base = len(first_unit)
    second = bytearray(b'\x01')

    def second_add(data):
        offset = len(second) + 11
        second.extend(data)
        return offset

    # Same relative offset as CU1's uint32_t, but a different width. Resolving
    # the inherited type in the concrete CU must not silently return uint8_t.
    u8 = second_add(b'\x02uint8_t\0\x01\x07')
    second_add(b'\x0a' + ref(abstract) + location(0x20000000))
    second_add(b'\x0b' + ref(declaration) + location(0x20000010))
    second_add(b'\x0a' + ref(bridge) + location(0x20000014))
    # Both definitions refer to the same declaration: ambiguity is intentional.
    if duplicate_address is not None:
        second_add(b'\x0a' + ref(abstract) + location(duplicate_address))
    second_add(b'\x0e' + ref(abstract) + b'override\0' + ref(u8) + location(0x20000018))
    second_add(b'\x07zero_object\0' + ref(u8) + location(0))
    second_add(b'\x07dynamic\0' + ref(u8) + b'\x01\x50')  # DW_OP_reg0
    second_add(b'\x12loclist\0' + ref(u8) + ref(0))
    second_add(b'\x13declaration_only\0' + ref(u8) + location(0x20000020))
    second_add(b'\x0d' + ref(direct))  # A reference never inherits its location.
    second_add(b'\x0a' + ref(tail) + location(0x20000028))
    cycle = second_base + len(second) + 11
    second_add(b'\x0a' + ref(cycle) + location(0x20000030))
    type_cycle = second_base + len(second) + 11
    second_add(b'\x14' + ref(type_cycle) + b'type_cycle\0' + location(0x20000034))
    second_add(b'\x0f' + ref(abstract) + ref(declaration) + location(0x20000038))
    second_add(b'\x0a' + ref(0xfffffff0) + location(0x2000003c))
    second_add(b'\x15incomplete\0' + location(0x20000040))
    second_add(b'\x10function\0')
    second_add(b'\x07local\0' + ref(u8) + location(0x20000044))
    second.append(0)
    second_add(b'\x11namespace\0')
    second_add(b'\x07namespaced\0' + ref(u8) + location(0x20000048))
    second.extend((0, 0))
    debug = first_unit + unit(second)
    names = b'\0.shstrtab\0.bss\0.debug_abbrev\0.debug_info\0'
    image = bytearray(b'\0' * 52)
    sections = [b'\0' * 40]
    for name, content, kind, flags, address in [
        ('.shstrtab', names, 3, 0, 0), ('.bss', b'\0' * 256, 8, 3, 0x20000000),
        ('.debug_abbrev', abbrev, 1, 0, 0), ('.debug_info', debug, 1, 0, 0),
    ]:
        offset = len(image)
        image.extend(content)
        sections.append(struct.pack('<10I', names.index(name.encode()), kind, flags,
                                    address, offset, len(content), 0, 0, 1, 0))
    shoff = len(image)
    image.extend(b''.join(sections))
    image[:52] = struct.pack('<16sHHIIIIIHHHHHH', b'\x7fELF\x01\x01\x01' + b'\0' * 9,
                            2, 40, 1, 0, 0, shoff, 0, 52, 0, 0, 40, len(sections), 1)
    return image


class DwarfInheritanceTests(unittest.TestCase):
    def parser(self, duplicate_address=None):
        return DwarfVariables(ELFFile(io.BytesIO(inherited_elf(duplicate_address))))

    def test_abstract_origin_cross_cu_struct_array_and_decode(self):
        parser = self.parser()
        state = parser.resolve('state')
        self.assertEqual((state['address'], state['size']), (0x20000000, 12))
        self.assertEqual(parser.resolve('state.items[1]')['address'], 0x20000008)
        self.assertEqual(decode(state['type'], struct.pack('<fII', 1.5, 10, 20)),
                         {'value': 1.5, 'items': [10, 20]})

    def test_specification_chain_keeps_ambiguity_and_reference_owner(self):
        parser = self.parser()
        # Direct specification and origin->specification both resolve the CU1
        # uint32_t, not the uint8_t at the same relative offset in concrete CU2.
        self.assertEqual(len(parser.variables['counter']), 2)
        for _, variable in parser.variables['counter']:
            owner = attribute_owner(variable, 'DW_AT_type')
            self.assertEqual(owner.cu.cu_offset, 0)
            typ = owner.get_DIE_from_attribute('DW_AT_type')
            self.assertEqual(typ.attributes['DW_AT_byte_size'].value, 4)
        with self.assertRaisesRegex(ValueError, 'unique'):
            parser.resolve('counter')

    def test_declaration_flag_is_not_inherited(self):
        parser = self.parser()
        parser.variables['counter'] = parser.variables['counter'][:1]
        self.assertEqual(parser.resolve('counter')['size'], 4)
        self.assertNotIn('declaration_only', parser.variables)

    def test_direct_name_and_type_override_origin(self):
        item = self.parser().resolve('override')
        self.assertEqual((item['address'], item['size']), (0x20000018, 1))

    def test_duplicate_locations_remain_ambiguous_even_at_same_address(self):
        for address in (0x20000000, 0x20000050):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, 'unique'):
                self.parser(address).resolve('state')

    def test_missing_dynamic_and_local_locations_are_rejected(self):
        parser = self.parser()
        for name in ('dynamic', 'loclist', 'local', 'declaration_only'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                parser.resolve(name)
        self.assertEqual(len(parser.variables['direct']), 1)
        self.assertEqual(parser.resolve('namespaced')['address'], 0x20000048)

    def test_address_zero_keeps_original_location_policy(self):
        # Zero can be a real Flash address. The inheritance fix must not turn
        # one target's discarded-section tombstone into a universal rule.
        self.assertEqual(self.parser().resolve('zero_object')['address'], 0)

    def test_bad_inheritance_does_not_hide_good_globals(self):
        parser = self.parser()
        # Cycle, >32 chain, conflicting dual links and dangling ref all have
        # no name of their own, and must not add a state/counter candidate.
        self.assertEqual(len(parser.variables['state']), 1)
        self.assertEqual(len(parser.variables['counter']), 2)
        self.assertEqual(parser.resolve('direct')['address'], 0x20000080)
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            parser.resolve('type_cycle')
        with self.assertRaisesRegex(ValueError, 'complete DWARF type'):
            parser.resolve('incomplete')

    def test_inherited_plan_keeps_expression_and_bounds_guards(self):
        parser = self.parser()
        for expression in ('state.items[2]', 'state.missing', 'state()', '*state', 'state->value'):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                parser.resolve(expression)
        first = parser.resolve('state.items[0]')
        first['address'] = 0
        self.assertEqual(parser.resolve('state.items[0]')['address'], 0x20000004)


if __name__ == '__main__':
    unittest.main()
