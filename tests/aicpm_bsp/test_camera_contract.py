import re
import unittest

from tests.aicpm_bsp.common import read_text


CAMERA = "sysdrv/source/kernel/arch/arm/boot/dts/rv1106g-aicpm-v1-camera.dtsi"
DRIVER = "sysdrv/source/kernel/drivers/media/i2c/ov5647.c"
BINDING = "sysdrv/source/kernel/Documentation/devicetree/bindings/media/i2c/ov5647.yaml"


class CameraContract(unittest.TestCase):
    @property
    def camera(self):
        return read_text(CAMERA)

    def test_cam0_enabled_path(self):
        for token in (
            "clock-frequency = <25000000>",
            "&i2c4",
            "i2c4m2_xfer",
            "ov5647_cam0: camera@36",
            "reg = <0x36>",
            "vdd-supply = <&vdd_cam0_3v3>",
            "&csi2_dphy1",
            "&mipi0_csi2",
            "&rkcif_mipi_lvds",
            "&rkcif_mipi_lvds_sditf",
            "&rkisp_vir0",
            "data-lanes = <1 2>",
        ):
            self.assertIn(token, self.camera)

    def test_cam1_reserved_disabled(self):
        for token in (
            "&i2c3",
            "i2c3m2_xfer",
            "ov5647_cam1: camera@36",
            "vdd-supply = <&vdd_cam1_3v3>",
            "&csi2_dphy2",
            "&mipi1_csi2",
            "&rkcif_mipi_lvds1",
            "&rkcif_mipi_lvds1_sditf",
            "&rkisp_vir1",
        ):
            self.assertIn(token, self.camera)
        self.assertGreaterEqual(self.camera.count('status = "disabled"'), 7)

    def test_mipi_endpoint_reg_has_local_address_cells(self):
        for mipi, input_reg, output_reg in (("mipi0_csi2", 1, 0), ("mipi1_csi2", 1, 0)):
            block = re.search(rf"&{mipi}\s*\{{(?P<body>.*?)^\}};", self.camera, re.DOTALL | re.MULTILINE)
            self.assertIsNotNone(block)
            body = block.group("body")
            for port_reg, endpoint_reg in ((0, input_reg), (1, output_reg)):
                port = re.search(
                    rf"port@{port_reg}\s*\{{(?P<body>.*?)^\s*\}};",
                    body,
                    re.DOTALL | re.MULTILINE,
                )
                self.assertIsNotNone(port)
                self.assertIn("#address-cells = <1>;", port.group("body"))
                self.assertIn("#size-cells = <0>;", port.group("body"))
                self.assertIn(f"reg = <{endpoint_reg}>;", port.group("body"))

    def test_unverified_control_gpio_not_bound(self):
        for forbidden in ("RK_PA2", "RK_PA4", "RK_PA5", "RK_PA7", "pwdn-gpios", "reset-gpios"):
            self.assertNotIn(forbidden, self.camera)

    def test_current_driver_is_not_accepted_as_complete(self):
        driver = read_text(DRIVER)
        self.assertIn("MEDIA_BUS_FMT_SBGGR10_1X10", driver)
        self.assertIn("ov5647_1080p30_10bpp", driver)
        self.assertIn("RKMODULE_GET_MODULE_INFO", driver)


if __name__ == "__main__":
    unittest.main()
