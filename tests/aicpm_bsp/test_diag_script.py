import hashlib
import re
import shlex
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

    @staticmethod
    def _safety_gate(script):
        if not script.startswith("#!/bin/sh\n") or "\r" in script:
            raise AssertionError("production script must use the fixed POSIX shebang and LF")
        body = script.split("\n", 1)[1]
        forbidden = re.compile(
            r"(?im)(?:^|[;|&()\s])(?:insmod|modprobe|rmmod|gpioset|gpioget|gpioinfo|"
            r"pwm|tee|dd|cp|install|iw|rfkill|wpa_supplicant|hostapd|udhcpc|dhclient|"
            r"ifconfig|reboot|shutdown|poweroff)(?=$|[;|&()\s])|"
            r"(?:^|[;|&()\s])(?:busybox|command|env|exec|sh\s+-c)(?=$|[;|&()\s])|"
            r"ip\s+(?:addr|route|link\s+set)|"
            r"(?:^|[;|&()\s])(?:printenv|export\s+-p|set)(?=$|[;|&()\s])|"
            r"(?:^|[;\n])\s*[A-Za-z_][A-Za-z0-9_]*=(?:insmod|modprobe|rmmod|"
            r"gpioset|gpioget|gpioinfo|pwm|tee|dd|cp|install|iw|rfkill)(?=$|[;\s])|"
            r"(?:^|[;\n])\s*\"\$[A-Za-z_][A-Za-z0-9_]*\"|"
            r"/dev/aicpm-l9110s|(?:cat\s+)?/etc/shadow|/proc/self/environ|"
            r"/etc/wpa_supplicant|/(?:data|oem)/[^\n]*(?:mqtt|cloud|device|private)|"
            r"(?:^|[;\n])\s*target=/(?:sys|proc|dev)/|>\s*['\"]?\$\{?target|"
            r"(?:tee|dd|cp|install|mv)\b[^\n]*(?:/(?:sys|proc|dev)/|\$\{?target)|"
            r">\s*/(?:sys|proc|dev)/|sed\s+-i"
        )
        lexer = shlex.shlex(script, posix=True, punctuation_chars="();<>|&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError as error:
            raise AssertionError("production script is not safely tokenizable") from error
        allowed_absolute_tokens = {
            "/proc/cmdline",
            "/proc/mtd",
            "/proc/partitions",
            "/proc/meminfo",
            "/sys/class/mtd",
            "/sys/class/mmc_host",
            "/sys/class/net",
            "/sys/class/pwm",
        }
        reviewed_absolute_contexts = {
            "/proc/cmdline": "\tcollect_file read_proc_cmdline /proc/cmdline || return 1",
            "/proc/mtd": "\tcollect_file read_proc_mtd /proc/mtd || return 1",
            "/proc/partitions": "\tcollect_file read_proc_partitions /proc/partitions || return 1",
            "/proc/meminfo": "\tcollect_file read_proc_meminfo /proc/meminfo || return 1",
            "/sys/class/mtd": "\tcollect_directory scan_sys_class_mtd /sys/class/mtd || return 1",
            "/sys/class/mmc_host": "\tcollect_directory scan_sys_class_mmc_host /sys/class/mmc_host || return 1",
            "/sys/class/net": "\tcollect_directory scan_sys_class_net /sys/class/net || return 1",
            "/sys/class/pwm": "\tcollect_directory scan_sys_class_pwm /sys/class/pwm || return 1",
        }
        allowed_absolute_assignments = {
            "REPORT=/run/aicpm-firstboard-report.txt": "REPORT=/run/aicpm-firstboard-report.txt",
            "USB_ROOT=/sys/bus/usb/devices": "USB_ROOT=/sys/bus/usb/devices",
        }
        reviewed_relative_contexts = {
            "#!/bin/sh": ("#!/bin/sh", 1, {"#!/bin/sh"}),
            "$USB_ROOT/*": ('"$USB_ROOT"/*', 1, {"\tfor device in \"$USB_ROOT\"/*; do"}),
            "$device/idVendor": (
                "$device/idVendor",
                2,
                {
                    "\t\t[ -f \"$device/idVendor\" ] || continue",
                    "\t\tcollect_command usb_vendor cat \"$device/idVendor\" || return 1",
                },
            ),
            "$device/idProduct": (
                "$device/idProduct",
                1,
                {"\t\tcollect_command usb_product_id cat \"$device/idProduct\" || return 1"},
            ),
            "$device/product": (
                "$device/product",
                1,
                {"\t\tcollect_command usb_product cat \"$device/product\" || return 1"},
            ),
        }
        reviewed_redactor_sha256 = (
            "4d994fc5f3d6ea3c3706c725191661291a3ae4b7ada5fedc56bd8cfc14e80a54"
        )
        lines = set(script.splitlines())
        for token, expected_line in reviewed_absolute_contexts.items():
            if script.count(token) != 1 or expected_line not in lines:
                raise AssertionError("absolute data path used outside reviewed collector")
        for token, expected_line in allowed_absolute_assignments.items():
            if script.count(token) != 1 or expected_line not in lines:
                raise AssertionError("absolute assignment used outside reviewed declaration")
        for token, (needle, expected_count, expected_lines) in reviewed_relative_contexts.items():
            if script.count(needle) != expected_count or not expected_lines.issubset(lines):
                raise AssertionError("relative slash token used outside reviewed collector")
        for token in tokens:
            if "/" not in token:
                continue
            if token in allowed_absolute_tokens or token in allowed_absolute_assignments:
                continue
            if token in reviewed_relative_contexts:
                continue
            if hashlib.sha256(token.encode("utf-8")).hexdigest() == reviewed_redactor_sha256:
                continue
            raise AssertionError("unapproved slash token may bypass the fake PATH")
        if forbidden.search(script):
            raise AssertionError("unsafe production command")
        reviewed_script_sha256 = (
            "c0bced971b9119710d5e42d51903c7cc5315556a864b5956be5414659b321135"
        )
        if hashlib.sha256(script.encode("utf-8")).hexdigest() != reviewed_script_sha256:
            raise AssertionError("production diagnostic script differs from reviewed bytes")

    @classmethod
    def setUpClass(cls):
        script = read_text(SCRIPT)
        cls._safety_gate(script)
        cls.assertIn(cls, "REPORT=/run/aicpm-firstboard-report.txt", script)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake-bin"
        self.fake.mkdir()
        self.report = self.root / "run" / "aicpm-firstboard-report.txt"
        self.report.parent.mkdir()
        self.usb = self.root / "usb"
        self.usb.mkdir()
        self.calls = self.root / "calls.log"
        self.chmod_state = self.root / "chmod.state"
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
        self._cmd("awk", 'exec /usr/bin/awk "$@"')
        self._cmd("mktemp", 'exec /usr/bin/mktemp "$@"')
        self._cmd("mv", 'printf "mv\\n" >> "$FAKE_LOG"; exec /bin/mv "$@"')
        self._cmd("chmod", 'exec /bin/chmod "$@"')
        self._cmd("rm", 'exec /bin/rm "$@"')
        self._cmd("logger", 'printf "logger:%s\\n" "$*" >&2')

    def _copy(self):
        source = (ROOT / SCRIPT).read_text(encoding="utf-8")
        fixed = "REPORT=/run/aicpm-firstboard-report.txt"
        self.assertIn(fixed, source)
        self.assertEqual(source.count("USB_ROOT=/sys/bus/usb/devices"), 1)
        source = source.replace("USB_ROOT=/sys/bus/usb/devices", f"USB_ROOT={self.usb}")
        copy = self.root / "S30aicpm-firstboard-diag"
        copy.write_text(source.replace(fixed, f"REPORT={self.report}"), encoding="utf-8")
        copy.chmod(0o755)
        return copy

    def _run_copy(self, copy, action="start"):
        return subprocess.run(
            [str(copy), action], text=True, capture_output=True, check=False,
            env={"PATH": str(self.fake), "FAKE_LOG": str(self.calls), "CHMOD_STATE": str(self.chmod_state), "LC_ALL": "C"},
        )

    def _run(self, action="start"):
        return self._run_copy(self._copy(), action)

    def _temps(self):
        return list(self.report.parent.glob("aicpm-firstboard-report.txt.tmp.*"))

    def _assert_fatal_preserves_complete_report(self, command, body):
        self.report.write_text("old-complete\n", encoding="utf-8")
        self.report.chmod(0o600)
        self.chmod_state.unlink(missing_ok=True)
        self._cmd(command, body)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report.read_text(encoding="utf-8"), "old-complete\n")
        self.assertEqual(self._temps(), [])
        self.assertIn("report collection internal failure", result.stderr)
        self.assertIn("logger:", result.stderr)

    def _assert_overlay_pipeline(self, build):
        post = build[build.index("function post_overlay()"):build.index("\n}\n", build.index("function post_overlay()"))]
        firmware = build[build.index("function build_firmware()"):build.index("\n}\n", build.index("function build_firmware()"))]
        rootfs = build[build.index("function build_rootfs()"):build.index("\n}\n", build.index("function build_rootfs()"))]
        mkimg = build[build.index("function build_mkimg()"):build.index("\n}\n", build.index("function build_mkimg()"))]
        self.assertIn("overlay/$RK_POST_OVERLAY/*", post)
        self.assertIn("$RK_PROJECT_PACKAGE_ROOTFS_DIR/", post)
        self.assertIn("--chmod=u=rwX,go=rX", post)
        self.assertNotIn("post_overlay", rootfs)
        self.assertLess(firmware.index("__PACKAGE_ROOTFS"), firmware.index("post_overlay"))
        self.assertLess(firmware.index("post_overlay"), firmware.index("build_mkimg $GLOBAL_ROOT_FILESYSTEM_NAME $RK_PROJECT_PACKAGE_ROOTFS_DIR"))
        self.assertIn("src=$2", mkimg)
        self.assertIn("__RELEASE_FILESYSTEM_FILES $src", mkimg)
        self.assertIn("$RK_PROJECT_TOOLS_MKFS_EXT4 $src $dst", mkimg)
        self.assertEqual(
            exported(read_text(BOARD), "RK_PARTITION_FS_TYPE_CFG"),
            "rootfs@IGNORE@ubifs",
        )
        self.assertIn("$RK_PROJECT_TOOLS_MKFS_UBIFS $src $(dirname $dst) $part_size $part_name $fs_type $RK_UBIFS_COMP", mkimg)

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
        self.assertIn("AICPM_DIAG_REDACTED_LINE", report)
        self.assertIn("AICPM_DIAG_USB_EMPTY", report)
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

    def test_each_readonly_collector_emits_its_own_fixed_failure_marker(self):
        device = self.usb / "1-1"
        device.mkdir()
        for name in ("idVendor", "idProduct", "product"):
            (device / name).write_text("fixture\n", encoding="utf-8")
        cases = (
            ("cat", "/proc/cmdline", "read_proc_cmdline"),
            ("cat", "/proc/mtd", "read_proc_mtd"),
            ("cat", "/proc/partitions", "read_proc_partitions"),
            ("cat", "/proc/meminfo", "read_proc_meminfo"),
            ("date", "-Iseconds", "date"),
            ("uname", "-a", "uname"),
            ("find", "/sys/class/mtd", "scan_sys_class_mtd"),
            ("find", "/sys/class/mmc_host", "scan_sys_class_mmc_host"),
            ("find", "/sys/class/net", "scan_sys_class_net"),
            ("find", "/sys/class/pwm", "scan_sys_class_pwm"),
            ("basename", str(device), "usb_name"),
            ("cat", str(device / "idVendor"), "usb_vendor"),
            ("cat", str(device / "idProduct"), "usb_product_id"),
            ("cat", str(device / "product"), "usb_product"),
        )
        for command, path, marker in cases:
            with self.subTest(marker=marker):
                self._defaults()
                self._cmd(command, f'if [ "$1" = "{path}" ]; then exit 29; fi; printf "ok\\n"')
                result = self._run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    f"AICPM_DIAG_ERROR {marker} rc=29",
                    self.report.read_text(encoding="utf-8"),
                )
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

    def test_missing_usb_root_has_a_marker_and_never_reads_host_sysfs(self):
        self.usb.rmdir()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("AICPM_DIAG_ERROR scan_usb rc=1", report)
        self.assertNotIn("/sys/bus/usb/devices", report)

    def test_unreadable_usb_root_has_a_marker(self):
        self.usb.chmod(0)
        try:
            result = self._run()
        finally:
            self.usb.chmod(0o700)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "AICPM_DIAG_ERROR scan_usb rc=1",
            self.report.read_text(encoding="utf-8"),
        )

    def test_usb_device_enumeration_collects_name_vendor_product_and_id(self):
        device = self.usb / "1-1"
        device.mkdir()
        for name in ("idVendor", "idProduct", "product"):
            (device / name).write_text("fixture\n", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("1-1", report)
        for name in ("idVendor", "idProduct", "product"):
            self.assertIn(f"read:{device / name}", report)
        self.assertNotIn("AICPM_DIAG_USB_EMPTY", report)

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

    def test_internal_tail_redactor_and_chmod_failures_do_not_publish(self):
        self._assert_fatal_preserves_complete_report("awk", "exit 42")
        self._defaults()
        self._assert_fatal_preserves_complete_report("chmod", "exit 43")
        self._defaults()
        self._assert_fatal_preserves_complete_report(
            "chmod",
            'if [ ! -e "$CHMOD_STATE" ]; then : > "$CHMOD_STATE"; exec /bin/chmod "$@"; fi; exit 43',
        )

    def test_tail_failure_is_a_degraded_readonly_report(self):
        self._cmd("tail", "exit 41")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report.read_text(encoding="utf-8")
        self.assertIn("AICPM_DIAG_ERROR dmesg_tail rc=41", report)
        self.assertTrue(report.startswith("AICPM_FIRSTBOARD_REPORT_V1"))
        self.assertEqual(self._temps(), [])
        self.assertIn("collection failure: dmesg_tail rc=41", result.stderr)

    def test_checked_append_mutation_cannot_publish_a_partial_report(self):
        self.report.write_text("old-complete\n", encoding="utf-8")
        copy = self._copy()
        original = copy.read_text(encoding="utf-8")
        self.assertIn('printf \'%s\\n\' "$1" >>"$TMP" || { fatal; return 1; }', original)
        copy.write_text(
            original.replace('printf \'%s\\n\' "$1" >>"$TMP" || { fatal; return 1; }', "fatal; return 1", 1),
            encoding="utf-8",
        )
        result = self._run_copy(copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report.read_text(encoding="utf-8"), "old-complete\n")
        self.assertEqual(self._temps(), [])
        self.assertIn("report collection internal failure", result.stderr)

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
        self._assert_overlay_pipeline(build)
        subprocess.run(["git", "diff", "--quiet", BASE_COMMIT, "--", BASELINE_WPA], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    def test_static_safety_rejects_direct_and_wrapper_bypass_mutants(self):
        script = read_text(SCRIPT)
        self._safety_gate(script)
        for mutant in (
            "/sbin/modprobe rtl8822cu", "busybox modprobe rtl8822cu", "command modprobe rtl8822cu",
            "env modprobe rtl8822cu", "gpioset gpiochip0 1=1", "echo 1 > /sys/class/pwm/pwmchip0/export",
            "cat /dev/aicpm-l9110s0", "ip link set eth0 up", "wpa_supplicant -i wlan0", "reboot",
            "runner=modprobe; \"$runner\" rtl8822cu",
        ):
            with self.subTest(mutant=mutant):
                with self.assertRaises(AssertionError):
                    self._safety_gate(f"#!/bin/sh\n{mutant}\n")
        self.assertIn("REPORT=/run/aicpm-firstboard-report.txt", script)
        self.assertNotRegex(script, r"(?:AICPM_|REPORT=)\\$\\{|/run/\\$")


    def test_review_hardening_contract_is_present(self):
        script = read_text(SCRIPT)
        for token in (
            "AICPM_DIAG_REDACTED_LINE", "USB_ROOT=/sys/bus/usb/devices",
            "append_line", "fatal", "PRIVATE KEY", "AICPM_DIAG_USB_EMPTY",
        ):
            self.assertIn(token, script)
        self.assertNotIn("/usr/", script)

    def test_source_modes_and_precise_overlay_sequence(self):
        self.assertEqual(stat.S_IMODE((ROOT / SCRIPT).stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((ROOT / SAFE_WPA).stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(Path(__file__).stat().st_mode), 0o644)
        build = read_text(BUILD)
        self._assert_overlay_pipeline(build)

    def test_overlay_pipeline_rejects_order_target_mode_and_dataflow_mutations(self):
        build = read_text(BUILD)
        moved = build.replace(
            "\tpost_overlay\n\n\tif [ -n \"$GLOBAL_INITRAMFS_BOOT_NAME\"",
            "\tif [ -n \"$GLOBAL_INITRAMFS_BOOT_NAME\"",
            1,
        )
        moved = moved.replace(
            "\tbuild_mkimg $GLOBAL_ROOT_FILESYSTEM_NAME $RK_PROJECT_PACKAGE_ROOTFS_DIR\n",
            "\tbuild_mkimg $GLOBAL_ROOT_FILESYSTEM_NAME $RK_PROJECT_PACKAGE_ROOTFS_DIR\n\tpost_overlay\n",
            1,
        )
        mutations = (
            ("$tmp_path/overlay/$RK_POST_OVERLAY/* $RK_PROJECT_PACKAGE_ROOTFS_DIR/", "$tmp_path/overlay/$RK_POST_OVERLAY/* $RK_PROJECT_PACKAGE_OEM_DIR/"),
            ("--chmod=u=rwX,go=rX", ""),
            ("src=$2", "src=$1"),
        )
        with self.assertRaises((AssertionError, ValueError)):
            self._assert_overlay_pipeline(moved)
        for old, new in mutations:
            with self.subTest(old=old):
                self.assertIn(old, build)
                mutated = build.replace(old, new, 1)
                with self.assertRaises((AssertionError, ValueError)):
                    self._assert_overlay_pipeline(mutated)

    def test_deny_scanner_rejects_review_mutants(self):
        script = read_text(SCRIPT)
        self._safety_gate(script)
        for mutant in (
            "/usr/sbin/modprobe x", "/usr/bin/tee /sys/x", "/usr/local/sbin/modprobe x",
            "busybox modprobe x", "command modprobe x", "env -i modprobe x", "exec modprobe x",
            'sh -c "modprobe x"', "cmd=tee", 'runner=/usr/bin/tee; "$runner" /sys/x',
            "tee /sys/x", "dd of=/dev/x", "cp x /proc/x", "install x /dev/x", "mv x /sys/x",
            "target=/sys/x; echo 1 > \"$target\"", "ip addr add x", "ip route add x", "ip link set eth0 up",
            "iw dev", "rfkill block all", "printenv", "env", "export -p", "set", "cat /etc/shadow",
            "cat /proc/self/environ", "cat /etc/wpa_supplicant.conf", "cat /data/mqtt.conf", "cat /oem/cloud.json",
            "sed -i x /sys/x",
        ):
            with self.assertRaises(AssertionError, msg=mutant):
                self._safety_gate(f"#!/bin/sh\n{mutant}\n")

    def test_redacts_mixed_secret_formats_and_keeps_normal_diagnostics(self):
        self._cmd("dmesg", "printf '%s\\n' 'password=two words' 'token: Bearer alpha beta' '\"password\": \"json secret\"' 'clientSecret=camel secret' 'privateKey=private key value' 'wifiPsk=wireless secret' 'wifiPassword: quoted password' 'passphrase=phrase with spaces' 'mqtt_pass=broker secret' 'pwd=short secret' 'authToken=opaque-A7' 'refreshToken: opaque-R8' 'sessionToken=opaque-S9' 'preSharedKey=opaque-P0' '-----BEGIN OPENSSH PRIVATE KEY-----' 'high-entropy-private-body' '-----END OPENSSH PRIVATE KEY-----' 'devices online' 'capability=usb-host' 'api version=1'")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report.read_text(encoding="utf-8")
        for secret in ("two words", "alpha beta", "json secret", "camel secret", "private key value", "wireless secret", "quoted password", "phrase with spaces", "broker secret", "short secret", "opaque-A7", "opaque-R8", "opaque-S9", "opaque-P0", "high-entropy-private-body"):
            self.assertNotIn(secret, report)
        self.assertGreaterEqual(report.count("AICPM_DIAG_REDACTED_LINE"), 13)
        for normal in ("devices online", "capability=usb-host", "api version=1"):
            self.assertIn(normal, report)

    def test_global_safety_gate_rejects_absolute_external_commands(self):
        source = read_text(SCRIPT)
        self._safety_gate(source)
        anchor = "\tcollect_command date date -Iseconds || return 1"
        self.assertIn(anchor, source)
        for mutant in (
            "/usr/bin/tee /sys/x",
            "/bin/cat /proc/cmdline",
            "/bin/busybox dmesg",
            "/usr/bin/env",
            'runner=/bin/cat; "$runner" /proc/cmdline',
            "/opt/vendor/bin/modprobe rtl8822cu",
            "/tmp/tee /sys/x",
            "/busybox-helper /proc/cmdline",
            'runner=/opt/vendor/bin/helper; "$runner" /proc/cmdline',
            "! /opt/vendor/bin/modprobe rtl8822cu",
            "if false; then :; else /opt/vendor/bin/modprobe rtl8822cu; fi",
            "{ /opt/vendor/bin/modprobe rtl8822cu; }",
            "time /opt/vendor/bin/modprobe rtl8822cu",
            "if collect_command injected /opt/vendor/bin/helper; then :; fi",
            "true || collect_command injected /opt/vendor/bin/helper",
            "`/opt/vendor/bin/helper`",
            "v=x; : ${v#x}; /opt/vendor/bin/modprobe rtl8822cu",
            "chmod 0777 /sys/class/pwm",
            "rm -f /proc/cmdline",
            ". /proc/cmdline",
        ):
            with self.subTest(mutant=mutant):
                mutated = source.replace(
                    anchor, anchor + "\n\t" + mutant, 1
                )
                self.assertNotEqual(mutated, source)
                with self.assertRaises(AssertionError):
                    self._safety_gate(mutated)

    def test_safety_gate_rejects_embedded_absolute_paths_and_context_replacement(self):
        source = read_text(SCRIPT)
        collector = "\tcollect_file read_proc_cmdline /proc/cmdline || return 1"
        self.assertIn(collector, source)
        date_line = "\tcollect_command date date -Iseconds || return 1"
        self.assertIn(date_line, source)
        report_line = "REPORT=/run/aicpm-firstboard-report.txt"
        self.assertIn(report_line, source)
        mutants = (
            source.replace(
                collector,
                "\tsafe-fake --path=/proc/cmdline || return 1",
                1,
            ),
            source.replace(
                date_line,
                date_line + "\n\tawk -f/opt/vendor/payload.awk \"$TMP\"",
                1,
            ),
            source.replace(
                date_line,
                date_line + "\n\tEMPTY=; PATH=$EMPTY/opt/vendor/bin:$PATH; helper",
                1,
            ),
            source.replace(
                report_line,
                "safe-fake --report=REPORT=/run/aicpm-firstboard-report.txt",
                1,
            ),
            source.replace(
                date_line,
                date_line + "\n\tslash=${REPORT%run*}"
                + "\n\thelper=${slash}opt${slash}vendor${slash}bin${slash}helper"
                + "\n\tcollect_command injected \"$helper\" || return 1",
                1,
            ),
            source.replace(
                date_line,
                date_line + "\n\tslash=${REPORT%run*}"
                + "\n\tPATH=${slash}opt${slash}vendor${slash}bin:$PATH; helper",
                1,
            ),
            source.replace(
                date_line,
                date_line + "\n\trm -f \"$USB_ROOT\"/* || true",
                1,
            ),
            source.replace(
                date_line,
                date_line + "\n\tchmod 0777 \"$USB_ROOT\"/* || true",
                1,
            ),
        )
        for mutant in mutants:
            with self.subTest(mutant=mutant):
                with self.assertRaises(AssertionError):
                    self._safety_gate(mutant)

    def test_ubifs_release_sink_mutation_is_rejected(self):
        board = read_text(BOARD)
        self.assertEqual(
            exported(board, "RK_PARTITION_FS_TYPE_CFG"),
            "rootfs@IGNORE@ubifs",
        )
        build = read_text(BUILD)
        self._assert_overlay_pipeline(build)
        sink = "$RK_PROJECT_TOOLS_MKFS_UBIFS $src $(dirname $dst) $part_size $part_name $fs_type $RK_UBIFS_COMP"
        self.assertIn(sink, build)
        mutated = build.replace(
            sink,
            sink.replace("$src", "$RK_PROJECT_PACKAGE_OEM_DIR", 1),
            1,
        )
        with self.assertRaises((AssertionError, ValueError)):
            self._assert_overlay_pipeline(mutated)


if __name__ == "__main__":
    unittest.main()
