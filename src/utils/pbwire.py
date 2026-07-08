"""Minimal protobuf wire-format reader (spec-less: no .proto schema exists for
poe.ninja's internal builds-search API, so this reads raw tag/wiretype pairs
rather than depending on generated message classes).

Only decodes what src/fetchers/ninja_builds.py actually needs: varints,
length-delimited bytes/strings, and repeated fields grouped by field number.
Unknown wire types raise ValueError so callers can treat a shape change as a
validation failure (spec 10.1b) instead of silently misreading data.
"""
from __future__ import annotations

from typing import Iterator


class ProtoParseError(ValueError):
    pass


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoParseError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """Yield (field_number, wire_type, value) for each top-level field in buf.

    value is: int for wire_type 0/5/1 (varint/fixed32/fixed64, returned as
    raw int/bytes-as-int not applicable — fixed32/64 returned as bytes),
    bytes for wire_type 2 (length-delimited).
    """
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            val, pos = read_varint(buf, pos)
            yield field_num, wire_type, val
        elif wire_type == 2:
            length, pos = read_varint(buf, pos)
            if pos + length > n:
                raise ProtoParseError(f"length-delimited field {field_num} overruns buffer")
            yield field_num, wire_type, buf[pos:pos + length]
            pos += length
        elif wire_type == 1:
            if pos + 8 > n:
                raise ProtoParseError(f"fixed64 field {field_num} overruns buffer")
            yield field_num, wire_type, buf[pos:pos + 8]
            pos += 8
        elif wire_type == 5:
            if pos + 4 > n:
                raise ProtoParseError(f"fixed32 field {field_num} overruns buffer")
            yield field_num, wire_type, buf[pos:pos + 4]
            pos += 4
        else:
            raise ProtoParseError(f"unsupported wire_type {wire_type} for field {field_num} at {pos}")


def first(buf: bytes, field_num: int) -> object:
    for fn, _wt, val in iter_fields(buf):
        if fn == field_num:
            return val
    return None


def all_of(buf: bytes, field_num: int) -> list:
    return [val for fn, _wt, val in iter_fields(buf) if fn == field_num]


def as_str(val: object) -> str:
    if not isinstance(val, (bytes, bytearray)):
        raise ProtoParseError(f"expected bytes for string field, got {type(val)!r}")
    return val.decode("utf-8")


def packed_varints(buf: bytes) -> list[int]:
    """Decode a packed-repeated varint field's raw bytes into a list of ints."""
    values = []
    pos = 0
    while pos < len(buf):
        v, pos = read_varint(buf, pos)
        values.append(v)
    return values
