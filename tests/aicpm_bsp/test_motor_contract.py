import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.aicpm_bsp.common import ROOT, read_text


UAPI = "sysdrv/source/kernel/include/uapi/linux/aicpm_l9110s.h"
DRIVER = "sysdrv/source/kernel/drivers/misc/aicpm_l9110s.c"
CORE = "sysdrv/source/kernel/drivers/misc/aicpm_l9110s_core.c"
CORE_H = "sysdrv/source/kernel/drivers/misc/aicpm_l9110s_core.h"
TEST = "sysdrv/source/kernel/drivers/misc/aicpm_l9110s_test.c"
KCONFIG = "sysdrv/source/kernel/drivers/misc/Kconfig"
MAKEFILE = "sysdrv/source/kernel/drivers/misc/Makefile"
FRAGMENT = "sysdrv/source/kernel/arch/arm/configs/rv1106-aicpm.config"
BINDING = "sysdrv/source/kernel/Documentation/devicetree/bindings/misc/aicpm,l9110s.yaml"
DTS = "sysdrv/source/kernel/arch/arm/boot/dts/rv1106g-aicpm-v1.dts"
KUNIT_CONFIG = "tests/aicpm_bsp/l9110s.kunitconfig"


def strip_c_comments(text):
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.DOTALL)


def c_function_body(text, name):
    source = strip_c_comments(text)
    match = re.search(rf"\b{re.escape(name)}\s*\([^;]*?\)\s*\{{", source, re.DOTALL)
    if not match:
        raise AssertionError(f"missing C function: {name}")
    depth = 1
    in_string = False
    escaped = False
    for index in range(match.end(), len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.end() : index]
    raise AssertionError(f"unterminated C function: {name}")


def ordered(body, *needles):
    cursor = -1
    for needle in needles:
        cursor = body.find(needle, cursor + 1)
        if cursor < 0:
            raise AssertionError(f"missing/out-of-order token: {needle}")


def c_case_body(text, case_name, occurrence=1):
    matches = list(re.finditer(rf"\bcase\s+{re.escape(case_name)}\s*:", text))
    if len(matches) < occurrence:
        raise AssertionError(f"missing case {case_name} occurrence {occurrence}")
    start = matches[occurrence - 1].end()
    end_match = re.search(r"\b(?:case\s+|default\s*:)", text[start:])
    return text[start : start + end_match.start()] if end_match else text[start:]


def config_block(text, symbol):
    match = re.search(
        rf"^config {re.escape(symbol)}\n(?P<body>.*?)(?=^config |^menuconfig |^endmenu\b|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing Kconfig symbol: {symbol}")
    return match.group("body")


def assert_kconfig_contract(text):
    driver = config_block(text, "AICPM_L9110S")
    if not re.search(r"^\s*bool(?:\s|$)", driver, re.MULTILINE):
        raise AssertionError("driver must be bool")
    if re.search(r"^\s*tristate(?:\s|$)", driver, re.MULTILINE):
        raise AssertionError("driver must not be tristate")
    depends = re.findall(r"^\s*depends on (.+)$", driver, re.MULTILINE)
    if depends != ["OF && PWM"]:
        raise AssertionError(f"unexpected driver dependency: {depends}")
    test = config_block(text, "AICPM_L9110S_KUNIT_TEST")
    if not re.search(r"^\s*bool(?:\s|$)", test, re.MULTILINE):
        raise AssertionError("KUnit target must be bool")
    if re.findall(r"^\s*depends on (.+)$", test, re.MULTILINE) != [
        "KUNIT && AICPM_L9110S"
    ]:
        raise AssertionError("KUnit target must require built-in driver")


def struct_fields(text, name):
    match = re.search(rf"struct\s+{re.escape(name)}\s*\{{(?P<body>.*?)\}}\s*;", text, re.DOTALL)
    if not match:
        raise AssertionError(f"missing struct {name}")
    return [
        (field_type, field_name)
        for field_type, field_name in re.findall(
            r"^\s*(__[us](?:8|16|32|64))\s+([A-Za-z_]\w*)\s*;\s*$",
            strip_c_comments(match.group("body")),
            re.MULTILINE,
        )
    ]


