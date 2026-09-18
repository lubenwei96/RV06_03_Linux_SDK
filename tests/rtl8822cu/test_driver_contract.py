import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "sysdrv/drv_ko/wifi/rtl8822cu"
EXPECTED_TREE_SHA256 = "15ccf3ac98da402e9f31587f69ebae6728405392acca162a04cc488a07376e46"


class DriverContract(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DRIVER.is_dir(), "formal rtl8822cu source directory is missing")

    def test_01_complete_rtl8822cu_source_tree(self):
        self.assertTrue(any((DRIVER / "hal/rtl8822c").rglob("*.c")))
        for path in ("Makefile", "core", "include", "os_dep/linux/usb_intf.c", "COPYING"):
            self.assertTrue((DRIVER / path).exists(), path)

    def test_02_source_manifest_is_bound_to_admitted_tree(self):
        manifest = json.loads((DRIVER / "SOURCE-MANIFEST.json").read_text())
        self.assertEqual(manifest["source_tree_sha256"], EXPECTED_TREE_SHA256)
        self.assertEqual(manifest["archive_sha256"], "b665da126a6a306d89653dcf2e964154e082be5e817230b97fce6ebbc285d47e")
        self.assertEqual(manifest["binary_blob_candidates"], [])

    def test_03_product_feature_switches_are_exact(self):
        makefile = (DRIVER / "Makefile").read_text(errors="strict")
        expected = {
            "CONFIG_RTL8822C": "y", "CONFIG_RTL8188F": "n",
            "CONFIG_USB_HCI": "y", "CONFIG_SDIO_HCI": "n",
            "CONFIG_PCI_HCI": "n", "CONFIG_BT_COEXIST": "n",
            "CONFIG_MP_INCLUDED": "n", "CONFIG_USB_AUTOSUSPEND": "n",
        }
        for key, value in expected.items():
            self.assertRegex(makefile, rf"(?m)^{key}\s*=\s*{value}\s*$", key)

    def test_04_module_name_is_88x2cu(self):
        makefile = (DRIVER / "Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^MODULE_NAME\s*:?=\s*88x2cu\s*$")
        self.assertRegex(makefile, r"(?m)^obj-m\s*\+=\s*\$\(MODULE_NAME\)\.o\s*$")

    def test_05_sdk_kernel_object_build_contract(self):
        makefile = (DRIVER / "Makefile").read_text()
        for token in ("all: modules", "modules:", "-C $(KERNEL_DIR)", "M=$(CURDIR)",
                      "O=$(WIFI_BUILD_KERNEL_OBJ_DIR)", "$(CURDIR)/88x2cu.ko",
                      "$(M_OUT_DIR)/88x2cu.ko", "clean:"):
            self.assertIn(token, makefile)

    def test_06_no_host_install_download_or_parallel_escape(self):
        makefile = (DRIVER / "Makefile").read_text()
        for pattern in (r"-j(?:8|12)\b", r"\bsudo\b", r"/lib/modules", r"\b(?:curl|wget)\b"):
            self.assertNotRegex(makefile, pattern)

    def test_07_no_prebuilt_or_executable_payload_is_tracked(self):
        names = subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--", str(DRIVER.relative_to(ROOT))]
        ).decode().split("\0")
        for name in filter(None, names):
            path = ROOT / name
            self.assertNotIn(path.suffix.lower(), {".ko", ".o", ".a", ".so", ".exe"}, name)
            if path.is_file():
                head = path.read_bytes()[:4]
                self.assertFalse(head.startswith(b"\x7fELF") or head.startswith(b"MZ"), name)

    def test_08_usb_id_table_remains_vendor_owned(self):
        text = (DRIVER / "os_dep/linux/usb_intf.c").read_text()
        self.assertIn("0xC82C", text)
        self.assertIn("0xC812", text)
        self.assertNotIn("AICPM", text)

    def test_09_imports_rockchip_vfs_internal_namespace(self):
        text = (DRIVER / "os_dep/linux/os_intfs.c").read_text()
        namespace_import = (
            "MODULE_IMPORT_NS("
            "VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver);"
        )
        self.assertEqual(1, text.count(namespace_import))


if __name__ == "__main__":
    unittest.main()
