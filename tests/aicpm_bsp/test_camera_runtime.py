import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.aicpm_bsp.common import ROOT


OVERLAY = ROOT / "project/cfg/BoardConfig_IPC/overlay/aicpm-v1"
IQ_NAME = "ov5647_HBV-RPI-AUTO-IRCUT-3.6-S1.0_3.6mm.json"
IQ = OVERLAY / "etc/iqfiles" / IQ_NAME
MODULE_INIT = OVERLAY / "etc/init.d/S20aicpm-media-modules"
PREVIEW = OVERLAY / "usr/bin/aicpm-camera-preview"
ROOTFS = ROOT / "output/out/rootfs_uclibc_rv1106"
EXPECTED_IQ_SHA256 = "3095663640111f3bfd3d39d5531f6ca232b2339335ec12f1029e6584cb7cce33"


def make_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


class CameraIqContract(unittest.TestCase):
    def test_product_overlay_contains_only_the_selected_ov5647_iq(self):
        iq_dir = IQ.parent
        self.assertTrue(IQ.is_file())
        self.assertEqual([path.name for path in iq_dir.glob("*.json")], [IQ_NAME])
        raw = IQ.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), EXPECTED_IQ_SHA256)
        data = json.loads(raw)
        self.assertEqual(data["sensor_calib"]["resolution"], {"width": 2592, "height": 1944})
        for key in ("sensor_calib", "module_calib", "main_scene", "uapi", "sys_static_cfg"):
            self.assertIn(key, data)

    def test_product_iq_uses_only_isp32_scenes_and_disables_unavailable_cac_map(self):
        data = json.loads(IQ.read_bytes())
        sub_scenes = [
            sub_scene
            for main_scene in data["main_scene"]
            for sub_scene in main_scene["sub_scene"]
        ]
        self.assertGreaterEqual(len(sub_scenes), 2)
        cac_scene_count = 0
        for sub_scene in sub_scenes:
            self.assertIn("scene_isp32", sub_scene)
            self.assertNotIn("scene_isp21", sub_scene)
            if "cac_v11" not in sub_scene["scene_isp32"]:
                continue
            cac_scene_count += 1
            cac = sub_scene["scene_isp32"]["cac_v11"]["SettingPara"]
            self.assertEqual(cac["enable"], 0)
            self.assertEqual(cac["psf_path"], "")
        self.assertGreater(cac_scene_count, 0)

    def test_final_rootfs_contains_the_exact_product_camera_assets(self):
        pairs = (
            (IQ, ROOTFS / "etc/iqfiles" / IQ_NAME),
            (MODULE_INIT, ROOTFS / "etc/init.d/S20aicpm-media-modules"),
            (PREVIEW, ROOTFS / "usr/bin/aicpm-camera-preview"),
        )
        for source, packaged in pairs:
            with self.subTest(packaged=packaged):
                self.assertTrue(packaged.is_file(), packaged)
                self.assertEqual(source.read_bytes(), packaged.read_bytes())
        self.assertTrue(os.access(pairs[1][1], os.X_OK))
        self.assertTrue(os.access(pairs[2][1], os.X_OK))


class MediaModuleInitContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.modules = self.root / "modules"
        self.modules.write_text("", encoding="utf-8")
        self.ko = self.root / "ko"
        self.ko.mkdir()
        for name in ("mpp_vcodec.ko", "rockit.ko", "hpmcu_wrap.bin"):
            (self.ko / name).write_text("fixture\n", encoding="utf-8")
        self.calls = self.root / "calls"
        make_executable(
            self.bin / "insmod",
            'printf "%s\\n" "$*" >> "$AICPM_TEST_CALLS"\n'
            'name=$(basename "$1" .ko)\n'
            'printf "%s 0 0 - Live 0x0\\n" "$name" >> "$AICPM_MODULES_FILE"',
        )
        make_executable(self.bin / "logger", 'printf "logger:%s\\n" "$*" >> "$AICPM_TEST_CALLS"')
        make_executable(self.bin / "grep", 'exec /bin/grep "$@"')
        make_executable(self.bin / "basename", 'exec /usr/bin/basename "$@"')

    def tearDown(self):
        self.temp.cleanup()

    def run_init(self):
        env = {
            "PATH": str(self.bin),
            "AICPM_MODULE_DIR": str(self.ko),
            "AICPM_MODULES_FILE": str(self.modules),
            "AICPM_TEST_CALLS": str(self.calls),
            "LC_ALL": "C",
        }
        return subprocess.run([str(MODULE_INIT), "start"], env=env, text=True, capture_output=True)

    def test_loads_media_modules_in_order_with_product_arguments_and_is_idempotent(self):
        first = self.run_init()
        self.assertEqual(first.returncode, 0, first.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls[0], str(self.ko / "mpp_vcodec.ko"))
        self.assertEqual(
            calls[1],
            f"{self.ko / 'rockit.ko'} mcu_fw_path={self.ko / 'hpmcu_wrap.bin'} "
            "mcu_fw_addr=0xff6fe000 isp_max_h=1944",
        )
        second = self.run_init()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.calls.read_text(encoding="utf-8").splitlines(), calls)


class CameraPreviewContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = self.root / "simple_vi_bind_venc_rtsp"
        self.arguments = self.root / "arguments"
        self.sample.write_text(
            "#!/usr/bin/python3\n"
            "import os\n"
            "import signal\n"
            "import sys\n"
            "import time\n"
            "open(os.environ['AICPM_TEST_ARGUMENTS'], 'w').write('\\n'.join(sys.argv[1:]) + '\\n')\n"
            "signal.signal(signal.SIGINT, lambda signum, frame: sys.exit(0))\n"
            "while True: time.sleep(1)\n",
            encoding="utf-8",
        )
        self.sample.chmod(0o755)
        self.run_dir = self.root / "run"
        self.iq_dir = self.root / "iqfiles"
        self.iq_dir.mkdir()
        (self.iq_dir / IQ_NAME).write_text("fixture\n", encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update(
            {
                "AICPM_SAMPLE": str(self.sample),
                "AICPM_IQ_DIR": str(self.iq_dir),
                "AICPM_RUN_DIR": str(self.run_dir),
                "AICPM_TEST_ARGUMENTS": str(self.arguments),
                "AICPM_START_WAIT": "0",
                "AICPM_STOP_WAIT": "3",
                "LC_ALL": "C",
            }
        )

    def tearDown(self):
        subprocess.run([str(PREVIEW), "stop"], env=self.env, capture_output=True)
        self.temp.cleanup()

    def run_preview(self, action):
        return subprocess.run([str(PREVIEW), action], env=self.env, text=True, capture_output=True)

    def test_start_uses_full_hd_h264_and_attached_aiq_argument_then_stops_cleanly(self):
        started = self.run_preview("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        deadline = time.monotonic() + 2
        while not self.arguments.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(
            self.arguments.read_text(encoding="utf-8").splitlines(),
            ["-I", "0", "-w", "1920", "-h", "1080", f"-a{self.iq_dir}", "-e", "h264", "-b", "4096"],
        )
        duplicate = self.run_preview("start")
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertIn("already running", duplicate.stdout)
        status = self.run_preview("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        stopped = self.run_preview("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertNotEqual(self.run_preview("status").returncode, 0)

    def test_start_rejects_missing_exact_product_iq(self):
        (self.iq_dir / IQ_NAME).unlink()
        result = self.run_preview("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(IQ_NAME, result.stderr)


if __name__ == "__main__":
    unittest.main()
