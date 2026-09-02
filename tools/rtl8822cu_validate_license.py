#!/usr/bin/env python3
"""Validate a written RTL8822CU source/patch redistribution decision."""

import argparse
import json
import re
from pathlib import Path


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _object(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def validate(manifest_path, decision_path):
    manifest = _object(manifest_path)
    decision = _object(decision_path)
    if decision.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    for key in ("archive_sha256", "source_tree_sha256"):
        if not SHA256.fullmatch(str(manifest.get(key, ""))):
            raise ValueError(f"manifest {key} invalid")
        if decision.get(key) != manifest[key]:
            raise ValueError(f"{key} mismatch")
    licenses = manifest.get("license_files")
    if not isinstance(licenses, list) or not licenses or not all(isinstance(item, str) and item for item in licenses):
        raise ValueError("manifest license_files invalid")
    if decision.get("license_files") != licenses:
        raise ValueError("license_files mismatch")
    for key in ("source_repository_allowed", "patch_redistribution_allowed"):
        if decision.get(key) is not True:
            raise ValueError(f"{key} must be true")
    if manifest.get("binary_blob_candidates") != [] or decision.get("approved_binary_blobs") != []:
        raise ValueError("binary blob approval must be empty")
    for key in ("supplier", "obtained_at_utc", "reviewer", "reviewed_at_utc"):
        if not isinstance(decision.get(key), str) or not decision[key].strip():
            raise ValueError(f"{key} must be non-empty")
    if decision.get("engineering_only") is not True:
        raise ValueError("engineering_only must be true for this source")
    if decision.get("production_provenance_status") != "BLOCKED":
        raise ValueError("production_provenance_status must remain BLOCKED")
    return decision


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--decision", required=True)
    args = parser.parse_args(argv)
    try:
        validate(args.manifest, args.decision)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"rtl8822cu license decision rejected: {exc}\n")


if __name__ == "__main__":
    main()
