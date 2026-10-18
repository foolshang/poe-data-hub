"""Standalone tool: put a saved copy of a published JSON file back on Firebase (rollback).

Used before/after a recompute: back up the public file, run the job, and if the new output is wrong,
republish the backup exactly as it was. Goes through PricesPublisher.publish, so the readback verify and the
cache headers are the same as the job's own publishes.

Usage (on the VM, with the service credentials):
    GOOGLE_APPLICATION_CREDENTIALS=/opt/poe-data-hub/data/service-account.json \
        python -m tools.republish_blob BACKUP.json poe1/meta/latest.json            # dry run, publishes nothing
    ... python -m tools.republish_blob BACKUP.json poe1/meta/latest.json --apply     # actually publishes

Refuses anything that is not a JSON object, and by default refuses a blob path that does not already exist
on Firebase (a typo must not create a new public file).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.storage.publisher import PricesPublisher
from src.utils.config import load_config

DEFAULT_CACHE_CONTROL = "public, max-age=3600"  # what the meta job publishes meta/latest.json with


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup_file", type=Path)
    parser.add_argument("blob_path", help="e.g. poe1/meta/latest.json")
    parser.add_argument("--apply", action="store_true", help="publish for real (default: dry run)")
    parser.add_argument("--cache-control", default=DEFAULT_CACHE_CONTROL)
    parser.add_argument("--allow-new", action="store_true", help="allow a blob path that does not exist yet")
    args = parser.parse_args(argv)

    data = json.loads(args.backup_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("refusing: the backup is not a JSON object", file=sys.stderr)
        return 2

    cfg = load_config()
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))
    current = publisher.fetch_previous(args.blob_path)
    if current is None and not args.allow_new:
        print(f"refusing: {args.blob_path} does not exist on Firebase (use --allow-new if that is intended)", file=sys.stderr)
        return 2

    def describe(d: dict | None) -> str:
        if d is None:
            return "(missing)"
        return f"generated_at={d.get('generated_at')} league={d.get('league')} mods={len(d.get('mods', []))}"

    print(f"currently published : {describe(current)}")
    print(f"backup to republish : {describe(data)}  ({args.backup_file})")
    if not args.apply:
        print("dry run: nothing published (add --apply)")
        return 0
    publisher.publish(data, args.blob_path, cache_control=args.cache_control)
    print(f"republished {args.blob_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
