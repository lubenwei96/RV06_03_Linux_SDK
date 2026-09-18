#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


FILES = {
    "authorization.json",
    "flash-record.json",
    "firstboard-report.txt",
    "firstboard-identity.json",
    "usb.json",
    "reconnect.jsonl",
    "cold-boot.jsonl",
    "soak/samples.tsv",
    "soak/summary.txt",
    "soak/exit-code",
}
DIRECTORIES = {".", "soak"}
SCOPES = ["flash", "reset", "serial", "wlan-power", "wireless-transmit"]
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HEX4_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
SECRET_RE = re.compile(
    rb"(?im)^[ \t]*(?:network[ \t]*=[ \t]*\{|(?:ssid|bssid|psk|password)[ \t]*=)"
)
IPV4_RE = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
MAC_RE = re.compile(rb"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])")
SAMPLE_FIELDS = [
    "utc",
    "round",
    "interface_present",
    "wpa_state",
    "freq",
    "signal_dbm",
    "ping_ok",
    "memavailable_kib",
    "kernel_error_count",
    "load_failed",
    "sample_status",
]
SOAK_SUMMARY_KEYS = {
    "result",
    "samples_completed",
    "interface_present",
    "wpa_completed",
    "frequency_2g",
    "ping_success",
    "load_failed_samples",
    "kernel_error_growth",
    "memory_floor_pass",
    "first_60_memavailable_avg_kib",
    "last_60_memavailable_avg_kib",
}


class EvidenceError(Exception):
    pass


def fail(message):
    raise EvidenceError(message)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_file(path, label):
    try:
        info = os.lstat(path)
    except OSError as exc:
        fail(f"{label}: {exc}")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        fail(f"{label} must be a non-link regular file")
    if info.st_nlink != 1:
        fail(f"{label} must have exactly one hard link")
    return Path(path)


def inspect_tree(root, require_exact):
    root = Path(root)
    try:
        root_info = os.lstat(root)
    except OSError as exc:
        fail(f"evidence root: {exc}")
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        fail("evidence root must be a non-link directory")

    found_files = set()
    found_dirs = {"."}
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_dir = directory_path.relative_to(root).as_posix()
        if relative_dir == ".":
            relative_dir = "."
        for name in list(dirnames):
            path = directory_path / name
            info = os.lstat(path)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                fail(f"non-directory or linked directory: {relative}")
            found_dirs.add(relative)
        for name in filenames:
            path = directory_path / name
            info = os.lstat(path)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                fail(f"non-regular evidence leaf: {relative}")
            if info.st_nlink != 1:
                fail(f"hard-linked evidence leaf: {relative}")
            found_files.add(relative)
    if require_exact:
        if found_dirs != DIRECTORIES:
            fail(f"unexpected evidence directories: {sorted(found_dirs ^ DIRECTORIES)}")
        if found_files != FILES:
            fail(f"unexpected evidence files: {sorted(found_files ^ FILES)}")
    return root, sorted(found_files)


def tree_records_and_sha(root, files):
    records = []
    for relative in sorted(files):
        path = root / relative
        records.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    payload = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return records, sha256_bytes(payload)


def read_json(path, keys):
    regular_file(path, str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"invalid JSON {path.name}: {exc}")
    if not isinstance(value, dict) or set(value) != set(keys):
        fail(f"invalid fields in {path.name}")
    return value


def read_jsonl(path, keys, count):
    regular_file(path, str(path))
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        fail(f"invalid JSONL {path.name}: {exc}")
    if len(lines) != count or any(not line for line in lines):
        fail(f"{path.name} must contain {count} records")
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(f"invalid JSONL {path.name}: {exc}")
        if not isinstance(value, dict) or set(value) != set(keys):
            fail(f"invalid fields in {path.name}")
        records.append(value)
    return records


def parse_utc(value, label):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        fail(f"invalid UTC timestamp for {label}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        fail(f"invalid UTC timestamp for {label}")
    return parsed


def require_sha(value, label):
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        fail(f"invalid SHA-256 for {label}")
    return value


def require_bool(value, expected, label):
    if not isinstance(value, bool) or value is not expected:
        fail(f"invalid boolean for {label}")


def require_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"invalid integer for {label}")
    return value


def scan_sensitive(root, files):
    for relative in files:
        data = (root / relative).read_bytes()
        if SECRET_RE.search(data) or IPV4_RE.search(data) or MAC_RE.search(data):
            fail(f"sensitive network value in {relative}")


def parse_key_values(path, required_keys):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            fail(f"invalid key/value line in {path.name}")
        key, value = line.split("=", 1)
        if not key or key in values:
            fail(f"duplicate key in {path.name}")
        values[key] = value
    if set(values) != set(required_keys):
        fail(f"invalid keys in {path.name}")
    return values


