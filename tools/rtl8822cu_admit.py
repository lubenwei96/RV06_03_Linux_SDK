#!/usr/bin/env python3
"""Safely admit and attest one RTL8822CU source archive."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath


MAX_ARCHIVE_BYTES = 268435456
MAX_MEMBERS = 20000
MAX_MEMBER_BYTES = 33554432
MAX_EXPANDED_BYTES = 536870912
MAX_COMPRESSION_RATIO = 100
PREBUILT_SUFFIXES = {".ko", ".o", ".a", ".so", ".exe", ".dll"}
BLOB_SUFFIXES = {".bin", ".fw", ".rom", ".img", ".dat", ".hex"}
VCS_SEGMENTS = {".git", ".svn", ".hg", "CVS"}
VCS_FILES = {".gitignore", ".gitattributes", ".gitmodules"}
RESERVED = "SOURCE-MANIFEST.json"
REQUIRED = ("Makefile", "core/", "hal/rtl8822c/", "include/", "os_dep/linux/usb_intf.c")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(name):
    if "\\" in name:
        raise ValueError(f"unsafe path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe path: {name!r}")
    if any(part in VCS_SEGMENTS for part in path.parts) or path.name in VCS_FILES:
        raise ValueError(f"VCS metadata rejected: {name}")
    if path.name == RESERVED:
        raise ValueError(f"reserved manifest name rejected: {name}")
    return path


def _inventory(path):
    members = []
    if zipfile.is_zipfile(path):
        kind = "zip"
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                name = _safe_name(item.filename.rstrip("/")) if item.filename.rstrip("/") else None
                if name is None:
                    continue
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError(f"link rejected: {item.filename}")
                if item.is_dir():
                    members.append({"name": name, "size": 0, "type": "dir"})
                else:
                    members.append({"name": name, "size": item.file_size, "type": "file"})
    else:
        kind = "tar"
        try:
            archive = tarfile.open(path, "r:*")
        except tarfile.TarError as exc:
            raise ValueError("unsupported archive") from exc
        with archive:
            for item in archive.getmembers():
                name = _safe_name(item.name.rstrip("/")) if item.name.rstrip("/") else None
                if name is None:
                    continue
                if item.issym() or item.islnk():
                    raise ValueError(f"link rejected: {item.name}")
                if item.isdir():
                    members.append({"name": name, "size": 0, "type": "dir"})
                elif item.isfile():
                    members.append({"name": name, "size": item.size, "type": "file"})
                else:
                    raise ValueError(f"special archive member rejected: {item.name}")
    if len(members) > MAX_MEMBERS:
        raise ValueError("member count limit exceeded")
    files = [item for item in members if item["type"] == "file"]
    if any(item["size"] > MAX_MEMBER_BYTES for item in files):
        raise ValueError("member size limit exceeded")
    expanded = sum(item["size"] for item in files)
    if expanded > MAX_EXPANDED_BYTES:
        raise ValueError("expanded size limit exceeded")
    archive_size = Path(path).stat().st_size
    if expanded > archive_size * MAX_COMPRESSION_RATIO:
        raise ValueError("compression ratio limit exceeded")
    names = [item["name"] for item in files]
    if len(names) != len(set(names)):
        raise ValueError("duplicate archive target")
    return kind, members


def _source_root(members):
    files = {item["name"] for item in members if item["type"] == "file"}
    candidates = []
    make_roots = []
    for path in files:
        if path.name != "Makefile":
            continue
        root = path.parent
        make_roots.append(root)
        def rel(value):
            return root / value if str(root) != "." else PurePosixPath(value)
        has_dirs = all(any(str(item).startswith(str(rel(prefix))) for item in files)
                       for prefix in ("core/", "hal/rtl8822c/", "include/"))
        has_usb = rel("os_dep/linux/usb_intf.c") in files
        licenses = sorted(item for item in files if item.parent == root and
                          (item.name == "COPYING" or item.name.startswith("COPYING.") or
                           item.name == "LICENSE" or item.name.startswith("LICENSE.")))
        if has_dirs and has_usb and licenses:
            candidates.append((root, licenses))
    if len(candidates) != 1:
        if len(make_roots) == 1:
            root = make_roots[0]
            def rel(value):
                return root / value if str(root) != "." else PurePosixPath(value)
            missing = []
            for prefix in ("core/", "hal/rtl8822c/", "include/"):
                if not any(str(item).startswith(str(rel(prefix))) for item in files):
                    missing.append(prefix)
            if rel("os_dep/linux/usb_intf.c") not in files:
                missing.append("os_dep/linux/usb_intf.c")
            if not any(item.parent == root and
                       (item.name == "COPYING" or item.name.startswith("COPYING.") or
                        item.name == "LICENSE" or item.name.startswith("LICENSE."))
                       for item in files):
                missing.append("LICENSE/COPYING")
            raise ValueError("required source markers/license missing: " + ", ".join(missing))
        raise ValueError(f"expected exactly one complete source root, found {len(candidates)}; license/markers missing")
    return candidates[0]


def _relative(path, root):
    return path.relative_to(root) if str(root) != "." else path


def _scan_bytes(relative, data):
    suffix = relative.suffix.lower()
    if suffix in PREBUILT_SUFFIXES or data.startswith((b"\x7fELF", b"MZ")):
        raise ValueError(f"prebuilt binary rejected: {relative}")
    if suffix in BLOB_SUFFIXES:
        raise ValueError(f"blob candidate rejected: {relative}")
    sample = data[:8192]
    if b"\x00" in sample or any(value < 9 or 13 < value < 32 for value in sample):
        raise ValueError(f"opaque binary content rejected: {relative}")


def _read_member(archive, kind, name):
    if kind == "zip":
        return archive.read(str(name))
    member = archive.getmember(str(name))
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"cannot read archive member: {name}")
    return stream.read()


def _files_manifest(root):
    root = Path(root)
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (path.exists() and not (path.is_dir() or path.is_file())):
            raise ValueError(f"staging contains link/special entry: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        _scan_bytes(PurePosixPath(relative), data)
        records.append({"path": relative, "type": "file", "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    return records


def admit_archive(archive, staging, manifest):
    archive, staging, manifest = Path(archive), Path(staging), Path(manifest)
    if not archive.is_file():
        raise FileNotFoundError(archive)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive size limit exceeded")
    if staging.exists():
        raise FileExistsError(f"staging already exists: {staging}")
    if manifest.exists():
        raise FileExistsError(f"manifest already exists: {manifest}")
    kind, members = _inventory(archive)
    root, license_paths = _source_root(members)
    stage_parent = staging.parent
    stage_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{staging.name}.", dir=stage_parent))
    manifest_tmp = None
    try:
        opener = zipfile.ZipFile(archive) if kind == "zip" else tarfile.open(archive, "r:*")
        with opener:
            seen = set()
            for item in members:
                if item["type"] != "file":
                    continue
                relative = _relative(item["name"], root)
                if str(relative) in seen:
                    raise ValueError(f"duplicate target after root stripping: {relative}")
                seen.add(str(relative))
                data = _read_member(opener, kind, item["name"])
                if len(data) != item["size"]:
                    raise ValueError(f"member size changed: {item['name']}")
                _scan_bytes(relative, data)
                target = temporary / str(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o644)
        records = _files_manifest(temporary)
        tree_json = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        data = {
            "schema_version": 1,
            "archive_name": archive.name,
            "archive_sha256": _sha256(archive),
            "archive_size_bytes": archive.stat().st_size,
            "source_root": str(root),
            "license_files": sorted(str(_relative(path, root)) for path in license_paths),
            "required_markers": list(REQUIRED),
            "file_count": len(records),
            "expanded_size_bytes": sum(item["size_bytes"] for item in records),
            "files": records,
            "binary_blob_candidates": [],
            "source_tree_sha256": hashlib.sha256(tree_json).hexdigest(),
            "imported_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        manifest.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=f".{manifest.name}.", dir=manifest.parent)
        os.close(handle)
        manifest_tmp = Path(temp_name)
        manifest_tmp.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, staging)
        try:
            os.replace(manifest_tmp, manifest)
        except Exception:
            shutil.rmtree(staging)
            raise
        return data
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        if manifest_tmp and manifest_tmp.exists():
            manifest_tmp.unlink()
        raise


def verify_staging(staging, manifest):
    staging, manifest = Path(staging), Path(manifest)
    if not staging.is_dir() or not manifest.is_file():
        raise ValueError("staging or manifest missing")
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    records = _files_manifest(staging)
    tree_json = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    actual_tree = hashlib.sha256(tree_json).hexdigest()
    if records != expected.get("files") or len(records) != expected.get("file_count"):
        raise ValueError("staging file manifest mismatch")
    if sum(item["size_bytes"] for item in records) != expected.get("expanded_size_bytes"):
        raise ValueError("staging expanded size mismatch")
    if actual_tree != expected.get("source_tree_sha256"):
        raise ValueError("staging tree hash mismatch")
    if expected.get("binary_blob_candidates") != []:
        raise ValueError("staging binary blob candidates not empty")
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--archive")
    group.add_argument("--verify-staging")
    parser.add_argument("--staging", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        if args.archive:
            admit_archive(args.archive, args.staging, args.manifest)
        else:
            if Path(args.verify_staging) != Path(args.staging):
                parser.error("--verify-staging must equal --staging")
            verify_staging(args.staging, args.manifest)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"rtl8822cu admission rejected: {exc}\n")


if __name__ == "__main__":
    main()
