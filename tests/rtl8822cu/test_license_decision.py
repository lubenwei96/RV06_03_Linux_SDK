import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/rtl8822cu_validate_license.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("rtl8822cu_validate_license", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LicenseDecision(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.json"
        self.decision = self.root / "decision.json"
        self.manifest_data = {"archive_sha256": "a" * 64, "source_tree_sha256": "b" * 64, "license_files": ["COPYING"], "binary_blob_candidates": []}
        self.decision_data = {
            "schema_version": 1, "archive_sha256": "a" * 64,
            "source_tree_sha256": "b" * 64,
            "supplier": "community mirror of Realtek archive",
            "obtained_at_utc": "2026-09-02T00:00:00Z", "license_files": ["COPYING"],
            "source_repository_allowed": True, "patch_redistribution_allowed": True,
            "approved_binary_blobs": [], "reviewer": "project owner",
            "reviewed_at_utc": "2026-09-02T00:00:01Z", "engineering_only": True,
            "production_provenance_status": "BLOCKED",
        }

    def tearDown(self):
        self.temp.cleanup()

    def validate(self):
        self.manifest.write_text(json.dumps(self.manifest_data))
        self.decision.write_text(json.dumps(self.decision_data))
        return load_tool().validate(self.manifest, self.decision)

    def test_01_matching_allowed_decision_passes(self):
        self.validate()

    def test_02_archive_sha_mismatch_is_rejected(self):
        self.decision_data["archive_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "archive_sha256"):
            self.validate()

    def test_03_source_tree_sha_mismatch_is_rejected(self):
        self.decision_data["source_tree_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "source_tree_sha256"):
            self.validate()

    def test_04_false_permission_or_missing_reviewer_is_rejected(self):
        for key in ("source_repository_allowed", "patch_redistribution_allowed"):
            self.decision_data[key] = False
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.validate()
            self.decision_data[key] = True
        self.decision_data["reviewer"] = ""
        with self.assertRaisesRegex(ValueError, "reviewer"):
            self.validate()

    def test_05_blob_or_license_set_mismatch_is_rejected(self):
        self.decision_data["approved_binary_blobs"] = [{"path": "blob.bin"}]
        with self.assertRaisesRegex(ValueError, "binary"):
            self.validate()
        self.decision_data["approved_binary_blobs"] = []
        self.decision_data["license_files"] = ["LICENSE"]
        with self.assertRaisesRegex(ValueError, "license_files"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
