import hashlib
import importlib.util
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/rtl8822cu_admit.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("rtl8822cu_admit", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_files(with_license=True):
    files = {
        "vendor/Makefile": b"obj-m += 88x2cu.o\n",
        "vendor/core/core.c": b"/* GPL-2.0 */\n",
        "vendor/hal/rtl8822c/hal.c": b"/* GPL-2.0 */\n",
        "vendor/include/driver.h": b"#pragma once\n",
        "vendor/os_dep/linux/usb_intf.c": b"/* GPL-2.0 */\n",
    }
    if with_license:
        files["vendor/COPYING"] = b"GNU GENERAL PUBLIC LICENSE Version 2\n"
    return files


def write_zip(path, files, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


def write_tar(path, files, special=None):
    import io
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        for info in special or ():
            archive.addfile(info)


class SourceAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "source.zip"
        self.stage = self.root / "stage"
        self.manifest = self.root / "manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def admit(self):
        return load_tool().admit_archive(self.archive, self.stage, self.manifest)

    def test_01_valid_minimal_tree_passes(self):
        write_zip(self.archive, source_files())
        self.admit()
        data = json.loads(self.manifest.read_text())
        self.assertEqual(data["file_count"], 6)
        self.assertEqual(data["binary_blob_candidates"], [])
        self.assertEqual(data["license_files"], ["COPYING"])

    def test_02_missing_rtl8822c_is_rejected(self):
        files = source_files()
        del files["vendor/hal/rtl8822c/hal.c"]
        write_zip(self.archive, files)
        with self.assertRaisesRegex(ValueError, "hal/rtl8822c"):
            self.admit()

    def test_03_missing_license_is_rejected(self):
        write_zip(self.archive, source_files(False))
        with self.assertRaisesRegex(ValueError, "LICENSE|COPYING|license"):
            self.admit()

    def test_04_zip_path_traversal_is_rejected(self):
        files = source_files()
        files["../escape"] = b"x"
        write_zip(self.archive, files)
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            self.admit()
        self.assertFalse(self.stage.exists())

    def test_05_tar_links_and_special_nodes_are_rejected(self):
        self.archive = self.root / "source.tar"
        for kind in ("symlink", "hardlink", "fifo", "device"):
            special = tarfile.TarInfo("vendor/bad")
            if kind == "symlink":
                special.type, special.linkname = tarfile.SYMTYPE, "Makefile"
            elif kind == "hardlink":
                special.type, special.linkname = tarfile.LNKTYPE, "vendor/Makefile"
            elif kind == "fifo":
                special.type = tarfile.FIFOTYPE
            else:
                special.type = tarfile.CHRTYPE
            write_tar(self.archive, source_files(), [special])
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "link|special"):
                self.admit()

    def test_06_prebuilt_and_executable_magic_are_rejected(self):
        for name, data in (("bad.ko", b"x"), ("bad.o", b"x"), ("bad.a", b"x"),
                           ("bad.exe", b"x"), ("bad.txt", b"\x7fELFrest"),
                           ("bad.c", b"MZrest")):
            files = source_files()
            files["vendor/" + name] = data
            write_zip(self.archive, files)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "binary|prebuilt"):
                self.admit()

    def test_07_multiple_source_roots_are_rejected(self):
        files = source_files()
        files.update({name.replace("vendor/", "other/"): data for name, data in source_files().items()})
        write_zip(self.archive, files)
        with self.assertRaisesRegex(ValueError, "source root"):
            self.admit()

    def test_08_hash_size_and_tree_hash_are_recorded(self):
        write_zip(self.archive, source_files())
        self.admit()
        data = json.loads(self.manifest.read_text())
        self.assertEqual(data["archive_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(data["archive_size_bytes"], self.archive.stat().st_size)
        self.assertRegex(data["source_tree_sha256"], r"^[0-9a-f]{64}$")

    def test_09_existing_staging_is_not_overwritten(self):
        write_zip(self.archive, source_files())
        self.stage.mkdir()
        marker = self.stage / "keep"
        marker.write_text("keep")
        with self.assertRaisesRegex(FileExistsError, "staging"):
            self.admit()
        self.assertEqual(marker.read_text(), "keep")

    def test_10_failure_leaves_no_staging(self):
        write_zip(self.archive, source_files(False))
        with self.assertRaises(ValueError):
            self.admit()
        self.assertFalse(self.stage.exists())
        self.assertFalse(self.manifest.exists())

    def test_11_archive_size_limit_is_checked_before_open(self):
        write_zip(self.archive, source_files())
        tool = load_tool()
        with mock.patch.object(tool, "MAX_ARCHIVE_BYTES", self.archive.stat().st_size - 1), self.assertRaisesRegex(ValueError, "archive size"):
            tool.admit_archive(self.archive, self.stage, self.manifest)

    def test_12_member_count_limit_is_checked(self):
        write_zip(self.archive, source_files())
        tool = load_tool()
        with mock.patch.object(tool, "MAX_MEMBERS", 2), self.assertRaisesRegex(ValueError, "member count"):
            tool.admit_archive(self.archive, self.stage, self.manifest)

    def test_13_single_member_limit_is_checked(self):
        files = source_files()
        files["vendor/large.c"] = b"x" * 2048
        write_zip(self.archive, files)
        tool = load_tool()
        with mock.patch.object(tool, "MAX_MEMBER_BYTES", 1024), self.assertRaisesRegex(ValueError, "member size"):
            tool.admit_archive(self.archive, self.stage, self.manifest)

    def test_14_expanded_size_limit_is_checked(self):
        write_zip(self.archive, source_files())
        tool = load_tool()
        with mock.patch.object(tool, "MAX_EXPANDED_BYTES", 10), self.assertRaisesRegex(ValueError, "expanded size"):
            tool.admit_archive(self.archive, self.stage, self.manifest)

    def test_15_compression_ratio_limit_is_checked(self):
        files = source_files()
        files["vendor/compressible.c"] = b"A" * 20000
        write_zip(self.archive, files)
        tool = load_tool()
        with mock.patch.object(tool, "MAX_COMPRESSION_RATIO", 2), self.assertRaisesRegex(ValueError, "compression ratio"):
            tool.admit_archive(self.archive, self.stage, self.manifest)

    def test_16_staging_mutations_are_rejected(self):
        write_zip(self.archive, source_files())
        tool = load_tool()
        self.admit()
        target = self.stage / "Makefile"
        original = target.read_bytes()
        target.write_bytes(b"X" * len(original))
        with self.assertRaisesRegex(ValueError, "staging"):
            tool.verify_staging(self.stage, self.manifest)

    def test_17_blob_extensions_are_rejected(self):
        for suffix in (".bin", ".fw", ".rom", ".img", ".dat", ".hex"):
            files = source_files()
            files["vendor/blob" + suffix] = b"opaque"
            write_zip(self.archive, files)
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "blob"):
                self.admit()

    def test_18_nul_and_control_bytes_are_rejected(self):
        for data in (b"abc\x00def", b"abc\x01def"):
            files = source_files()
            files["vendor/opaque.c"] = data
            write_zip(self.archive, files)
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "opaque"):
                self.admit()

    def test_19_vcs_metadata_is_rejected(self):
        for name in ("vendor/.git/config", "vendor/nested/.svn/entries", "vendor/.gitignore", "vendor/.gitmodules"):
            files = source_files()
            files[name] = b"x"
            write_zip(self.archive, files)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "VCS"):
                self.admit()

    def test_20_reserved_manifest_name_is_rejected(self):
        files = source_files()
        files["vendor/SOURCE-MANIFEST.json"] = b"{}"
        write_zip(self.archive, files)
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.admit()


if __name__ == "__main__":
    unittest.main()
