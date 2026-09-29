"""Bounded DWARF decoding of statically addressed C globals; no expression VM."""
import math
import re
import struct

from elftools.dwarf.descriptions import describe_form_class
from elftools.dwarf.dwarf_expr import DWARFExprParser


def attr(die, name, default=None):
    value = die.attributes.get(name)
    return value.value if value is not None else default


def name_of(die):
    value = attr(die, "DW_AT_name", b"")
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def constant(die, name, default=None):
    value = die.attributes.get(name)
    if value is None:
        return default
    if describe_form_class(value.form) != "constant":
        raise ValueError(f"Dynamic {name} is not supported")
    return value.value


def underlying(die):
    visited = set()
    while die.tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type", "DW_TAG_restrict_type", "DW_TAG_atomic_type"):
        if die.offset in visited or len(visited) >= 32:
            raise ValueError("Cyclic or excessively nested type")
        visited.add(die.offset)
        die = die.get_DIE_from_attribute("DW_AT_type")
    return die


def byte_size(die, depth=0):
    if depth > 16:
        raise ValueError("Type is too complex")
    die = underlying(die)
    size = constant(die, "DW_AT_byte_size")
    if size is None and die.tag == "DW_TAG_pointer_type":
        size = die.cu["address_size"]
    if size is None and die.tag == "DW_TAG_array_type":
        size = byte_size(die.get_DIE_from_attribute("DW_AT_type"), depth + 1)
        for count in array_shape(die):
            size *= count
    if not isinstance(size, int) or not 0 < size <= 65536:
        raise ValueError("Incomplete type or object exceeds 64KB")
    return size


def array_shape(die):
    if attr(die, "DW_AT_ordering", 0) != 0 or any(
            key in die.attributes for key in ("DW_AT_byte_stride", "DW_AT_bit_stride")):
        raise ValueError("Only contiguous row-major arrays are supported")
    counts = []
    for bound in die.iter_children():
        if bound.tag != "DW_TAG_subrange_type":
            continue
        if any(key in bound.attributes for key in ("DW_AT_byte_stride", "DW_AT_bit_stride")):
            raise ValueError("Strided arrays are not supported")
        lower = constant(bound, "DW_AT_lower_bound", 0)
        count = constant(bound, "DW_AT_count")
        if count is None:
            upper = constant(bound, "DW_AT_upper_bound")
            count = upper - lower + 1 if upper is not None else None
        if lower != 0 or not isinstance(count, int) or not 0 < count <= 4096:
            raise ValueError("Only fixed zero-based arrays up to 4096 elements are supported")
        counts.append(count)
    if not counts or len(counts) > 8 or math.prod(counts) > 4096:
        raise ValueError("Missing or excessive array dimensions")
    return counts


def member_offset(member):
    if "DW_AT_bit_size" in member.attributes or "DW_AT_data_bit_offset" in member.attributes:
        raise ValueError("Bitfields are not supported")
    value = member.attributes.get("DW_AT_data_member_location")
    if value is None:
        raise ValueError("Member has no fixed offset")
    if describe_form_class(value.form) == "constant":
        offset = value.value
    else:
        operations = DWARFExprParser(member.cu.structs).parse_expr(value.value)
        if len(operations) != 1 or operations[0].op_name != "DW_OP_plus_uconst":
            raise ValueError("Only constant member offsets are supported")
        offset = operations[0].args[0]
    if not 0 <= offset <= 65536:
        raise ValueError("Invalid member offset")
    return offset


def describe(die, depth=0, budget=None):
    budget = budget if budget is not None else [1024]
    budget[0] -= 1
    if depth > 16 or budget[0] < 0:
        raise ValueError("Type is too complex")
    label = name_of(die)
    die = underlying(die)
    result = {"name": label or name_of(die), "size": byte_size(die)}
    if die.tag == "DW_TAG_base_type":
        if attr(die, "DW_AT_endianity", 0) not in (0, 2):
            raise ValueError("Only little-endian base types are supported")
        encoding = attr(die, "DW_AT_encoding")
        kinds = {1: "address", 2: "bool", 4: "float", 5: "signed", 6: "signed", 7: "unsigned", 8: "unsigned"}
        if encoding not in kinds or result["size"] not in (1, 2, 4, 8):
            raise ValueError("Unsupported base type encoding or width")
        result["kind"] = kinds[encoding]
        if result["kind"] == "float" and result["size"] not in (4, 8):
            raise ValueError("Only IEEE binary32/binary64 floats are supported")
    elif die.tag == "DW_TAG_pointer_type":
        result["kind"] = "pointer"  # Address value only; never dereference.
    elif die.tag == "DW_TAG_enumeration_type":
        result["kind"] = "enum"
        result["values"] = {name_of(child): attr(child, "DW_AT_const_value")
                            for child in die.iter_children() if child.tag == "DW_TAG_enumerator"}
        result["signed"] = attr(die, "DW_AT_encoding") in (5, 6) or any(v < 0 for v in result["values"].values())
        if "DW_AT_type" in die.attributes:
            result["signed"] = attr(underlying(die.get_DIE_from_attribute("DW_AT_type")), "DW_AT_encoding") in (5, 6)
    elif die.tag == "DW_TAG_structure_type":
        result.update(kind="struct", fields=[])
        for child in die.iter_children():
            if child.tag != "DW_TAG_member":
                continue
            name, offset = name_of(child), member_offset(child)
            if not name or any(f["name"] == name for f in result["fields"]):
                raise ValueError("Anonymous or duplicate structure members are unsupported")
            child_type = describe(child.get_DIE_from_attribute("DW_AT_type"), depth + 1, budget)
            if offset + child_type["size"] > result["size"]:
                raise ValueError("Member lies outside its structure")
            result["fields"].append({"name": name, "offset": offset, "type": child_type})
    elif die.tag == "DW_TAG_array_type":
        result.update(kind="array", shape=array_shape(die),
                      element=describe(die.get_DIE_from_attribute("DW_AT_type"), depth + 1, budget))
        if result["size"] != math.prod(result["shape"]) * result["element"]["size"]:
            raise ValueError("Padded or inconsistent arrays are unsupported")
    else:
        raise ValueError(f"Unsupported type {die.tag}")
    return result