def validate_samples(path):
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != SAMPLE_FIELDS:
            fail("invalid soak sample fields")
        rows = list(reader)
    if len(rows) != 1440:
        fail("soak must contain exactly 1440 samples")
    times = []
    memory = []
    errors = []
    for index, row in enumerate(rows, 1):
        if int_value(row["round"], "soak round") != index:
            fail("soak rounds are missing, duplicated, or stale")
        times.append(parse_utc(row["utc"], "soak sample"))
        if row["interface_present"] != "1" or row["wpa_state"] != "COMPLETED":
            fail("soak association is incomplete")
        freq = int_value(row["freq"], "soak frequency")
        if not 2400 <= freq <= 2499:
            fail("soak frequency is not 2.4 GHz")
        if row["ping_ok"] != "1" or row["load_failed"] != "0":
            fail("soak connectivity or loader failure")
        if row["sample_status"] != "COMPLETED":
            fail("soak sample is incomplete")
        mem = int_value(row["memavailable_kib"], "MemAvailable")
        if mem < 24576:
            fail("soak memory floor failed")
        memory.append(mem)
        errors.append(int_value(row["kernel_error_count"], "kernel errors"))
    if times != sorted(times) or len(set(times)) != len(times):
        fail("soak timestamps are not strictly increasing")
    if any(value > errors[0] for value in errors):
        fail("kernel error count grew during soak")
    first_average = sum(memory[:60]) // 60
    last_average = sum(memory[-60:]) // 60
    if last_average < first_average - 16384:
        fail("soak memory trend failed")
    return times, first_average, last_average


def int_value(value, label):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        fail(f"invalid integer for {label}")
    return parsed


