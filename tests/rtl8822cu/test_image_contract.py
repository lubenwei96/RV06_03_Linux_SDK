import fnmatch
import hashlib
import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRIVER_MODULE = ROOT / "sysdrv/drv_ko/out/88x2cu.ko"
STAGING_TREES = (
    ROOT / "sysdrv/out/kernel_drv_ko",
    ROOT / "output/out/sysdrv_out/kernel_drv_ko",
    ROOT / "output/out/rootfs_uclibc_rv1106/oem/usr/ko",
)
ROOTFS = ROOT / "output/out/rootfs_uclibc_rv1106"
SAFE_WPA = ROOT / "project/cfg/BoardConfig_IPC/overlay/aicpm-v1/etc/wpa_supplicant.conf"
SOAK = ROOT / "project/cfg/BoardConfig_IPC/overlay/aicpm-v1/usr/sbin/aicpm-wifi-soak"
BOARD = ROOT / "project/cfg/BoardConfig_IPC/BoardConfig-SPI_NAND-NONE-RV1106_AICPM-V1.mk"
KCONFIG = ROOT / "sysdrv/source/objs_kernel/.config"
BSP_BASE = "de6020e1def04073b885389d660caf6d2723231c"
BASELINE_WPA = (
    "sysdrv/tools/board/buildroot/overlay/etc/wpa_supplicant.conf",
    "sysdrv/drv_ko/wifi/ssv6x5x/wpa_supplicant.conf",
)
SECRET_RE = re.compile(
    rb"(?im)^[ \t]*(?:network[ \t]*=[ \t]*\{|(?:ssid|bssid|psk|password)[ \t]*=)"
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exported(text, name):
    matches = re.findall(rf"(?m)^export {re.escape(name)}=(.*)$", text)
    if len(matches) != 1:
        raise AssertionError(f"expected one export for {name}, got {matches!r}")
    return matches[0]


def forbidden_part(part):
    value = part.casefold()
    return (
        value in {"aic_fw", "rtl_bt"}
        or fnmatch.fnmatchcase(value, "aic8800*")
        or fnmatch.fnmatchcase(value, "aic_load_fw*")
        or fnmatch.fnmatchcase(value, "aic_bt*")
        or fnmatch.fnmatchcase(value, "*btusb*")
    )


class ImageContract(unittest.TestCase):
    def test_01_three_staging_modules_and_markers_are_bound(self):
        self.assertTrue(DRIVER_MODULE.is_file())
        expected_module_sha = sha256(DRIVER_MODULE)
        for tree in STAGING_TREES:
            with self.subTest(tree=tree):
                module = tree / "88x2cu.ko"
                marker = tree / "wifi_chip_type"
                self.assertTrue(module.is_file() and module.stat().st_size > 0)
                self.assertEqual(expected_module_sha, sha256(module))
                self.assertEqual(b"RTL8822CU_USB\n", marker.read_bytes())

    def test_02_rootfs_has_fail_closed_loaders(self):
        wifi = (STAGING_TREES[2] / "insmod_wifi.sh").read_text()
        ko = (STAGING_TREES[2] / "insmod_ko.sh").read_text()
        self.assertIn("chip_marker=/oem/usr/ko/wifi_chip_type", wifi)
        self.assertIn('if [ -e "$chip_marker" ] || [ -L "$chip_marker" ]; then', wifi)
        self.assertIn("RTL8822CU_USB)", wifi)
        self.assertIn("/run/wifi-load.failed", ko)
        self.assertIn("aicpm: Wi-Fi module load failed", ko)

    def test_03_no_legacy_aic_or_bt_payload_in_any_path_segment(self):
        for tree in STAGING_TREES:
            self.assertTrue(tree.is_dir(), tree)
            for path in tree.rglob("*"):
                relative = path.relative_to(tree)
                bad = [part for part in relative.parts if forbidden_part(part)]
                self.assertEqual([], bad, f"forbidden payload path: {relative}")

    def test_04_product_wpa_is_exact_and_secret_free(self):
        expected = b"ctrl_interface=/var/run/wpa_supplicant\nupdate_config=0\n"
        self.assertEqual(expected, SAFE_WPA.read_bytes())
        final_wpa = ROOTFS / "etc/wpa_supplicant.conf"
        self.assertEqual(expected, final_wpa.read_bytes())
        self.assertIsNone(SECRET_RE.search(final_wpa.read_bytes()))
        for mutant in (
            b"network={\nssid=x\npsk=x\n}",
            b" network = {\n ssid = x\n bssid = x\n password = x",
        ):
            self.assertIsNotNone(SECRET_RE.search(mutant))

    def test_05_sdk_example_wpa_files_remain_byte_unmodified(self):
        for path in BASELINE_WPA:
            with self.subTest(path=path):
                committed = subprocess.run(
                    ["git", "diff", "--quiet", f"{BSP_BASE}..HEAD", "--", path],
                    cwd=ROOT,
                    check=False,
                )
                working = subprocess.run(
                    ["git", "diff", "--quiet", "--", path], cwd=ROOT, check=False
                )
                self.assertEqual(0, committed.returncode)
                self.assertEqual(0, working.returncode)

    def test_06_required_wifi_userland_is_executable(self):
        for tool in ("iw", "wpa_supplicant", "wpa_cli", "wpa_passphrase", "dhcpcd"):
            candidates = [ROOTFS / prefix / tool for prefix in ("usr/sbin", "usr/bin", "sbin", "bin")]
            self.assertTrue(any(p.is_file() and os.access(p, os.X_OK) for p in candidates), tool)

    def test_07_soak_script_has_fixed_nonsecret_24h_contract(self):
        self.assertTrue(SOAK.is_file() and os.access(SOAK, os.X_OK))
        subprocess.run(["sh", "-n", str(SOAK)], check=True)
        text = SOAK.read_text()
        self.assertIn("AICPM_WIFI_SOAK_SAMPLES=1440", text)
        self.assertIn("AICPM_WIFI_SOAK_INTERVAL=60", text)
        self.assertIn("INTERFACE GATEWAY OUTPUT_DIR", text)
        self.assertNotRegex(text.casefold(), r"\bssid\b|\bbssid\b")
        rootfs_soak = ROOTFS / "usr/sbin/aicpm-wifi-soak"
        self.assertEqual(SOAK.read_bytes(), rootfs_soak.read_bytes())
        self.assertTrue(os.access(rootfs_soak, os.X_OK))

    def test_08_bluetooth_is_disabled_in_final_kernel_config(self):
        config = KCONFIG.read_text()
        self.assertIn("# CONFIG_BT is not set\n", config)
        self.assertIsNone(re.search(r"(?m)^CONFIG_BT(?:=|_)", config))

    def test_09_formal_board_identity_and_safety_gates_remain_fixed(self):
        board = BOARD.read_text()
        expected = {
            "RK_CHIP": "rv1106",
            "RK_BOOT_MEDIUM": "spi_nand",
            "RK_KERNEL_DTS": "rv1106g-aicpm-v1.dts",
            "RK_POST_OVERLAY": "aicpm-v1",
            "RK_ENABLE_WIFI": "y",
            "RK_ENABLE_WIFI_CHIP": "RTL8822CU_USB",
        }
        for key, value in expected.items():
            self.assertEqual(value, exported(board, key))


if __name__ == "__main__":
    unittest.main()