def decode(schema, data, budget=None):
    budget = budget if budget is not None else [8192]
    budget[0] -= 1
    if budget[0] < 0 or len(data) != schema["size"]:
        raise ValueError("Decoded value exceeds limits or byte length is incorrect")
    kind = schema["kind"]
    if kind == "struct":
        return {f["name"]: decode(f["type"], data[f["offset"]:f["offset"] + f["type"]["size"]], budget)
                for f in schema["fields"]}
    if kind == "array":
        def array(shape, raw):
            if len(shape) == 1:
                width = schema["element"]["size"]
                return [decode(schema["element"], raw[i * width:(i + 1) * width], budget) for i in range(shape[0])]
            stride = len(raw) // shape[0]
            return [array(shape[1:], raw[i * stride:(i + 1) * stride]) for i in range(shape[0])]
        return array(schema["shape"], data)
    if kind == "float":
        value = struct.unpack("<f" if len(data) == 4 else "<d", data)[0]
        return value if math.isfinite(value) else "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    value = int.from_bytes(data, "little", signed=kind == "signed" or (kind == "enum" and schema["signed"]))
    if kind == "enum":
        return {"value": value, "names": [n for n, v in schema["values"].items() if v == value]}
    if kind in ("pointer", "address"):
        return {"address": value, "hex": f"0x{value:08x}", "dereferenced": False}
    return bool(value) if kind == "bool" else value


class DwarfVariables:
    def __init__(self, elf):
        self.variables = {}
        if not elf.has_dwarf_info():
            return
        for cu in elf.get_dwarf_info().iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_variable" or attr(die, "DW_AT_declaration", False):
                    continue
                parent = die.get_parent()
                if parent is None or parent.tag not in ("DW_TAG_compile_unit", "DW_TAG_namespace"):
                    continue
                location = die.attributes.get("DW_AT_location")
                if location is None or describe_form_class(location.form) not in ("exprloc", "block"):
                    continue
                try:
                    operations = DWARFExprParser(cu.structs).parse_expr(location.value)
                except Exception:
                    # An unsupported vendor/TLS location must not hide other globals.
                    continue
                if len(operations) == 1 and operations[0].op_name == "DW_OP_addr" and name_of(die):
                    self.variables.setdefault(name_of(die), []).append((operations[0].args[0], die))

    def resolve(self, expression):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*|\[[0-9]+\])*", expression):
            raise ValueError("Use global.member[index] only; calls, pointers and expressions are forbidden")
        root = re.match(r"[A-Za-z_][A-Za-z_0-9]*", expression).group()
        candidates = self.variables.get(root, [])
        if len(candidates) != 1:
            raise ValueError(f"Global {root!r} has no unique static DWARF location (missing, optimized out, or ambiguous)")
        address, variable = candidates[0]
        die = variable.get_DIE_from_attribute("DW_AT_type")
        base, total = address, byte_size(die)
        tokens = re.findall(r"\.([A-Za-z_][A-Za-z_0-9]*)|\[([0-9]+)\]", expression[len(root):])
        position = 0
        while position < len(tokens):
            member, index = tokens[position]
            die = underlying(die)
            if member:
                if die.tag != "DW_TAG_structure_type":
                    raise ValueError("Member access requires a structure")
                members = [c for c in die.iter_children() if c.tag == "DW_TAG_member" and name_of(c) == member]
                if len(members) != 1:
                    raise ValueError(f"Unknown or ambiguous member {member}")
                offset = member_offset(members[0])
                child = members[0].get_DIE_from_attribute("DW_AT_type")
                if offset + byte_size(child) > byte_size(die):
                    raise ValueError("Member exceeds structure bounds")
                address += offset
                die = child
                position += 1
            else:
                if die.tag != "DW_TAG_array_type":
                    raise ValueError("Indexing requires a fixed array")
                shape = array_shape(die)
                element = die.get_DIE_from_attribute("DW_AT_type")
                for axis, count in enumerate(shape):
                    if position >= len(tokens) or tokens[position][0]:
                        raise ValueError("Provide all dimensions when indexing a multidimensional array")
                    index = int(tokens[position][1])
                    if index >= count:
                        raise ValueError("Array index out of bounds")
                    address += index * math.prod(shape[axis + 1:]) * byte_size(element)
                    position += 1
                die = element
        schema = describe(die)
        if address < base or address + schema["size"] > base + total:
            raise ValueError("Selected variable lies outside its global object")
        return {"expression": expression, "address": address, "size": schema["size"], "type": schema}