def validate_evidence(evidence_dir, expected_image, expected_module):
    root, files = inspect_tree(evidence_dir, require_exact=True)
    scan_sensitive(root, files)
    expected_image = regular_file(expected_image, "expected image")
    expected_module = regular_file(expected_module, "expected module")
    image_sha = sha256_file(expected_image)
    module_sha = sha256_file(expected_module)

    authorization = read_json(
        root / "authorization.json",
        {"authorization_id", "authorized_utc", "image_sha256", "scope"},
    )
    if not isinstance(authorization["authorization_id"], str) or not authorization[
        "authorization_id"
    ]:
        fail("authorization id is empty")
    if authorization["scope"] != SCOPES:
        fail("authorization scope is incomplete or reordered")
    if require_sha(authorization["image_sha256"], "authorization image") != image_sha:
        fail("authorization image hash mismatch")
    authorized_time = parse_utc(authorization["authorized_utc"], "authorization")

    flash = read_json(
        root / "flash-record.json",
        {
            "source_image",
            "image_sha256",
            "native_exit_code",
            "started_utc",
            "ended_utc",
            "board_id_sha256",
            "camera_p1_p2_disconnected",
            "transfer_channel",
            "transfer_tool_version",
        },
    )
    if not isinstance(flash["source_image"], str) or not Path(flash["source_image"]).is_absolute():
        fail("flash source image must be an absolute path")
    if require_sha(flash["image_sha256"], "flash image") != image_sha:
        fail("flash image hash mismatch")
    if require_int(flash["native_exit_code"], "flash exit") != 0:
        fail("native flash command failed")
    require_sha(flash["board_id_sha256"], "board id")
    require_bool(flash["camera_p1_p2_disconnected"], True, "flash camera state")
    for key in ("transfer_channel", "transfer_tool_version"):
        if not isinstance(flash[key], str) or not flash[key]:
            fail(f"missing {key}")
    flash_start = parse_utc(flash["started_utc"], "flash start")
    flash_end = parse_utc(flash["ended_utc"], "flash end")

    report = regular_file(root / "firstboard-report.txt", "firstboard report")
    identity = read_json(
        root / "firstboard-identity.json",
        {
            "captured_utc",
            "report_sha256",
            "board_model",
            "wifi_chip_type",
            "device_module_sha256",
            "boot_id_sha256",
            "camera_p1_p2_disconnected",
        },
    )
    if require_sha(identity["report_sha256"], "report") != sha256_file(report):
        fail("firstboard report hash mismatch")
    if identity["board_model"] != "AICPM RV1106G V1.0":
        fail("wrong board model")
    if identity["wifi_chip_type"] != "RTL8822CU_USB":
        fail("wrong Wi-Fi marker")
    if require_sha(identity["device_module_sha256"], "device module") != module_sha:
        fail("device module hash mismatch")
    require_sha(identity["boot_id_sha256"], "initial boot id")
    require_bool(identity["camera_p1_p2_disconnected"], True, "identity camera state")
    identity_time = parse_utc(identity["captured_utc"], "firstboard identity")

    usb = read_json(
        root / "usb.json", {"captured_utc", "vid", "pid", "enumeration_errors"}
    )
    if not isinstance(usb["vid"], str) or not HEX4_RE.fullmatch(usb["vid"]):
        fail("missing or invalid USB VID")
    if not isinstance(usb["pid"], str) or not HEX4_RE.fullmatch(usb["pid"]):
        fail("missing or invalid USB PID")
    if require_int(usb["enumeration_errors"], "USB enumeration errors") != 0:
        fail("USB enumeration errors were recorded")
    usb_time = parse_utc(usb["captured_utc"], "USB capture")

    reconnect = read_jsonl(
        root / "reconnect.jsonl",
        {"round", "utc", "associated", "dhcp_bound", "freq", "ping_ok", "load_failed"},
        100,
    )
    reconnect_times = []
    for index, record in enumerate(reconnect, 1):
        if require_int(record["round"], "reconnect round") != index:
            fail("reconnect rounds are missing, duplicated, or stale")
        for field in ("associated", "dhcp_bound", "ping_ok"):
            require_bool(record[field], True, f"reconnect {field}")
        require_bool(record["load_failed"], False, "reconnect load failure")
        if not 2400 <= require_int(record["freq"], "reconnect frequency") <= 2499:
            fail("reconnect frequency is not 2.4 GHz")
        reconnect_times.append(parse_utc(record["utc"], "reconnect"))

    cold = read_jsonl(
        root / "cold-boot.jsonl",
        {
            "round",
            "utc",
            "boot_id_sha256",
            "auto_loaded",
            "usb_present",
            "wlan_present",
            "device_module_sha256",
            "load_failed",
        },
        30,
    )
    cold_times = []
    boot_ids = set()
    for index, record in enumerate(cold, 1):
        if require_int(record["round"], "cold boot round") != index:
            fail("cold-boot rounds are missing, duplicated, or stale")
        for field in ("auto_loaded", "usb_present", "wlan_present"):
            require_bool(record[field], True, f"cold boot {field}")
        require_bool(record["load_failed"], False, "cold boot load failure")
        boot_id = require_sha(record["boot_id_sha256"], "cold boot id")
        if boot_id in boot_ids:
            fail("cold boot ids are not unique")
        boot_ids.add(boot_id)
        if require_sha(record["device_module_sha256"], "cold module") != module_sha:
            fail("cold-boot module hash mismatch")
        cold_times.append(parse_utc(record["utc"], "cold boot"))

    soak_times, first_avg, last_avg = validate_samples(root / "soak/samples.tsv")
    soak_summary = parse_key_values(root / "soak/summary.txt", SOAK_SUMMARY_KEYS)
    expected_soak = {
        "result": "PASS",
        "samples_completed": "1440/1440",
        "interface_present": "1440/1440",
        "wpa_completed": "1440/1440",
        "frequency_2g": "1440/1440",
        "ping_success": "1440/1440",
        "load_failed_samples": "0",
        "kernel_error_growth": "0",
        "memory_floor_pass": "1",
        "first_60_memavailable_avg_kib": str(first_avg),
        "last_60_memavailable_avg_kib": str(last_avg),
    }
    if soak_summary != expected_soak:
        fail("soak summary does not match raw samples")
    if (root / "soak/exit-code").read_text(encoding="utf-8") != "0\n":
        fail("soak exit code is not zero")

    all_times = [
        authorized_time,
        flash_start,
        flash_end,
        identity_time,
        usb_time,
        *reconnect_times,
        *cold_times,
        *soak_times,
    ]
    if any(earlier >= later for earlier, later in zip(all_times, all_times[1:])):
        fail("evidence UTC values are not strictly monotonic")

    _, tree_sha = tree_records_and_sha(root, files)
    return {
        "hardware_validation": "PASS",
        "authorization_id": authorization["authorization_id"],
        "tested_image_sha256": image_sha,
        "usb_vid": usb["vid"].upper(),
        "usb_pid": usb["pid"].upper(),
        "device_module_sha256": module_sha,
        "reconnect": "100/100",
        "cold_boot": "30/30",
        "soak": "1440/1440",
        "soak_exit": "0",
        "started_utc": authorization["authorized_utc"],
        "ended_utc": soak_times[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "raw_evidence_tree_sha256": tree_sha,
    }


SUMMARY_ORDER = [
    "hardware_validation",
    "authorization_id",
    "tested_image_sha256",
    "usb_vid",
    "usb_pid",
    "device_module_sha256",
    "reconnect",
    "cold_boot",
    "soak",
    "soak_exit",
    "started_utc",
    "ended_utc",
    "raw_evidence_tree_sha256",
]


def summary_bytes(values):
    return "".join(f"{key}={values[key]}\n" for key in SUMMARY_ORDER).encode("utf-8")


def atomic_create(path, data):
    path = Path(path)
    if path.exists() or path.is_symlink():
        fail("output already exists")
    if not path.parent.is_dir() or path.parent.is_symlink():
        fail("output parent must be a non-link directory")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    temporary.unlink()


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--expected-image")
    parser.add_argument("--expected-module")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--output")
    actions.add_argument("--verify-summary")
    actions.add_argument("--print-tree-sha", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.print_tree_sha:
            root, files = inspect_tree(args.evidence_dir, require_exact=False)
            _, digest = tree_records_and_sha(root, files)
            print(digest)
            return 0
        if not args.expected_image or not args.expected_module:
            fail("expected image and module are required")
        values = validate_evidence(
            args.evidence_dir, args.expected_image, args.expected_module
        )
        expected = summary_bytes(values)
        if args.output:
            atomic_create(args.output, expected)
            return 0
        summary = regular_file(args.verify_summary, "summary")
        if summary.read_bytes() != expected:
            fail("summary does not match raw evidence")
        return 0
    except (EvidenceError, OSError, ValueError) as exc:
        print(f"evidence validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
