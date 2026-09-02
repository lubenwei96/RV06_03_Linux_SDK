import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WIFI_MAKEFILE = ROOT / "sysdrv/drv_ko/wifi/Makefile"
WIFI_LOADER = ROOT / "sysdrv/drv_ko/wifi/insmod_wifi.sh"
KO_LOADER = ROOT / "sysdrv/drv_ko/insmod_ko.sh"
BOARD_CONFIG = (
    ROOT
    / "project/cfg/BoardConfig_IPC/BoardConfig-SPI_NAND-NONE-RV1106_AICPM-V1.mk"
)
LEGACY_MARKER = "#AIC8800D40\n"
LEGACY_SHA256 = "c418aa082556fac8f1c2a50bb3248063bf8a29af1148803ff8d29da7ab449013"


class SdkIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.makefile = WIFI_MAKEFILE.read_text(encoding="utf-8")
        cls.wifi_loader = WIFI_LOADER.read_text(encoding="utf-8")
        cls.ko_loader = KO_LOADER.read_text(encoding="utf-8")
        cls.board_config = BOARD_CONFIG.read_text(encoding="utf-8")

    def test_01_exact_build_selector_writes_readonly_marker(self):
        branch = re.search(
            r"ifeq \(\$\(RK_ENABLE_WIFI_CHIP\),RTL8822CU_USB\)\n"
            r"(?P<body>.*?)\nendif",
            self.makefile,
            re.S,
        )
        self.assertIsNotNone(branch)
        body = branch.group("body")
        self.assertIn("@$(MAKE) -C rtl8822cu/", body)
        self.assertIn(
            "@printf '%s\\n' RTL8822CU_USB > $(M_OUT_DIR)/wifi_chip_type", body
        )
        self.assertIn("@chmod 0444 $(M_OUT_DIR)/wifi_chip_type", body)

    def test_02_clean_removes_driver_and_marker(self):
        clean = self.makefile.split("build-usb-clean:", 1)[1].split("\nclean:", 1)[0]
        self.assertIn("@$(MAKE) -C rtl8822cu/ clean", clean)
        self.assertIn("@rm -f $(M_OUT_DIR)/wifi_chip_type", clean)

    def test_03_empty_selector_fallback_does_not_expand_license_scope(self):
        fallback = self.makefile.split("\nelse\n\nbuild-usb:", 1)[1].split(
            "\nendif\n\nbuild-sdio-clean:", 1
        )[0]
        self.assertNotIn("rtl8822cu", fallback.lower())
        self.assertNotIn("88x2cu", fallback.lower())

    def test_04_marker_branch_precedes_and_preserves_legacy_logic(self):
        prefix, legacy = self.wifi_loader.split(LEGACY_MARKER, 1)
        self.assertIn("chip_marker=/oem/usr/ko/wifi_chip_type", prefix)
        self.assertIn('if [ -e "$chip_marker" ] || [ -L "$chip_marker" ]; then', prefix)
        self.assertIn('[ -f "$chip_marker" ] && [ -r "$chip_marker" ] || exit 1', prefix)
        self.assertIn("chip_type=$(tr -d '\\000\\r\\n ' < \"$chip_marker\") || exit 1", prefix)
        self.assertIn("RTL8822CU_USB)", prefix)
        self.assertIn("test -s /oem/usr/ko/88x2cu.ko || exit 1", prefix)
        self.assertIn("insmod /oem/usr/ko/88x2cu.ko", prefix)
        self.assertIn("exit $?", prefix)
        # The supplier script historically has no final newline.  Ignore only
        # that POSIX-text normalization while freezing every legacy command.
        actual = hashlib.sha256((LEGACY_MARKER + legacy).rstrip("\n").encode()).hexdigest()
        self.assertEqual(LEGACY_SHA256, actual)

    def test_05_marker_branch_has_no_device_probe_or_network_side_effects(self):
        marker_branch = self.wifi_loader.split("chip_marker=", 1)[1].split("fi\n", 1)[0]
        for forbidden in (
            "0xC82C",
            "0xC812",
            "aic_btusb",
            "ifconfig",
            "wpa_supplicant",
            "/sys/",
        ):
            self.assertNotIn(forbidden, marker_branch)

    def _run_loader_fixture(self, marker_kind, marker_bytes=b"", module=True, insmod_rc=0):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "wifi_chip_type"
            module_path = root / "88x2cu.ko"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            if marker_kind == "file":
                marker.write_bytes(marker_bytes)
            elif marker_kind == "directory":
                marker.mkdir()
            elif marker_kind == "broken_symlink":
                marker.symlink_to(root / "missing-target")
            elif marker_kind != "missing":
                raise AssertionError(marker_kind)
            if module:
                module_path.write_bytes(b"module")

            insmod = bin_dir / "insmod"
            insmod.write_text(f"#!/bin/sh\nexit {insmod_rc}\n", encoding="utf-8")
            insmod.chmod(0o755)

            script_text = self.wifi_loader.replace(
                "/oem/usr/ko/wifi_chip_type", str(marker)
            ).replace("/oem/usr/ko/88x2cu.ko", str(module_path))
            script = root / "insmod_wifi.sh"
            script.write_text(script_text, encoding="utf-8")
            script.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            return subprocess.run(
                ["sh", str(script)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode

    def test_06_damaged_or_unknown_marker_never_falls_back(self):
        fixtures = (
            ("file", b""),
            ("file", b"UNKNOWN\n"),
            ("directory", b""),
            ("broken_symlink", b""),
        )
        for kind, payload in fixtures:
            with self.subTest(kind=kind, payload=payload):
                self.assertNotEqual(0, self._run_loader_fixture(kind, payload))

    def test_07_valid_marker_sanitizes_bytes_and_propagates_insmod_status(self):
        marker = b" RTL8822CU_USB\x00\r\n "
        self.assertEqual(0, self._run_loader_fixture("file", marker, insmod_rc=0))
        self.assertEqual(17, self._run_loader_fixture("file", marker, insmod_rc=17))
        self.assertNotEqual(
            0, self._run_loader_fixture("file", marker, module=False, insmod_rc=0)
        )

    def test_08_background_loader_records_failure_and_kmsg(self):
        expected = (
            "rm -f /run/wifi-load.failed\n"
            "(\n"
            '\t"$(pwd)/insmod_wifi.sh"\n'
            "\twifi_rc=$?\n"
            '\tif [ "$wifi_rc" -ne 0 ]; then\n'
            "\t\tprintf 'exit_code=%s\\n' \"$wifi_rc\" > /run/wifi-load.failed\n"
            "\t\tprintf 'aicpm: Wi-Fi module load failed: exit_code=%s\\n' \"$wifi_rc\" > /dev/kmsg\n"
            "\tfi\n"
            '\texit "$wifi_rc"\n'
            ") &"
        )
        self.assertIn(expected, self.ko_loader)

    def test_09_formal_board_selects_only_rtl8822cu_wifi(self):
        expected = (
            "export RK_ENABLE_WIFI_APP=n",
            "export RK_ENABLE_WIFI=y",
            "export RK_ENABLE_WIFI_CHIP=RTL8822CU_USB",
        )
        for line in expected:
            with self.subTest(line=line):
                self.assertEqual(1, len(re.findall(rf"(?m)^{re.escape(line)}$", self.board_config)))
        self.assertNotRegex(self.board_config, r"(?m)^export RK_ENABLE_BT=y$")
        self.assertNotRegex(
            self.board_config, r"(?m)^export RK_ENABLE_WIFI_CHIP=.*AIC.*$"
        )


if __name__ == "__main__":
    unittest.main()
