import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/rtl8822cu_hardware_evidence.py"
SCOPES = ["flash", "reset", "serial", "wlan-power", "wireless-transmit"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc(second):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc) + timedelta(seconds=second)
    return base.strftime("%Y-%m-%dT%H:%M:%SZ")


def dump(path, value):
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def load(path):
    return json.loads(path.read_text())


class HardwareEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = self.root / "update.img"
        self.module = self.root / "88x2cu.ko"
        self.image.write_bytes(b"engineering-image")
        self.module.write_bytes(b"arm-module")
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        (self.evidence / "soak").mkdir(mode=0o700)
        self.summary = self.root / "summary.txt"
        self._build_valid_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def _build_valid_fixture(self):
        report = self.evidence / "firstboard-report.txt"
        report.write_text("AICPM_FIRSTBOARD_REPORT_V1\nboard=ok\n")
        dump(
            self.evidence / "authorization.json",
            {
                "authorization_id": "AUTH-20260902-001",
                "authorized_utc": utc(0),
                "image_sha256": sha(self.image),
                "scope": SCOPES,
            },
        )
        dump(
            self.evidence / "flash-record.json",
            {
                "source_image": str(self.image),
                "image_sha256": sha(self.image),
                "native_exit_code": 0,
                "started_utc": utc(1),
                "ended_utc": utc(2),
                "board_id_sha256": hashlib.sha256(b"board-1").hexdigest(),
                "camera_p1_p2_disconnected": True,
                "transfer_channel": "serial-binary-safe",
                "transfer_tool_version": "fixture-1",
            },
        )
        dump(
            self.evidence / "firstboard-identity.json",
            {
                "captured_utc": utc(3),
                "report_sha256": sha(report),
                "board_model": "AICPM RV1106G V1.0",
                "wifi_chip_type": "RTL8822CU_USB",
                "device_module_sha256": sha(self.module),
                "boot_id_sha256": hashlib.sha256(b"boot-initial").hexdigest(),
                "camera_p1_p2_disconnected": True,
            },
        )
        dump(
            self.evidence / "usb.json",
            {
                "captured_utc": utc(4),
                "vid": "0BDA",
                "pid": "C82C",
                "enumeration_errors": 0,
            },
        )
        reconnect = []
        for round_no in range(1, 101):
            reconnect.append(
                {
                    "round": round_no,
                    "utc": utc(4 + round_no),
                    "associated": True,
                    "dhcp_bound": True,
                    "freq": 2412,
                    "ping_ok": True,
                    "load_failed": False,
                }
            )
        (self.evidence / "reconnect.jsonl").write_text(
            "".join(json.dumps(v, sort_keys=True, separators=(",", ":")) + "\n" for v in reconnect)
        )
        cold = []
        for round_no in range(1, 31):
            cold.append(
                {
                    "round": round_no,
                    "utc": utc(104 + round_no),
                    "boot_id_sha256": hashlib.sha256(f"boot-{round_no}".encode()).hexdigest(),
                    "auto_loaded": True,
                    "usb_present": True,
                    "wlan_present": True,
                    "device_module_sha256": sha(self.module),
                    "load_failed": False,
                }
            )
        (self.evidence / "cold-boot.jsonl").write_text(
            "".join(json.dumps(v, sort_keys=True, separators=(",", ":")) + "\n" for v in cold)
        )
        sample_lines = [
            "utc\tround\tinterface_present\twpa_state\tfreq\tsignal_dbm\tping_ok\tmemavailable_kib\tkernel_error_count\tload_failed\tsample_status\n"
        ]
        for round_no in range(1, 1441):
            sample_lines.append(
                f"{utc(134 + round_no * 60)}\t{round_no}\t1\tCOMPLETED\t2412\t-45\t1\t65536\t0\t0\tCOMPLETED\n"
            )
        (self.evidence / "soak/samples.tsv").write_text("".join(sample_lines))
        (self.evidence / "soak/summary.txt").write_text(
            "result=PASS\n"
            "samples_completed=1440/1440\n"
            "interface_present=1440/1440\n"
            "wpa_completed=1440/1440\n"
            "frequency_2g=1440/1440\n"
            "ping_success=1440/1440\n"
            "load_failed_samples=0\n"
            "kernel_error_growth=0\n"
            "memory_floor_pass=1\n"
            "first_60_memavailable_avg_kib=65536\n"
            "last_60_memavailable_avg_kib=65536\n"
        )
        (self.evidence / "soak/exit-code").write_text("0\n")

    def _run(self, *extra, evidence=None):
        command = [
            "python3",
            str(TOOL),
            "--evidence-dir",
            str(evidence or self.evidence),
            "--expected-image",
            str(self.image),
            "--expected-module",
            str(self.module),
            *map(str, extra),
        ]
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def _generate(self):
        return self._run("--output", self.summary)

    def _rewrite_jsonl(self, name, records):
        (self.evidence / name).write_text(
            "".join(json.dumps(v, sort_keys=True, separators=(",", ":")) + "\n" for v in records)
        )

    def test_01_valid_fixture_generates_and_verifies_summary(self):
        result = self._generate()
        self.assertEqual(0, result.returncode, result.stderr)
        text = self.summary.read_text()
        for item in (
            "hardware_validation=PASS",
            "reconnect=100/100",
            "cold_boot=30/30",
            "soak=1440/1440",
            "soak_exit=0",
            f"tested_image_sha256={sha(self.image)}",
            f"device_module_sha256={sha(self.module)}",
        ):
            self.assertIn(item, text)
        verify = self._run("--verify-summary", self.summary)
        self.assertEqual(0, verify.returncode, verify.stderr)

    def test_02_print_tree_sha_is_exact_lowercase_hash(self):
        result = self._run("--print-tree-sha")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(result.stdout, r"^[0-9a-f]{64}\n$")

    def test_03_missing_or_duplicate_reconnect_round_is_rejected(self):
        path = self.evidence / "reconnect.jsonl"
        records = [json.loads(v) for v in path.read_text().splitlines()]
        for mutant in (records[:-1], records[:-1] + [records[-2]]):
            self._rewrite_jsonl("reconnect.jsonl", mutant)
            self.assertNotEqual(0, self._generate().returncode)
            if self.summary.exists():
                self.summary.unlink()
        self._rewrite_jsonl("reconnect.jsonl", records)

    def test_04_extra_missing_symlink_and_fifo_paths_are_rejected(self):
        extra = self.evidence / "extra.txt"
        extra.write_text("x")
        self.assertNotEqual(0, self._generate().returncode)
        extra.unlink()
        report = self.evidence / "firstboard-report.txt"
        saved = report.read_bytes()
        report.unlink()
        self.assertNotEqual(0, self._generate().returncode)
        report.symlink_to(self.image)
        self.assertNotEqual(0, self._generate().returncode)
        report.unlink()
        report.write_bytes(saved)
        fifo = self.evidence / "extra-fifo"
        os.mkfifo(fifo)
        self.assertNotEqual(0, self._generate().returncode)

    def test_05_root_symlink_and_file_hardlink_are_rejected(self):
        alias = self.root / "evidence-link"
        alias.symlink_to(self.evidence, target_is_directory=True)
        self.assertNotEqual(0, self._run("--print-tree-sha", evidence=alias).returncode)
        report = self.evidence / "firstboard-report.txt"
        report.unlink()
        os.link(self.evidence / "authorization.json", report)
        self.assertNotEqual(0, self._run("--print-tree-sha").returncode)

    def test_06_module_image_and_report_hash_tampering_are_rejected(self):
        identity = self.evidence / "firstboard-identity.json"
        original_identity = load(identity)
        changed = dict(original_identity, device_module_sha256="0" * 64)
        dump(identity, changed)
        self.assertNotEqual(0, self._generate().returncode)
        dump(identity, original_identity)
        report = self.evidence / "firstboard-report.txt"
        report.write_text(report.read_text() + "changed\n")
        self.assertNotEqual(0, self._generate().returncode)

    def test_07_each_image_binding_mismatch_is_rejected(self):
        auth_path = self.evidence / "authorization.json"
        flash_path = self.evidence / "flash-record.json"
        auth = load(auth_path)
        flash = load(flash_path)
        dump(auth_path, dict(auth, image_sha256="1" * 64))
        self.assertNotEqual(0, self._generate().returncode)
        dump(auth_path, auth)
        dump(flash_path, dict(flash, image_sha256="2" * 64))
        self.assertNotEqual(0, self._generate().returncode)
        dump(flash_path, flash)
        self.image.write_bytes(b"changed-expected-image")
        self.assertNotEqual(0, self._generate().returncode)

    def test_08_camera_disconnect_scope_vid_and_time_are_required(self):
        flash_path = self.evidence / "flash-record.json"
        flash = load(flash_path)
        dump(flash_path, dict(flash, camera_p1_p2_disconnected=False))
        self.assertNotEqual(0, self._generate().returncode)
        dump(flash_path, flash)
        auth_path = self.evidence / "authorization.json"
        auth = load(auth_path)
        dump(auth_path, dict(auth, scope=SCOPES[:-1]))
        self.assertNotEqual(0, self._generate().returncode)
        dump(auth_path, auth)
        usb_path = self.evidence / "usb.json"
        usb = load(usb_path)
        dump(usb_path, dict(usb, vid=""))
        self.assertNotEqual(0, self._generate().returncode)
        dump(usb_path, usb)
        dump(flash_path, dict(flash, started_utc=utc(-1)))
        self.assertNotEqual(0, self._generate().returncode)

    def test_09_soak_failure_short_or_old_rounds_are_rejected(self):
        exit_code = self.evidence / "soak/exit-code"
        exit_code.write_text("1\n")
        self.assertNotEqual(0, self._generate().returncode)
        exit_code.write_text("0\n")
        samples = self.evidence / "soak/samples.tsv"
        lines = samples.read_text().splitlines(True)
        samples.write_text("".join(lines[:-1] + [lines[-2]]))
        self.assertNotEqual(0, self._generate().returncode)

    def test_10_sensitive_fields_with_or_without_spaces_are_rejected(self):
        report = self.evidence / "firstboard-report.txt"
        for secret in ("ssid=value\n", " network = {\n password = value\n"):
            original = report.read_text()
            report.write_text(original + secret)
            self.assertNotEqual(0, self._generate().returncode)
            report.write_text(original)

    def test_11_manual_summary_change_is_rejected(self):
        self.assertEqual(0, self._generate().returncode)
        self.summary.write_text(self.summary.read_text().replace("soak_exit=0", "soak_exit=1"))
        result = self._run("--verify-summary", self.summary)
        self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
