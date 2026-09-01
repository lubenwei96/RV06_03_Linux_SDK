import re
import unittest

from tests.aicpm_bsp.common import read_text


BOARD = "project/cfg/BoardConfig_IPC/BoardConfig-SPI_NAND-NONE-RV1106_AICPM-V1.mk"
FRAGMENT = "sysdrv/source/kernel/arch/arm/configs/rv1106-aicpm.config"
BUILD_SCRIPT = "project/build.sh"


def exported(text: str, name: str) -> str:
    match = re.search(rf"^export\s+{re.escape(name)}=(.*)$", text, re.MULTILINE)
    if not match:
        raise AssertionError(f"missing export {name}")
    return match.group(1).strip().strip('"')


class BoardConfigContract(unittest.TestCase):
    def setUp(self):
        self.board = read_text(BOARD)
        self.fragment = read_text(FRAGMENT)
        self.build_script = read_text(BUILD_SCRIPT)

    def test_fixed_build_identity(self):
        self.assertEqual(exported(self.board, "RK_CHIP"), "rv1106")
        self.assertEqual(exported(self.board, "RK_BOOT_MEDIUM"), "spi_nand")
        self.assertEqual(exported(self.board, "RK_KERNEL_DTS"), "rv1106g-aicpm-v1.dts")
        self.assertEqual(
            exported(self.board, "RK_KERNEL_DEFCONFIG_FRAGMENT"),
            "rv1106-pm.config rv1106-aicpm.config",
        )

    def test_first_board_gates(self):
        self.assertEqual(exported(self.board, "RK_ENABLE_WIFI"), "n")
        self.assertEqual(exported(self.board, "RK_ENABLE_WIFI_CHIP"), "")
        self.assertEqual(exported(self.board, "RK_ENABLE_ADBD"), "n")
        self.assertEqual(exported(self.board, "RK_ENABLE_MOTOR"), "n")
        self.assertEqual(exported(self.board, "RK_POST_OVERLAY"), "aicpm-v1")

    def test_removed_rv06_products(self):
        for forbidden in ("AIC8800", "RK_LVGL_APP", "RGB", "PCA9535"):
            self.assertNotIn(forbidden, self.board)
        self.assertEqual(exported(self.board, "RK_AICPM_PACKAGE_IQFILES"), "n")
        self.assertEqual(exported(self.board, "RK_CAMERA_SENSOR_IQFILES"), "")
        self.assertEqual(exported(self.board, "RK_CAMERA_SENSOR_CAC_BIN"), "")

    def test_iq_packaging_requires_explicit_product_opt_in(self):
        start = self.build_script.index("function __PACKAGE_RESOURCES()")
        end = self.build_script.index("\n}\n", start)
        body = self.build_script[start:end]
        sentinel = 'if [ "${RK_AICPM_PACKAGE_IQFILES:-y}" = "n" ];then'
        self.assertIn(sentinel, body)
        self.assertLess(body.index(sentinel), body.index("RK_CAMERA_SENSOR_IQFILES"))
        self.assertIn('msg_warn "IQ/CAC packaging explicitly disabled by BoardConfig"', body)

    def test_kernel_fragment(self):
        required = {
            "CONFIG_KEYBOARD_GPIO=y",
            "CONFIG_VIDEO_OV5647=y",
            "CONFIG_VIDEO_ROCKCHIP_CIF=y",
            "CONFIG_VIDEO_ROCKCHIP_ISP=y",
            "CONFIG_PHY_ROCKCHIP_CSI2_DPHY=y",
            "CONFIG_PWM_ROCKCHIP=y",
            "# CONFIG_BT is not set",
        }
        self.assertTrue(required.issubset(set(self.fragment.splitlines())))

    def test_board_config_reset_really_unsets_variables(self):
        start = self.build_script.index("function unset_board_config_all()")
        end = self.build_script.index("\n}\n", start)
        body = self.build_script[start:end]
        self.assertIn('unset "$board_var"', body)
        self.assertIn("sort -u", body)
        self.assertNotIn("source $tmp_file", body)

    def test_multiple_kernel_fragments_are_one_make_argument(self):
        self.assertIn(
            'KERNEL_CFG_FRAGMENT="${RK_KERNEL_DEFCONFIG_FRAGMENT}"',
            self.build_script,
        )


if __name__ == "__main__":
    unittest.main()
