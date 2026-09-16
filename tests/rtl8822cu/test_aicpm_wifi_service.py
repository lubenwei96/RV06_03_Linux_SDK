import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "project/cfg/BoardConfig_IPC/overlay/aicpm-v1"
MANAGER = OVERLAY / "usr/sbin/aicpm-wifi-manager"
INIT = OVERLAY / "etc/init.d/S32aicpm-wifi"
ROOTFS = ROOT / "output/out/rootfs_uclibc_rv1106"
DHCPCD_CONF = OVERLAY / "etc/dhcpcd.conf"


def write_executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


def process_start_time(pid):
    return Path(f"/proc/{pid}/stat").read_text().split()[21]


class WifiServiceBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.bin = self.root / "bin"
        self.usb = self.root / "usb"
        self.net = self.root / "net"
        self.module = self.root / "module"
        self.run = self.root / "run"
        self.config = self.root / "wpa_supplicant.conf"
        self.trace = self.root / "trace"
        for path in (self.bin, self.usb, self.net, self.run):
            path.mkdir(parents=True)

        device = self.usb / "1-1"
        device.mkdir()
        (device / "idVendor").write_text("0bda\n")
        (device / "idProduct").write_text("c82c\n")
        self.config.write_text(
            "ctrl_interface=/var/run/wpa_supplicant\n"
            "update_config=0\n"
            "network={\nssid=\"fixture-network\"\npsk=" + "a" * 64 + "\n}\n"
        )
        self.config.chmod(0o600)

        write_executable(
            self.root / "loader",
            "#!/bin/sh\n"
            "printf 'loader\\n' >>\"$AICPM_TEST_TRACE\"\n"
            "mkdir -p \"$AICPM_MODULE_PATH\" \"$AICPM_NET_ROOT/wlan0\"\n",
        )
        write_executable(
            self.bin / "ip",
            "#!/bin/sh\nprintf 'ip:%s\\n' \"$*\" >>\"$AICPM_TEST_TRACE\"\n",
        )
        write_executable(
            self.bin / "logger",
            "#!/bin/sh\nprintf 'logger:%s\\n' \"$*\" >>\"$AICPM_TEST_TRACE\"\n",
        )
        write_executable(self.bin / "stat", "#!/bin/sh\nexit 127\n")
        write_executable(
            self.bin / "fake-wpa-worker",
            "#!/bin/sh\nwhile :; do sleep 1; done\n",
        )
        write_executable(
            self.bin / "wpa_supplicant",
            "#!/bin/sh\n"
            "printf 'wpa_supplicant:%s\\n' \"$*\" >>\"$AICPM_TEST_TRACE\"\n"
            "iface=; config=; pid_file=\n"
            "saved_args=\"$*\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -i) shift; iface=$1 ;;\n"
            "    -c) shift; config=$1 ;;\n"
            "    -P) shift; pid_file=$1 ;;\n"
            "  esac\n"
            "  shift\n"
            "done\n"
            "start_worker() {\n"
            "  fake-wpa-worker -i \"$iface\" -c \"$config\" >/dev/null 2>&1 &\n"
            "  worker=$!\n"
            "  printf '%s\\n' \"$worker\" >\"$pid_file\"\n"
            "  [ -z \"${AICPM_TEST_WORKER_PID_FILE:-}\" ] || "
            "printf '%s\\n' \"$worker\" >\"$AICPM_TEST_WORKER_PID_FILE\"\n"
            "}\n"
            "if [ -n \"${AICPM_TEST_DROP_FIRST_START_FILE:-}\" ] && "
            "[ ! -e \"$AICPM_TEST_DROP_FIRST_START_FILE\" ]; then\n"
            "  : >\"$AICPM_TEST_DROP_FIRST_START_FILE\"\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"${AICPM_TEST_DELAYED_PID:-0}\" = 1 ]; then\n"
            "  (sleep 0.2; start_worker) &\n"
            "else\n"
            "  start_worker\n"
            "fi\n",
        )
        write_executable(
            self.bin / "wpa_cli",
            "#!/bin/sh\n"
            "if [ -n \"$AICPM_TEST_WPA_STATE_FILE\" ]; then state=$(cat \"$AICPM_TEST_WPA_STATE_FILE\"); "
            "else state=${AICPM_TEST_WPA_STATE:-SCANNING}; fi\n"
            "printf 'wpa_state=%s\\n' \"$state\"\n",
        )
        write_executable(
            self.bin / "dhcpcd",
            "#!/bin/sh\nprintf 'dhcpcd:%s\\n' \"$*\" >>\"$AICPM_TEST_TRACE\"\n",
        )

        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": f"{self.bin}:{self.environment['PATH']}",
                "AICPM_USB_ROOT": str(self.usb),
                "AICPM_NET_ROOT": str(self.net),
                "AICPM_MODULE_PATH": str(self.module),
                "AICPM_WPA_CONFIG": str(self.config),
                "AICPM_WPA_OWNER_UID": str(os.getuid()),
                "AICPM_WIFI_LOADER": str(self.root / "loader"),
                "AICPM_WPA_PID_FILE": str(self.run / "wpa.pid"),
                "AICPM_WPA_IDENTITY_FILE": str(self.run / "wpa.identity"),
                "AICPM_WPA_PENDING_FILE": str(self.run / "wpa.pending"),
                "AICPM_WPA_PROCESS_TOKEN": "fake-wpa-worker",
                "AICPM_ASSOC_STATE_FILE": str(self.run / "association.state"),
                "AICPM_TEST_TRACE": str(self.trace),
            }
        )

    def tearDown(self):
        pid_file = self.run / "wpa.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 9)
            except (ProcessLookupError, ValueError):
                pass
        self.temporary_directory.cleanup()

    def run_once(self, **updates):
        environment = self.environment.copy()
        environment.update(updates)
        return subprocess.run(
            ["sh", str(MANAGER), "--once"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_loads_usb_driver_starts_wpa_and_requests_dhcp_once(self):
        first = self.run_once(AICPM_TEST_WPA_STATE="COMPLETED")
        self.assertEqual(0, first.returncode, first.stderr)
        second = self.run_once(AICPM_TEST_WPA_STATE="COMPLETED")
        self.assertEqual(0, second.returncode, second.stderr)

        trace = self.trace.read_text()
        self.assertEqual(1, trace.count("loader\n"), trace)
        self.assertEqual(1, trace.count("wpa_supplicant:"), trace)
        self.assertEqual(1, trace.count("dhcpcd:-n wlan0"), trace)
        self.assertIn("ip:link set wlan0 up", trace)
        self.assertNotIn("fixture-network", trace)
        self.assertNotIn("a" * 32, trace)

    def test_missing_or_insecure_config_never_starts_wpa(self):
        self.config.chmod(0o644)
        result = self.run_once()
        self.assertNotEqual(0, result.returncode)
        trace = self.trace.read_text()
        self.assertNotIn("wpa_supplicant:", trace)
        self.assertNotIn("fixture-network", trace)

    def test_dead_wpa_pid_is_replaced(self):
        self.module.mkdir()
        (self.net / "wlan0").mkdir()
        (self.run / "wpa.pid").write_text("999999\n")
        result = self.run_once()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, self.trace.read_text().count("wpa_supplicant:"))

    def test_retries_identity_capture_while_background_daemon_finishes_starting(self):
        self.module.mkdir()
        (self.net / "wlan0").mkdir()
        result = self.run_once(AICPM_TEST_DELAYED_PID="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.run / "wpa.identity").is_file())

    def test_failed_capture_is_reclaimed_without_starting_a_second_supplicant(self):
        self.module.mkdir()
        (self.net / "wlan0").mkdir()
        first = self.run_once(
            AICPM_TEST_DELAYED_PID="1",
            AICPM_WPA_IDENTITY_ATTEMPTS="1",
            AICPM_WPA_IDENTITY_INTERVAL="0.01",
        )
        self.assertNotEqual(0, first.returncode)
        time.sleep(0.3)
        second = self.run_once()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(1, self.trace.read_text().count("wpa_supplicant:"))

    def test_stale_pending_state_expires_and_retries_after_false_success(self):
        self.module.mkdir()
        (self.net / "wlan0").mkdir()
        dropped = self.run / "drop-first-start"
        updates = {
            "AICPM_TEST_DROP_FIRST_START_FILE": str(dropped),
            "AICPM_WPA_IDENTITY_ATTEMPTS": "1",
            "AICPM_WPA_IDENTITY_INTERVAL": "0.01",
            "AICPM_WPA_PENDING_ATTEMPTS": "2",
        }
        self.assertNotEqual(0, self.run_once(**updates).returncode)
        self.assertNotEqual(0, self.run_once(**updates).returncode)
        recovered = self.run_once(**updates)
        self.assertEqual(0, recovered.returncode, recovered.stderr)
        self.assertEqual(2, self.trace.read_text().count("wpa_supplicant:"))
        self.assertTrue((self.run / "wpa.identity").is_file())

    def test_stop_waits_for_delayed_pending_supplicant_and_terminates_it(self):
        self.module.mkdir()
        (self.net / "wlan0").mkdir()
        first = self.run_once(
            AICPM_TEST_DELAYED_PID="1",
            AICPM_TEST_WORKER_PID_FILE=str(self.run / "worker.pid"),
            AICPM_WPA_IDENTITY_ATTEMPTS="1",
            AICPM_WPA_IDENTITY_INTERVAL="0.01",
        )
        self.assertNotEqual(0, first.returncode)
        environment = self.environment.copy()
        environment.update(
            {
                "AICPM_WIFI_MANAGER": str(self.root / "absent-manager"),
                "AICPM_MANAGER_PID_FILE": str(self.run / "manager.pid"),
                "AICPM_MANAGER_IDENTITY_FILE": str(self.run / "manager.identity"),
                "AICPM_MANAGER_LOCK_DIR": str(self.run / "manager.lock"),
                "AICPM_WPA_PENDING_WAIT_ATTEMPTS": "20",
                "AICPM_WPA_PENDING_WAIT_INTERVAL": "0.05",
            }
        )
        stopped = subprocess.run(["sh", str(INIT), "stop"], env=environment)
        self.assertEqual(0, stopped.returncode)
        for _ in range(20):
            if (self.run / "worker.pid").exists():
                break
            time.sleep(0.05)
        else:
            self.fail("delayed supplicant did not reach the test fixture")
        pid = int((self.run / "worker.pid").read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse((self.run / "wpa.pending").exists())

    def test_live_unrelated_pid_is_not_trusted_as_supplicant(self):
        self.module.mkdir()
        (self.net / "wlan0").mkdir()
        (self.run / "wpa.pid").write_text(f"{os.getpid()}\n")
        result = self.run_once()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, self.trace.read_text().count("wpa_supplicant:"))

    def test_live_wpa_named_helper_is_not_trusted_as_supplicant(self):
        self.module.mkdir()
        (self.net / "wlan0").mkdir()
        helper_path = self.bin / "wpa-helper"
        write_executable(helper_path, "#!/bin/sh\nwhile :; do sleep 1; done\n")
        helper = subprocess.Popen([str(helper_path)])
        try:
            (self.run / "wpa.pid").write_text(f"{helper.pid}\n")
            (self.run / "wpa.identity").write_text(
                f"{helper.pid} {process_start_time(helper.pid)}\n"
            )
            result = self.run_once()
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIsNone(helper.poll(), "manager killed an unrelated process")
            self.assertEqual(1, self.trace.read_text().count("wpa_supplicant:"))
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()

    def test_no_target_usb_device_fails_without_loading(self):
        for child in self.usb.iterdir():
            for item in child.iterdir():
                item.unlink()
            child.rmdir()
        result = self.run_once()
        self.assertNotEqual(0, result.returncode)
        trace = self.trace.read_text()
        self.assertNotIn("loader\n", trace)
        self.assertNotIn("wpa_supplicant:", trace)

    def test_running_daemon_recovers_process_and_refreshes_dhcp_on_reassociation(self):
        state_file = self.root / "wpa.state"
        state_file.write_text("COMPLETED\n")
        environment = self.environment.copy()
        environment.update(
            {
                "AICPM_TEST_WPA_STATE_FILE": str(state_file),
                "AICPM_WIFI_INTERVAL": "1",
                "AICPM_WIFI_MAX_BACKOFF": "2",
            }
        )
        manager = subprocess.Popen(["sh", str(MANAGER), "run"], env=environment)
        try:
            for _ in range(50):
                if (self.run / "wpa.pid").exists() and self.trace.exists():
                    if self.trace.read_text().count("dhcpcd:-n wlan0") >= 1:
                        break
                time.sleep(0.1)
            else:
                self.fail("daemon did not establish its initial managed connection")

            old_pid = int((self.run / "wpa.pid").read_text().strip())
            os.kill(old_pid, 9)
            for _ in range(60):
                new_pid = int((self.run / "wpa.pid").read_text().strip())
                if new_pid != old_pid and self.trace.read_text().count("wpa_supplicant:") >= 2:
                    break
                time.sleep(0.1)
            else:
                self.fail("daemon did not replace the dead supplicant")

            state_file.write_text("SCANNING\n")
            for _ in range(40):
                if (self.run / "association.state").read_text().strip() == "SCANNING":
                    break
                time.sleep(0.1)
            else:
                self.fail("daemon did not observe disassociation")

            dhcp_before_reassociation = self.trace.read_text().count("dhcpcd:-n wlan0")
            state_file.write_text("COMPLETED\n")
            for _ in range(40):
                if self.trace.read_text().count("dhcpcd:-n wlan0") > dhcp_before_reassociation:
                    break
                time.sleep(0.1)
            else:
                self.fail("daemon did not refresh DHCP after reassociation")
        finally:
            manager.terminate()
            manager.wait(timeout=5)

    def test_retry_backoff_is_capped(self):
        for child in self.usb.iterdir():
            for item in child.iterdir():
                item.unlink()
            child.rmdir()
        delays = self.root / "delays"
        write_executable(
            self.bin / "sleep",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$1\" >>\"$AICPM_TEST_DELAYS\"\n"
            "lines=$(wc -l <\"$AICPM_TEST_DELAYS\")\n"
            "[ \"$lines\" -lt 5 ] || kill -TERM \"$PPID\"\n",
        )
        environment = self.environment.copy()
        environment.update(
            {
                "AICPM_TEST_DELAYS": str(delays),
                "AICPM_WIFI_INTERVAL": "1",
                "AICPM_WIFI_MAX_BACKOFF": "4",
            }
        )
        subprocess.run(["sh", str(MANAGER), "run"], env=environment, timeout=5, check=False)
        self.assertEqual(["1", "2", "4", "4", "4"], delays.read_text().splitlines())


class WifiInitBehavior(unittest.TestCase):
    def manager_environment(self, root, fake_manager):
        return {
            **os.environ,
            "AICPM_WIFI_MANAGER": str(fake_manager),
            "AICPM_MANAGER_PID_FILE": str(root / "manager.pid"),
            "AICPM_MANAGER_IDENTITY_FILE": str(root / "manager.identity"),
            "AICPM_MANAGER_LOCK_DIR": str(root / "manager.lock"),
            "AICPM_WPA_PID_FILE": str(root / "wpa.pid"),
            "AICPM_WPA_IDENTITY_FILE": str(root / "wpa.identity"),
            "AICPM_WPA_PENDING_FILE": str(root / "wpa.pending"),
            "AICPM_ASSOC_STATE_FILE": str(root / "association.state"),
        }

    def test_start_is_idempotent_and_stop_terminates_manager(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_manager = root / "manager"
            pid_file = root / "manager.pid"
            manager_identity = root / "manager.identity"
            wpa_pid_file = root / "wpa.pid"
            wpa_identity = root / "wpa.identity"
            association_state = root / "association.state"
            association_state.write_text("COMPLETED\n")
            write_executable(fake_manager, "#!/bin/sh\nwhile :; do sleep 1; done\n")
            environment = os.environ.copy()
            environment.update(
                {
                    "AICPM_WIFI_MANAGER": str(fake_manager),
                    "AICPM_MANAGER_PID_FILE": str(pid_file),
                    "AICPM_MANAGER_IDENTITY_FILE": str(manager_identity),
                    "AICPM_MANAGER_LOCK_DIR": str(root / "manager.lock"),
                    "AICPM_WPA_PID_FILE": str(wpa_pid_file),
                    "AICPM_WPA_IDENTITY_FILE": str(wpa_identity),
                    "AICPM_ASSOC_STATE_FILE": str(association_state),
                }
            )
            try:
                subprocess.run(["sh", str(INIT), "start"], env=environment, check=True)
                first_pid = int(pid_file.read_text().strip())
                subprocess.run(["sh", str(INIT), "start"], env=environment, check=True)
                self.assertEqual(first_pid, int(pid_file.read_text().strip()))
                subprocess.run(["sh", str(INIT), "status"], env=environment, check=True)
                subprocess.run(["sh", str(INIT), "restart"], env=environment, check=True)
                second_pid = int(pid_file.read_text().strip())
                self.assertNotEqual(first_pid, second_pid)
                subprocess.run(["sh", str(INIT), "status"], env=environment, check=True)
                os.kill(second_pid, 0)
                subprocess.run(["sh", str(INIT), "stop"], env=environment, check=True)
                self.assertFalse(association_state.exists())
                for _ in range(30):
                    try:
                        os.kill(second_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("manager process survived stop")
            finally:
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text().strip()), 9)
                    except (ProcessLookupError, ValueError):
                        pass

    def test_restart_refuses_to_duplicate_live_manager_with_missing_identity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_manager = root / "manager"
            write_executable(fake_manager, "#!/bin/sh\nwhile :; do sleep 1; done\n")
            environment = self.manager_environment(root, fake_manager)
            subprocess.run(["sh", str(INIT), "start"], env=environment, check=True)
            pid = int((root / "manager.pid").read_text().strip())
            (root / "manager.identity").unlink()
            try:
                result = subprocess.run(["sh", str(INIT), "restart"], env=environment)
                self.assertNotEqual(0, result.returncode)
                os.kill(pid, 0)
                self.assertEqual(pid, int((root / "manager.pid").read_text().strip()))
            finally:
                os.kill(pid, 9)

    def test_failed_identity_write_cleans_up_new_manager(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_manager = root / "manager"
            write_executable(fake_manager, "#!/bin/sh\nwhile :; do sleep 1; done\n")
            environment = self.manager_environment(root, fake_manager)
            identity_path = root / "identity-is-a-directory"
            identity_path.mkdir()
            environment["AICPM_MANAGER_IDENTITY_FILE"] = str(identity_path)
            result = subprocess.run(["sh", str(INIT), "start"], env=environment)
            self.assertNotEqual(0, result.returncode)
            pid = int((root / "manager.pid").read_text().strip())
            for _ in range(30):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, 9)
                self.fail("manager survived a failed identity write")

    def test_stale_start_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_manager = root / "manager"
            write_executable(fake_manager, "#!/bin/sh\nwhile :; do sleep 1; done\n")
            environment = self.manager_environment(root, fake_manager)
            lock = root / "manager.lock"
            lock.mkdir()
            (lock / "owner").write_text("999999 1\n")
            try:
                subprocess.run(["sh", str(INIT), "start"], env=environment, check=True)
                pid = int((root / "manager.pid").read_text().strip())
                os.kill(pid, 0)
            finally:
                if (root / "manager.pid").exists():
                    os.kill(int((root / "manager.pid").read_text().strip()), 9)

    def test_stop_does_not_kill_unrelated_wpa_named_process(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            helper_path = root / "wpa-helper"
            write_executable(helper_path, "#!/bin/sh\nwhile :; do sleep 1; done\n")
            helper = subprocess.Popen([str(helper_path)])
            wpa_pid_file = root / "wpa.pid"
            wpa_identity = root / "wpa.identity"
            wpa_pid_file.write_text(f"{helper.pid}\n")
            wpa_identity.write_text(
                f"{helper.pid} {process_start_time(helper.pid)}\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "AICPM_WIFI_MANAGER": str(root / "manager"),
                    "AICPM_MANAGER_PID_FILE": str(root / "manager.pid"),
                    "AICPM_MANAGER_IDENTITY_FILE": str(root / "manager.identity"),
                    "AICPM_MANAGER_LOCK_DIR": str(root / "manager.lock"),
                    "AICPM_WPA_PID_FILE": str(wpa_pid_file),
                    "AICPM_WPA_IDENTITY_FILE": str(wpa_identity),
                    "AICPM_ASSOC_STATE_FILE": str(root / "association.state"),
                }
            )
            try:
                result = subprocess.run(["sh", str(INIT), "stop"], env=environment)
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(helper.poll(), "init script killed an unrelated process")
            finally:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait()


class WifiServiceImageContract(unittest.TestCase):
    def test_dhcpcd_does_not_start_a_competing_supplicant(self):
        text = DHCPCD_CONF.read_text()
        directives = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(1, directives.count("nohook wpa_supplicant"))
        installed = ROOTFS / "etc/dhcpcd.conf"
        self.assertEqual(DHCPCD_CONF.read_bytes(), installed.read_bytes())

    def test_scripts_are_executable_valid_and_copied_to_rootfs(self):
        for source, installed in (
            (MANAGER, ROOTFS / "usr/sbin/aicpm-wifi-manager"),
            (INIT, ROOTFS / "etc/init.d/S32aicpm-wifi"),
        ):
            self.assertTrue(source.is_file() and os.access(source, os.X_OK), source)
            subprocess.run(["sh", "-n", str(source)], check=True)
            self.assertEqual(source.read_bytes(), installed.read_bytes())
            self.assertTrue(os.access(installed, os.X_OK), installed)


if __name__ == "__main__":
    unittest.main()