def assert_uapi_layout(text):
    expected = {
        "aicpm_l9110s_run": [
            ("__u32", "abi_version"),
            ("__u32", "direction"),
            ("__u32", "duty_permille"),
            ("__u32", "duration_ms"),
        ],
        "aicpm_l9110s_status": [
            ("__u32", "abi_version"),
            ("__u32", "direction"),
            ("__s32", "last_error"),
            ("__u32", "remaining_ms"),
        ],
    }
    for name, fields in expected.items():
        actual = struct_fields(text, name)
        if actual != fields:
            raise AssertionError(f"{name} layout mismatch: {actual}")
    for macro in (
        "AICPM_L9110S_IOC_RUN",
        "AICPM_L9110S_IOC_STOP",
        "AICPM_L9110S_IOC_GET_STATUS",
    ):
        if text.count(f"#define {macro}") != 1:
            raise AssertionError(f"missing/duplicate ioctl: {macro}")


def dts_node(text, label):
    match = re.search(rf"\b{re.escape(label)}\s*:\s*[A-Za-z0-9,._+@-]+\s*\{{", text)
    if not match:
        raise AssertionError(f"missing DTS node {label}")
    depth = 1
    for index in range(match.end(), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    raise AssertionError(f"unterminated DTS node {label}")


def assert_pair_contract(driver):
    stop = c_function_body(driver, "aicpm_l9110s_pair_stop")
    ordered(
        stop,
        "first = pair->active_output",
        "second =",
        "pair->ops->apply(pair->context, first, 0)",
        "pair->ops->apply(pair->context, second, 0)",
        "if (!first_error)",
    )
    if "if (!first_error)" not in stop:
        raise AssertionError("pair stop must preserve the first hardware errno")
    drive = c_function_body(driver, "aicpm_l9110s_pair_drive")
    ordered(
        drive,
        "inactive =",
        "pair->ops->apply(pair->context, inactive, 0)",
        "pair->ops->apply(pair->context, active, duty_permille)",
        "aicpm_l9110s_pair_stop(pair)",
    )
    if "first_error" not in drive:
        raise AssertionError("drive cleanup must retain the triggering errno")


def assert_generation_contract(driver):
    schedule = c_function_body(driver, "aicpm_l9110s_schedule_timeout")
    ordered(
        schedule,
        "mutex_lock(&motor->core.state_lock)",
        "timeout_generation = generation",
        "mutex_unlock(&motor->core.state_lock)",
        "mod_delayed_work",
    )
    if schedule.count("mod_delayed_work") != 1:
        raise AssertionError("schedule wrapper must queue exactly once")
    worker = c_function_body(driver, "aicpm_l9110s_timeout_work")
    ordered(
        worker,
        "mutex_lock(&motor->core.state_lock)",
        "timeout_generation",
        "mutex_unlock(&motor->core.state_lock)",
        "aicpm_l9110s_timeout_core",
    )
    if any(token in worker for token in ("op_lock", "cancel_delayed_work", "mod_delayed_work")):
        raise AssertionError("worker must not own transactions or scheduling")


def assert_lifetime_contract(driver):
    probe = c_function_body(driver, "aicpm_l9110s_probe")
    if "devm_kzalloc" in probe or "kzalloc" not in probe:
        raise AssertionError("fd-visible storage must be explicitly refcounted")
    ordered(probe, "kref_init", "aicpm_l9110s_stop_transaction", "misc_register")
    if probe.count("platform_set_drvdata") != 2:
        raise AssertionError("probe must publish then clear drvdata on unwind")
    if probe.count("kref_put") != 1:
        raise AssertionError("probe unwind must drop its sole owner reference")
    open_body = c_function_body(driver, "aicpm_l9110s_open")
    ordered(
        open_body,
        "aicpm_l9110s_open_transaction",
        "if (ret)",
        "kref_get(&motor->refcount)",
        "file->private_data = motor",
    )
    remove = c_function_body(driver, "aicpm_l9110s_remove")
    ordered(
        remove,
        "misc_deregister",
        "aicpm_l9110s_begin_remove_transaction",
        "platform_set_drvdata",
        "kref_put",
    )
    if remove.count("kref_put") != 1:
        raise AssertionError("remove must drop exactly one platform-owner reference")
    release = c_function_body(driver, "aicpm_l9110s_release")
    if "pwm" in release or release.count("kref_put") != 1:
        raise AssertionError("post-remove release must only close core/drop fd reference")


def assert_schema_contract(binding):
    for pattern in (
        r"pwms:\s*\n\s*minItems: 2\s*\n\s*maxItems: 2",
        r"pwm-names:.*?- const: in-a.*?- const: in-b",
        r"aicpm,dead-time-us:.*?minimum: 1.*?maximum: 100000",
        r"aicpm,max-run-time-ms:.*?minimum: 1.*?maximum: 30000",
    ):
        if not re.search(pattern, binding, re.DOTALL):
            raise AssertionError(f"schema contract mismatch: {pattern}")


def assert_disabled_dts_contract(dts):
    node = dts_node(dts, "l9110s0")
    if len(re.findall(r'compatible\s*=\s*"aicpm,l9110s"\s*;', dts)) != 1:
        raise AssertionError("exactly one L9110S instance is allowed")
    for value in (
        'compatible = "aicpm,l9110s";',
        'pwm-names = "in-a", "in-b";',
        "aicpm,dead-time-us = <2000>;",
        "aicpm,max-run-time-ms = <3000>;",
        'status = "disabled";',
    ):
        if value not in node:
            raise AssertionError(f"motor node mismatch: {value}")
    for pwm in ("pwm5", "pwm6"):
        if not re.search(rf"&{pwm}\s*\{{\s*status\s*=\s*\"disabled\";\s*\}};", dts):
            raise AssertionError(f"{pwm} must remain disabled")


class MotorContract(unittest.TestCase):
    def test_abi_is_fixed_16_bytes_and_compat_safe(self):
        uapi = read_text(UAPI)
        assert_uapi_layout(uapi)
        self.assertIn("AICPM_L9110S_ABI_VERSION 1U", uapi)
        self.assertNotRegex(strip_c_comments(uapi), r"\blong\b|\w+\s*\*")
        for mutant in (
            uapi.replace("__u32 duration_ms", "__u64 duration_ms", 1),
            uapi.replace("__s32 last_error", "__u32 last_error", 1),
            uapi.replace("__u32 duty_permille;\n\t__u32 duration_ms;", "__u32 duration_ms;\n\t__u32 duty_permille;", 1),
        ):
            with self.assertRaises(AssertionError):
                assert_uapi_layout(mutant)

        program = r'''
#include <linux/aicpm_l9110s.h>
int main(void)
{
    _Static_assert(sizeof(struct aicpm_l9110s_run) == 16, "run ABI");
    _Static_assert(sizeof(struct aicpm_l9110s_status) == 16, "status ABI");
    _Static_assert(_IOC_SIZE(AICPM_L9110S_IOC_RUN) == 16, "run ioctl");
    _Static_assert(_IOC_SIZE(AICPM_L9110S_IOC_GET_STATUS) == 16, "status ioctl");
    _Static_assert(AICPM_L9110S_IOC_RUN != AICPM_L9110S_IOC_STOP, "RUN/STOP");
    _Static_assert(AICPM_L9110S_IOC_RUN != AICPM_L9110S_IOC_GET_STATUS, "RUN/STATUS");
    _Static_assert(AICPM_L9110S_IOC_STOP != AICPM_L9110S_IOC_GET_STATUS, "STOP/STATUS");
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "abi"
            subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Werror", "-D__KERNEL__",
                 "-I", str(ROOT / "sysdrv/source/kernel/include"),
                 "-I", str(ROOT / "sysdrv/source/kernel/include/uapi"),
                 "-x", "c", "-", "-o", str(binary)],
                input=program, text=True, check=True, cwd=ROOT,
            )
            subprocess.run([str(binary)], check=True)

    def test_build_configuration_is_builtin_and_of_enabled(self):
        kconfig = read_text(KCONFIG)
        assert_kconfig_contract(kconfig)
        for mutant in (
            kconfig.replace("\tbool \"AICPM", "\ttristate \"AICPM", 1),
            kconfig.replace("\tdepends on OF && PWM", "\tdepends on PWM", 1),
            kconfig.replace("\tdepends on KUNIT && AICPM_L9110S", "\tdepends on KUNIT", 1),
        ):
            with self.assertRaises(AssertionError):
                assert_kconfig_contract(mutant)
        makefile = read_text(MAKEFILE)
        self.assertEqual(makefile.count("obj-$(CONFIG_AICPM_L9110S)"), 1)
        self.assertIn("aicpm_l9110s_core.o aicpm_l9110s.o", makefile)
        self.assertEqual(makefile.count("obj-$(CONFIG_AICPM_L9110S_KUNIT_TEST)"), 1)
        self.assertIn("CONFIG_AICPM_L9110S=y", read_text(FRAGMENT).splitlines())
        self.assertEqual(read_text(KUNIT_CONFIG).splitlines(), [
            "CONFIG_KUNIT=y", "CONFIG_OF=y", "CONFIG_PWM=y",
            "CONFIG_AICPM_L9110S=y", "CONFIG_AICPM_L9110S_KUNIT_TEST=y",
        ])

    def test_core_transactions_freeze_errors_fault_and_dying_lifecycle(self):
        source = read_text(CORE_H) + "\n" + read_text(CORE)
        for name in (
            "aicpm_l9110s_open_transaction", "aicpm_l9110s_close_transaction",
            "aicpm_l9110s_run_transaction", "aicpm_l9110s_stop_transaction",
            "aicpm_l9110s_begin_remove_transaction", "aicpm_l9110s_timeout_core",
            "aicpm_l9110s_get_status_transaction",
        ):
            body = c_function_body(source, name)
            if name != "aicpm_l9110s_timeout_core":
                self.assertIn("mutex_lock(&core->op_lock)", body, name)
        validate = c_function_body(source, "aicpm_l9110s_validate_run")
        self.assertRegex(validate, re.compile(r"abi_version\s*!=.*?-EINVAL", re.DOTALL))
        self.assertRegex(validate, re.compile(r"duration_ms\s*==\s*0.*?-EINVAL", re.DOTALL))
        self.assertRegex(validate, re.compile(r"duration_ms\s*>.*?-ERANGE", re.DOTALL))
        run = c_function_body(source, "aicpm_l9110s_run_transaction")
        ordered(run, "aicpm_l9110s_validate_run", "cancel_timeout_sync")
        ordered(run, "cancel_timeout_sync", "stop", "dead_time", "drive", "schedule_timeout")
        self.assertIn("fault_latched", run)
        self.assertIn("-ENODEV", run)
        begin_remove = c_function_body(source, "aicpm_l9110s_begin_remove_transaction")
        ordered(begin_remove, "dying = true", "cancel_timeout_sync", "stop")
        close = c_function_body(source, "aicpm_l9110s_close_transaction")
        self.assertRegex(close, re.compile(r"if\s*\(.*dying.*\).*goto\s+out_unlock", re.DOTALL))
        timeout = c_function_body(source, "aicpm_l9110s_timeout_core")
        self.assertIn("mutex_lock(&core->state_lock)", timeout)
        self.assertNotIn("op_lock", timeout)
        self.assertNotIn("cancel_timeout_sync", timeout)
        self.assertIn("generation != core->generation", timeout)

    def test_driver_pwm_and_timeout_wrappers_are_ordered_and_synchronized(self):
        driver = read_text(DRIVER)
        assert_pair_contract(driver)
        apply_output = c_function_body(driver, "aicpm_l9110s_apply_output")
        for token in ("PWM_POLARITY_NORMAL", "enabled = true", "pwm_apply_state"):
            self.assertIn(token, apply_output)
        cancel = c_function_body(driver, "aicpm_l9110s_cancel_timeout_sync")
        self.assertEqual(cancel.count("cancel_delayed_work_sync"), 1)
        assert_generation_contract(driver)
        pair_mutations = (
            driver.replace("first = pair->active_output", "first = AICPM_L9110S_OUTPUT_A", 1),
            driver.replace("pair->ops->apply(pair->context, second, 0)", "/* skipped second low */", 1),
            driver.replace("if (!first_error)", "if (first_error)", 1),
            driver.replace("aicpm_l9110s_pair_stop(pair)", "0", 1),
        )
        for mutant in pair_mutations:
            with self.assertRaises(AssertionError):
                assert_pair_contract(mutant)
        generation_mutations = (
            driver.replace("mutex_lock(&motor->core.state_lock)", "/* lock removed */", 1),
            driver.replace("timeout_generation = generation", "timeout_generation = 0", 1),
            driver.replace("mutex_unlock(&motor->core.state_lock);\n\t(void)mod_delayed_work", "(void)mod_delayed_work", 1),
        )
        for mutant in generation_mutations:
            with self.assertRaises(AssertionError):
                assert_generation_contract(mutant)

    def test_misc_lifetime_and_ioctl_contract(self):
        driver = read_text(DRIVER)
        assert_lifetime_contract(driver)
        for mutant in (
            driver.replace("kzalloc", "devm_kzalloc", 1),
            driver.replace("kref_get(&motor->refcount)", "/* missing fd reference */", 1),
            driver.replace("misc_deregister(&motor->miscdev);", "/* deregister late */", 1),
            driver.replace("platform_set_drvdata(pdev, NULL);", "/* stale drvdata */", 1),
            driver.replace("kref_put(&motor->refcount", "/* leaked owner */ kref_get(&motor->refcount", 1),
        ):
            with self.assertRaises(AssertionError):
                assert_lifetime_contract(mutant)
        ioctl = c_function_body(driver, "aicpm_l9110s_ioctl")
        ordered(ioctl, "default:", "return -ENOTTY", "aicpm_l9110s_check_live_transaction")
        self.assertIn("return -EFAULT", ioctl)
        self.assertIn("struct aicpm_l9110s_status status = { 0 }", ioctl)
        self.assertIn("aicpm_l9110s_get_status_transaction", ioctl)
        run_case = c_case_body(ioctl, "AICPM_L9110S_IOC_RUN", occurrence=2)
        ordered(run_case, "copy_from_user", "aicpm_l9110s_run_transaction")
        for command in ("AICPM_L9110S_IOC_RUN", "AICPM_L9110S_IOC_STOP", "AICPM_L9110S_IOC_GET_STATUS"):
            self.assertGreaterEqual(ioctl.count(f"case {command}:"), 2)
        fops = strip_c_comments(driver)
        self.assertRegex(fops, r"\.compat_ioctl\s*=\s*compat_ptr_ioctl")
        self.assertRegex(fops, r"static_assert\s*\(sizeof\(struct aicpm_l9110s_run\)\s*==\s*16\)")
        self.assertRegex(fops, r"static_assert\s*\(sizeof\(struct aicpm_l9110s_status\)\s*==\s*16\)")

    def test_kunit_suite_is_exactly_11_behavioral_cases(self):
        test = strip_c_comments(read_text(TEST))
        expected = [
            "aicpm_l9110s_rejects_wrong_abi", "aicpm_l9110s_rejects_zero_duration",
            "aicpm_l9110s_rejects_duration_above_dt_limit", "aicpm_l9110s_orders_drive_and_status_counts_down",
            "aicpm_l9110s_rejects_second_open", "aicpm_l9110s_close_forces_stop",
            "aicpm_l9110s_timeout_forces_stop_without_self_cancel", "aicpm_l9110s_run_stop_race_leaves_outputs_low",
            "aicpm_l9110s_concurrent_runs_keep_newest_timeout", "aicpm_l9110s_suspend_forces_stop",
            "aicpm_l9110s_hardware_error_forces_stop",
        ]
        cases = re.findall(r"KUNIT_CASE\((aicpm_l9110s_[A-Za-z0-9_]+)\)", test)
        self.assertEqual(cases, expected)
        self.assertRegex(test, r"\.name\s*=\s*\"aicpm_l9110s\"")
        concurrent = c_function_body(test, "aicpm_l9110s_concurrent_runs_keep_newest_timeout")
        for token in ("completion", "kthread_run", "wait_for_completion", "complete"):
            self.assertIn(token, concurrent)
        hardware = c_function_body(test, "aicpm_l9110s_hardware_error_forces_stop")
        for token in ("fail_apply_call", "first_error", "fault_latched", "KUNIT_EXPECT_EQ"):
            self.assertIn(token, hardware)
        lifecycle = c_function_body(test, "aicpm_l9110s_run_stop_race_leaves_outputs_low")
        for token in ("begin_remove", "-ENODEV", "close_transaction"):
            self.assertIn(token, lifecycle)
        invalid = c_function_body(test, "aicpm_l9110s_rejects_wrong_abi")
        for token in ("cancel_count", "generation", "direction", "AICPM_L9110S_FORWARD"):
            self.assertIn(token, invalid)

    def test_schema_and_dts_keep_motor_physically_gated(self):
        binding = read_text(BINDING)
        assert_schema_contract(binding)
        dts = read_text(DTS)
        assert_disabled_dts_contract(dts)
        for mutant in (
            binding.replace("maxItems: 2", "maxItems: 3", 1),
            binding.replace("maximum: 30000", "maximum: 60000", 1),
            binding.replace("- const: in-b", "- const: motor-b", 1),
        ):
            with self.assertRaises(AssertionError):
                assert_schema_contract(mutant)
        for mutant in (
            dts.replace('status = "disabled";', 'status = "okay";', 1),
            dts.replace('status = "disabled";', "", 1),
            dts.replace("&pwm5 { status = \"disabled\"; };", "&pwm5 { status = \"okay\"; };", 1),
            dts.replace("aicpm,max-run-time-ms = <3000>;", "aicpm,max-run-time-ms = <30000>;", 1),
            dts.replace(
                "l9110s0: motor-controller {",
                'duplicate-motor { compatible = "aicpm,l9110s"; status = "disabled"; };\n\tl9110s0: motor-controller {',
                1,
            ),
        ):
            with self.assertRaises(AssertionError):
                assert_disabled_dts_contract(mutant)


if __name__ == "__main__":
    unittest.main()
