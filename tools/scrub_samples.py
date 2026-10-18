"""Replace real players' account / character names in data/samples/ with deterministic pseudonyms.

data/samples/ is tracked (curated fixtures) in a public repo, and poe.ninja's responses carry the names of
real people who never agreed to that. Every name is replaced by an ASCII pseudonym of the SAME BYTE LENGTH, so
protobuf length prefixes stay valid and the fixtures still decode exactly as before. The same real string always
gets the same pseudonym, so account<->character relationships across files survive.

Pseudonym shape (tests/test_samples_no_player_names.py relies on it): "anon_<hex>" (>= 6 bytes) or
"_<hex>" (shorter).

Usage:
    python -m tools.scrub_samples            # scrub everything under data/samples in place
    python -m tools.scrub_samples --check    # exit 1 if any real-looking name is left
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from src.utils import pbwire as pb

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
PSEUDONYM_RE = re.compile(r"^(anon_[0-9a-f]+|_[0-9a-f]{0,4})$")
_NAME_COLUMNS = ("name", "account")


def is_pseudonym(s: str) -> bool:
    return bool(PSEUDONYM_RE.match(s))


def _search_strings(raw: bytes) -> set[str]:
    """name/account strings of a poe.ninja builds-search protobuf response ({} if it is not one)."""
    found: set[str] = set()
    try:
        body = pb.first(raw, 1)
        if not isinstance(body, bytes):
            return found
        # column entries live in field 12 (current schema) or field 5 (pre-2026-08-04 schema)
        for old_schema, entry in [(False, e) for e in pb.all_of(body, 12)] + [(True, e) for e in pb.all_of(body, 5)]:
            label = None
            strings: list[str] = []
            for fn, wt, val in pb.iter_fields(entry):
                if fn in (1, 2) and wt == 2 and label is None:
                    label = pb.as_str(val)
                elif fn == 7 and wt == 2:
                    strings.append(pb.as_str(val))
                elif old_schema and fn == 2 and wt == 2 and label in _NAME_COLUMNS:   # string wrapped in a message
                    inner = pb.first(val, 1)
                    if isinstance(inner, bytes):
                        strings.append(pb.as_str(inner))
            if label in _NAME_COLUMNS:
                found.update(strings)
    except Exception:  # noqa: BLE001 -- not a search response (dictionary, facets, text ...)
        return set()
    return found


def _json_strings(raw: bytes) -> set[str]:
    """account/name of a poe.ninja character-detail JSON."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(doc, dict) or "account" not in doc or "pathOfBuildingExport" not in doc and "items" not in doc:
        return set()
    return {v for k in ("account", "name") if isinstance(v := doc.get(k), str) and v}


def player_strings(path: Path) -> set[str]:
    raw = path.read_bytes()
    return _json_strings(raw) | _search_strings(raw)


def sample_files(root: Path = SAMPLES) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("_"))


def pseudonym(real: str, taken: dict[str, str]) -> str:
    """Same-byte-length ASCII pseudonym; unique per real string whenever the length leaves room for it."""
    n = len(real.encode("utf-8"))
    used = set(taken.values())
    cand = ""
    for counter in range(64):
        digest = hashlib.sha256(f"{real}#{counter}".encode("utf-8")).hexdigest()
        cand = ("anon_" + digest)[:n] if n >= 6 else ("_" + digest)[:n]
        if cand not in used:
            break
    return cand                                  # 1-3 byte names may share a pseudonym; nobody is identifiable by that


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _reencode(buf: bytes, edit) -> bytes:
    """Re-serialize a protobuf message field by field; edit(field_no, wire_type, value) -> value."""
    out = bytearray()
    for fn, wt, val in pb.iter_fields(buf):
        val = edit(fn, wt, val)
        out += _varint((fn << 3) | wt)
        if wt == 0:
            out += _varint(val)
        elif wt == 2:
            out += _varint(len(val)) + val
        else:                                     # fixed32 / fixed64 come back as raw bytes
            out += val
    return bytes(out)


def rewrite_search(raw: bytes, mapping: dict[str, str]) -> bytes:
    """Replace names structurally (only inside the name/account columns), never by blind byte search."""
    def swap(b: bytes) -> bytes:
        real = b.decode("utf-8")
        return mapping[real].encode("utf-8") if real in mapping else b

    def column(entry: bytes, old_schema: bool) -> bytes:
        label = next((pb.as_str(v) for f, w, v in pb.iter_fields(entry) if f in (1, 2) and w == 2), None)
        if label not in _NAME_COLUMNS:
            return entry
        def edit(f, w, v):
            if f == 7 and w == 2:
                return swap(v)
            if old_schema and f == 2 and w == 2 and label in _NAME_COLUMNS and pb.first(v, 1) is not None:
                return _reencode(v, lambda f2, w2, v2: swap(v2) if (f2 == 1 and w2 == 2) else v2)
            return v
        return _reencode(entry, edit)

    def body_edit(f, w, v):
        if w == 2 and f == 12:
            return column(v, False)
        if w == 2 and f == 5:
            return column(v, True)
        return v

    def top_edit(f, w, v):
        return _reencode(v, body_edit) if (f == 1 and w == 2) else v

    return _reencode(raw, top_edit)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    per_file = {p: player_strings(p) for p in sample_files()}
    leftovers = {p: {s for s in names if not is_pseudonym(s)} for p, names in per_file.items()}
    leftovers = {p: s for p, s in leftovers.items() if s}
    if check:
        for p, s in leftovers.items():
            print(f"{p.relative_to(SAMPLES)}: {len(s)} real-looking name(s)")
        return 1 if leftovers else 0

    mapping: dict[str, str] = {}
    for names in leftovers.values():
        for real in sorted(names):
            mapping[real] = pseudonym(real, mapping)
    # longest first so a name that contains another is not corrupted by the shorter replacement
    for p in leftovers:
        raw = p.read_bytes()
        if _json_strings(raw):
            for real, fake in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
                if real in leftovers[p]:
                    raw = raw.replace(json.dumps(real).encode("utf-8"), json.dumps(fake).encode("utf-8"))
                    raw = raw.replace(json.dumps(real, ensure_ascii=False).encode("utf-8"), json.dumps(fake).encode("utf-8"))
        else:
            assert rewrite_search(raw, {}) == raw, f"{p.name}: protobuf round-trip is not byte-identical"
            raw = rewrite_search(raw, mapping)
        p.write_bytes(raw)
        print(f"scrubbed {p.relative_to(SAMPLES)}: {len(leftovers[p])} name(s)")
    print(f"{len(mapping)} distinct names replaced")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
