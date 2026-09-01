import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.aicpm_bsp.common import ROOT, read_text


SCRIPT = "project/cfg/BoardConfig_IPC/overlay/aicpm-v1/etc/init.d/S30aicpm-firstboard-diag"
SAFE_WPA = "project/cfg/BoardConfig_IPC/overlay/aicpm-v1/etc/wpa_supplicant.conf"
BOARD = "project/cfg/BoardConfig_IPC/BoardConfig-SPI_NAND-NONE-RV1106_AICPM-V1.mk"
BUILD = "project/build.sh"
BASE_COMMIT = "9257b117a258ca5e12e23901cbf032461c989d39"
BASELINE_WPA = "sysdrv/tools/board/buildroot/overlay/etc/wpa_supplicant.conf"


def exported(board, name):
    match = re.search(rf"^export\s+{re.escape(name)}=(.*)$", board, re.MULTILINE)
    if not match:
        raise AssertionError(f"missing export {name}")
    return match.group(1).strip().strip('"')


class DiagnosticScriptContract(unittest.TestCase):
    """Exercise the actual init script using disposable read-only command fakes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake-bin"
        self.fake.mkdir()
        self.report = self.root / "run" / "aicpm-firstboard-report.txt"
        self.report.parent.mkdir()
        self.calls = self.root / "calls.log"
        self._defaults()

    def tearDown(self):
        self.tmp.cleanup()

    def _cmd(self, name, body):
        path = self.fake / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def _defaults(self):
        self._cmd("cat", 'printf "read:%s\\n" "$*"')
        self._cmd("find", 'printf "find:%s\\n" "$*"')
        self._cmd("basename", 'exec /usr/bin/basename "$@"')
        self._cmd("date", 'printf "2026-09-02T00:00:00+00:00\\n"')
        self._cmd("uname", 'printf "AICPM test kernel\\n"')
        self._cmd("dmesg", 'printf "kernel psk=very-secret-value\\n"')
        self._cmd("tail", 'exec /usr/bin/tail "$@"')
        self._cmd("ip", 'if [ "$1" = "-br" ]; then printf "lo UNKNOWN\\n"; else printf "1: lo: <LOOPBACK>\\n"; fi')
        self._cmd("sed", 'exec /bin/sed "$@"')
        self._cmd("mktemp", 'exec /usr/bin/mktemp "$@"')
        self._cmd("mv", 'printf "mv\\n" >> "$FAKE_LOG"; exec /bin/mv "$@"')
        self._cmd("chmod", 'exec /bin/chmod "$@"')
        self._cmd("rm", 'exec /bin/rm "$@"')
        self._cmd("logger", 'printf "logger:%s\\n" "$*" >&2')

    def _copy(self):
        source = (ROOT / SCRIPT).read_text(encoding="utf-8")
        fixed = "REPORT=/run/aicpm-firstboard-report.txt"
        self.assertIn(fixed, source)
        copy = self.root / "S30aicpm-firstboard-diag"
        copy.write_text(source.replace(fixed, f"REPORT={self.report}"), encoding="utf-8")
        copy.chmod(0o755)
        return copy

    def _run(self, action="start"):
        return subprocess.run(
            [str(self._copy()), action], text=True, capture_output=True, check=False,
            env={"PATH": f"{self.fake}:/usr/bin:/bin", "FAKE_LOG": str(self.calls), "LC_ALL": "C"},
        )

    def _temps(self):
        return list(self.report.parent.glob("aicpm-firstboard-report.txt.tmp.*"))

    def test_start_collects_redacts_and_publishes_atomically(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report.read_text(encoding="utf-8")
        for heading in (
            "AICPM_FIRSTBOARD_REPORT_V1", "== /proc/cmdline ==", "== /proc/mtd ==",
            "== /proc/partitions ==", "== /proc/meminfo ==", "== /sys/class/mtd ==",
            "== /sys/class/mmc_host ==", "== /sys/class/net ==", "== /sys/class/pwm ==",
            "== USB ==", "== network ==", "== dmesg tail ==",
        ):
            self.assertIn(heading, report)
        self.assertIn("psk=[REDACTED]", report)
        self.assertNotIn("very-secret-value", report)
        self.assertEqual(stat.S_IMODE(self.report.stat().st_mode), 0o600)
        self.assertEqual(self.calls.read_text(encoding="utf-8").splitlines(), ["mv"])
        self.assertEqual(self._temps(), [])

    def test_read_command_failures_have_markers_without_blocking_boot(self):
        cases = {
            "cat": ("exit 17", "AICPM_DIAG_ERROR read_proc_cmdline rc=17"),
            "find": ("exit 18", "AICPM_DIAG_ERROR scan_sys_class_mtd rc=18"),
            "dmesg": ("exit 19", "AICPM_DIAG_ERROR dmesg rc=19"),
        }
        for command, (body, marker) in cases.items():
            with self.subTest(command=command):
                self._defaults()
                self._cmd(command, body)
                result = self._run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(marker, self.report.read_text(encoding="utf-8"))
                self.report.unlink()

    def test_ip_fallback_and_both_failure_markers(self):
        self._cmd("ip", 'if [ "$1" = "-br" ]; then exit 23; fi; printf "1: lo: <LOOPBACK>\\n"')
        self.assertEqual(self._run().returncode, 0)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("AICPM_DIAG_ERROR ip_br_link rc=23", report)
        self.assertIn("1: lo: <LOOPBACK>", report)
        self.report.unlink()
        self._cmd("ip", "exit 24")
        self.assertEqual(self._run().returncode, 0)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("AICPM_DIAG_ERROR ip_br_link rc=24", report)
        self.assertIn("AICPM_DIAG_ERROR ip_link rc=24", report)

    def test_mktemp_and_mv_failures_preserve_an_existing_complete_report(self):
        for command in ("mktemp", "mv"):
            with self.subTest(command=command):
                self.report.write_text("old-complete\n", encoding="utf-8")
                self.report.chmod(0o600)
                self._cmd(command, "exit 31")
                self.assertEqual(self._run().returncode, 0)
                self.assertEqual(self.report.read_text(encoding="utf-8"), "old-complete\n")
                self.assertEqual(self._temps(), [])
                self._defaults()

    def test_repeated_start_replaces_complete_final_without_tmp_residue(self):
        self.assertEqual(self._run().returncode, 0)
        self.report.write_text("old-complete\n", encoding="utf-8")
        self.assertEqual(self._run().returncode, 0)
        self.assertTrue(self.report.read_text(encoding="utf-8").startswith("AICPM_FIRSTBOARD_REPORT_V1"))
        self.assertEqual(self._temps(), [])

    def test_stop_is_noop_and_usage_is_nonzero(self):
        self.assertEqual(self._run("stop").returncode, 0)
        self.assertFalse(self.report.exists())
        result = self._run("unexpected")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)

    def test_overlay_is_exact_and_is_consumed_only_by_firmware_post_overlay(self):
        self.assertEqual((ROOT / SAFE_WPA).read_bytes(), b"ctrl_interface=/var/run/wpa_supplicant\nupdate_config=0\n")
        board = read_text(BOARD)
        self.assertEqual(exported(board, "RK_POST_OVERLAY"), "aicpm-v1")
        self.assertEqual(exported(board, "RK_ENABLE_WIFI_APP"), "n")
        self.assertEqual(exported(board, "RK_ENABLE_WIFI"), "n")
        self.assertEqual(exported(board, "RK_ENABLE_WIFI_CHIP"), "")
        build = read_text(BUILD)
        post = build[build.index("function post_overlay()"):]
        post = post[:post.index("\n}\n")]
        firmware = build[build.index("function build_firmware()"):]
        firmware = firmware[:firmware.index("\n}\n")]
        rootfs = build[build.index("function build_rootfs()"):]
        rootfs = rootfs[:rootfs.index("\n}\n")]
        self.assertIn("overlay/$RK_POST_OVERLAY", post)
        self.assertIn("post_overlay", firmware)
        self.assertNotIn("post_overlay", rootfs)
        subprocess.run(["git", "diff", "--quiet", BASE_COMMIT, "--", BASELINE_WPA], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    def test_static_safety_rejects_direct_and_wrapper_bypass_mutants(self):
        script = read_text(SCRIPT)
        forbidden = re.compile(
            r"(?im)(?:^|[;|&()\s])(?:"
            r"(?:/(?:sbin|bin|usr/bin)/)?(?:insmod|modprobe|rmmod|gpioset|gpioget|gpioinfo|pwm|wpa_supplicant|hostapd|udhcpc|dhclient|ifconfig|reboot|shutdown|poweroff)|"
            r"(?:busybox|command|env)\s+(?:insmod|modprobe|rmmod|gpioset|gpioget|gpioinfo|pwm|wpa_supplicant|hostapd|udhcpc|dhclient|ifconfig|reboot|shutdown|poweroff)|"
            r"ip\s+link\s+set)(?=$|[\s;|&()])|/dev/aicpm-l9110s|>\s*/(?:sys|proc|dev)/|"
            r"/(?:etc/(?:wpa_supplicant|.*mqtt|.*cloud)|.*(?:credential|secret|private))"
        )
        indirect = re.compile(
            r"(?im)(?:^|[;\n])\s*[A-Za-z_][A-Za-z0-9_]*=(?:insmod|modprobe|rmmod|gpioset|gpioget|gpioinfo|pwm|wpa_supplicant|hostapd|udhcpc|dhclient|ifconfig|reboot|shutdown|poweroff)(?=$|[;\s])|"
            r"\"\$[A-Za-z_][A-Za-z0-9_]*\"\s+(?:insmod|modprobe|rmmod|gpioset|gpioget|gpioinfo|pwm|wpa_supplicant|hostapd|udhcpc|dhclient|ifconfig|reboot|shutdown|poweroff)"
        )
        self.assertIsNone(forbidden.search(script))
        self.assertIsNone(indirect.search(script))
        for mutant in (
            "/sbin/modprobe rtl8822cu", "busybox modprobe rtl8822cu", "command modprobe rtl8822cu",
            "env modprobe rtl8822cu", "gpioset gpiochip0 1=1", "echo 1 > /sys/class/pwm/pwmchip0/export",
            "cat /dev/aicpm-l9110s0", "ip link set eth0 up", "wpa_supplicant -i wlan0", "reboot",
            "runner=modprobe; \"$runner\" rtl8822cu",
        ):
            with self.subTest(mutant=mutant):
                self.assertTrue(forbidden.search(mutant) or indirect.search(mutant))
        self.assertIn("REPORT=/run/aicpm-firstboard-report.txt", script)
        self.assertNotRegex(script, r"(?:AICPM_|REPORT=)\\$\\{|/run/\\$")


if __name__ == "__main__":
    unittest.main()
