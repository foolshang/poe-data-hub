"""Decoder for poe.ninja's "NDIC" dictionary format
(`/{game}/api/builds/dictionary/{hash}`) -- reverse-engineered live
2026-09-16 after poe.ninja replaced the old protobuf-based dictionary format
with this one (see `src/fetchers/ninja_builds.py`'s module docstring for the
full incident history: the format change silently broke the meta job for
~43 days before anyone re-fetched the live endpoint to check).

Not protobuf -- a custom binary format, magic bytes b"NDIC":

  bytes 0-3:   magic b"NDIC"
  bytes 4-7:   format version (uint32 LE) -- only version 2 seen/supported
  bytes 8-11:  reserved (0 observed)
  bytes 12-15: entry count N (uint32 LE)
  ...:         a header/bucket-index region of unknown internal structure
               (looks like a hash-bucket index for the website's own
               reverse lookups -- "given a name, find its index fast").
               Not needed here: this project only ever does the *forward*
               lookup (given an index from a search response, find the
               name), which needs nothing but the length-array + text blob
               below.
  last N bytes before the text blob: one length byte per entry (values
               seen are all < 128 -- a plain byte, not a protobuf varint).
  remainder:   all N strings concatenated in order, UTF-8, sized by the
               lengths above.

The bucket-index region's size isn't documented anywhere, so `decode`
locates the length-array/text-blob boundary empirically: the only valid
split point is the one where the last N bytes, read as lengths, sum to
exactly the number of bytes remaining in the buffer. Verified against real
poe.ninja "class" (30 entries) and "gem" (1280 entries PoE2, several
thousand PoE1) dictionaries for both games -- both decode with zero
leftover/short bytes.
"""
from __future__ import annotations

MAGIC = b"NDIC"
SUPPORTED_VERSION = 2


class NdicParseError(ValueError):
    pass


def decode(raw: bytes) -> list[str]:
    """Decode an NDIC dictionary's entries in index order. Raises
    `NdicParseError` immediately on a magic-byte or version mismatch (never
    silently -- a future poe.ninja format change must surface as a loud
    failure, not a mysteriously empty/wrong dictionary) with the actual
    bytes/version found, and if the length-array/text-blob split can't be
    located at all."""
    if raw[:4] != MAGIC:
        raise NdicParseError(f"expected magic {MAGIC!r}, got {raw[:4]!r}")
    if len(raw) < 16:
        raise NdicParseError(f"truncated header ({len(raw)} bytes, need at least 16)")
    version = int.from_bytes(raw[4:8], "little")
    if version != SUPPORTED_VERSION:
        raise NdicParseError(f"expected format version {SUPPORTED_VERSION}, got {version}")

    count = int.from_bytes(raw[12:16], "little")
    total = len(raw)
    if count == 0:
        return []

    for text_start in range(count, total + 1):
        lengths = raw[text_start - count:text_start]
        if sum(lengths) != total - text_start:
            continue
        pos = text_start
        names: list[str] = []
        try:
            for length in lengths:
                names.append(raw[pos:pos + length].decode("utf-8"))
                pos += length
        except UnicodeDecodeError:
            continue
        if pos == total:
            return names

    raise NdicParseError(f"could not locate {count} length-prefixed entries totaling to {total} bytes")
