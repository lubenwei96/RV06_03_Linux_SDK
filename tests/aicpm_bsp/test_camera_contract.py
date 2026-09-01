import re
import unittest

from tests.aicpm_bsp.common import read_text


CAMERA = "sysdrv/source/kernel/arch/arm/boot/dts/rv1106g-aicpm-v1-camera.dtsi"
DRIVER = "sysdrv/source/kernel/drivers/media/i2c/ov5647.c"
BINDING = "sysdrv/source/kernel/Documentation/devicetree/bindings/media/i2c/ov5647.yaml"


def node_block(text, header):
    match = re.search(
        rf"^[ \t]*{re.escape(header)}[ \t]*\{{[ \t]*$",
        text,
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"missing node block: {header}")

    depth = 0
    for index in range(match.end() - 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    raise AssertionError(f"unterminated node block: {header}")


def direct_properties(block):
    depth = 0
    properties = []
    for line in block.splitlines():
        if depth == 1 and "{" not in line and "}" not in line:
            stripped = line.strip()
            if stripped:
                properties.append(stripped)
        depth += line.count("{") - line.count("}")
    return properties


class CameraContract(unittest.TestCase):
    @property
    def camera(self):
        return read_text(CAMERA)

    def assert_node_properties(self, scope, header, required, status=None):
        block = node_block(scope, header)
        properties = direct_properties(block)
        for prop in required:
            self.assertIn(prop, properties, f"{header} must own {prop}")
        if status is not None:
            status_properties = [prop for prop in properties if prop.startswith("status = ")]
            self.assertEqual(
                status_properties,
                [f'status = "{status}";'],
                f"{header} must be explicitly {status}",
            )
        return block

    def assert_endpoint_remote(self, header, remote):
        endpoint = node_block(self.camera, header)
        remote_properties = [
            prop for prop in direct_properties(endpoint) if prop.startswith("remote-endpoint = ")
        ]
        self.assertEqual(
            remote_properties,
            [f"remote-endpoint = <&{remote}>;"],
            f"{header} must point only to {remote}",
        )

    def assert_endpoint_lanes(self, header):
        endpoint = node_block(self.camera, header)
        lane_properties = [
            prop for prop in direct_properties(endpoint) if prop.startswith("data-lanes = ")
        ]
        self.assertEqual(
            lane_properties,
            ["data-lanes = <1 2>;"],
            f"{header} must use CSI-2 lanes 1 and 2",
        )

    # Catches a CAM0 status flip or CAM0 resources moved onto the CAM1 sensor/I2C node.
    def test_cam0_enabled_path(self):
        self.assert_node_properties(
            self.camera,
            "ov5647_xclk_25m: ov5647-xclk-25m",
            (
                'compatible = "fixed-clock";',
                "#clock-cells = <0>;",
                "clock-frequency = <25000000>;",
                'clock-output-names = "ov5647_xclk_25m";',
            ),
        )
        i2c4 = self.assert_node_properties(
            self.camera,
            "&i2c4",
            (
                "clock-frequency = <400000>;",
                'pinctrl-names = "default";',
                "pinctrl-0 = <&i2c4m2_xfer>;",
            ),
            status="okay",
        )
        self.assert_node_properties(
            i2c4,
            "ov5647_cam0: camera@36",
            (
                'compatible = "ovti,ov5647";',
                "reg = <0x36>;",
                "clocks = <&ov5647_xclk_25m>;",
                "vdd-supply = <&vdd_cam0_3v3>;",
                "rockchip,camera-module-index = <0>;",
                'rockchip,camera-module-facing = "back";',
                'rockchip,camera-module-name = "HBV-RPI-AUTO-IRCUT-3.6-S1.0";',
                'rockchip,camera-module-lens-name = "3.6mm";',
            ),
            status="okay",
        )
        for header in (
            "&csi2_dphy_hw",
            "&csi2_dphy1",
            "&mipi0_csi2",
            "&rkcif",
            "&rkcif_mipi_lvds",
            "&rkcif_mipi_lvds_sditf",
            "&rkisp",
            "&rkisp_vir0",
        ):
            self.assert_node_properties(self.camera, header, (), status="okay")
        self.assert_node_properties(
            self.camera,
            "&rkcif",
            ('pinctrl-names = "default";', "pinctrl-0 = <&mipi_pins>;"),
            status="okay",
        )

    # Catches a CAM1 status flip or CAM1 address/pinmux/power/module metadata moved to CAM0.
    def test_cam1_reserved_disabled(self):
        i2c3 = self.assert_node_properties(
            self.camera,
            "&i2c3",
            (
                "clock-frequency = <400000>;",
                'pinctrl-names = "default";',
                "pinctrl-0 = <&i2c3m2_xfer>;",
            ),
            status="disabled",
        )
        self.assert_node_properties(
            i2c3,
            "ov5647_cam1: camera@36",
            (
                'compatible = "ovti,ov5647";',
                "reg = <0x36>;",
                "clocks = <&ov5647_xclk_25m>;",
                "vdd-supply = <&vdd_cam1_3v3>;",
                "rockchip,camera-module-index = <1>;",
                'rockchip,camera-module-facing = "front";',
                'rockchip,camera-module-name = "HBV-RPI-AUTO-IRCUT-3.6-S1.0";',
                'rockchip,camera-module-lens-name = "3.6mm";',
            ),
            status="disabled",
        )
        for header in (
            "&csi2_dphy2",
            "&mipi1_csi2",
            "&rkcif_mipi_lvds1",
            "&rkcif_mipi_lvds1_sditf",
            "&rkisp_vir1",
        ):
            self.assert_node_properties(self.camera, header, (), status="disabled")

    # Catches a one-way/wrong remote endpoint, a missing lane list, or MIPI local-reg drift.
    def test_mipi_endpoint_reg_has_local_address_cells(self):
        reciprocal_pairs = (
            ("ov5647_cam0_out: endpoint", "csi_dphy1_input", "csi_dphy1_input: endpoint", "ov5647_cam0_out"),
            ("csi_dphy1_output: endpoint", "mipi0_csi2_input", "mipi0_csi2_input: endpoint@1", "csi_dphy1_output"),
            ("mipi0_csi2_output: endpoint@0", "cif_mipi_in0", "cif_mipi_in0: endpoint", "mipi0_csi2_output"),
            ("mipi_lvds0_sditf: endpoint", "isp_in0", "isp_in0: endpoint", "mipi_lvds0_sditf"),
            ("ov5647_cam1_out: endpoint", "csi_dphy2_input", "csi_dphy2_input: endpoint", "ov5647_cam1_out"),
            ("csi_dphy2_output: endpoint", "mipi1_csi2_input", "mipi1_csi2_input: endpoint@1", "csi_dphy2_output"),
            ("mipi1_csi2_output: endpoint@0", "cif_mipi_in1", "cif_mipi_in1: endpoint", "mipi1_csi2_output"),
            ("mipi_lvds1_sditf: endpoint", "isp_in1", "isp_in1: endpoint", "mipi_lvds1_sditf"),
        )
        for left, left_remote, right, right_remote in reciprocal_pairs:
            self.assert_endpoint_remote(left, left_remote)
            self.assert_endpoint_remote(right, right_remote)

        for endpoint in (
            "ov5647_cam0_out: endpoint",
            "csi_dphy1_input: endpoint",
            "ov5647_cam1_out: endpoint",
            "csi_dphy2_input: endpoint",
        ):
            self.assert_endpoint_lanes(endpoint)

        for mipi, input_reg, output_reg in (("mipi0_csi2", 1, 0), ("mipi1_csi2", 1, 0)):
            mipi_block = node_block(self.camera, f"&{mipi}")
            ports = node_block(mipi_block, "ports")
            for port_reg, endpoint_reg in ((0, input_reg), (1, output_reg)):
                port = node_block(ports, f"port@{port_reg}")
                properties = direct_properties(port)
                self.assertIn("#address-cells = <1>;", properties)
                self.assertIn("#size-cells = <0>;", properties)
                self.assertEqual(
                    [prop for prop in properties if prop.startswith("reg = ")],
                    [f"reg = <{port_reg}>;"],
                )
                endpoint = node_block(port, f"mipi{mipi[-6]}_csi2_{'input' if port_reg == 0 else 'output'}: endpoint@{endpoint_reg}")
                self.assertEqual(
                    [prop for prop in direct_properties(endpoint) if prop.startswith("reg = ")],
                    [f"reg = <{endpoint_reg}>;"],
                )

    # Catches reserved CAM pins reintroduced directly or through camera control aliases/properties.
    def test_unverified_control_gpio_not_bound(self):
        for forbidden_pin in ("RK_PA2", "RK_PA4", "RK_PA5", "RK_PA7"):
            self.assertNotRegex(self.camera, rf"\b{forbidden_pin}\b")
        self.assertNotRegex(
            self.camera,
            r"(?i)(?<![a-z0-9])cam[01][_-](?:gpio|led[_-]on)(?![a-z0-9])",
        )
        self.assertNotRegex(
            self.camera,
            r"(?im)^\s*(?:[a-z0-9-]+,)?[a-z0-9-]*(?:pwdn|power-?down|reset|ir-?cut|infrared|uvc|led)[a-z0-9-]*\s*(?:=|;)",
        )

    def test_current_driver_is_not_accepted_as_complete(self):
        driver = read_text(DRIVER)
        self.assertIn("MEDIA_BUS_FMT_SBGGR10_1X10", driver)
        self.assertIn("ov5647_1080p30_10bpp", driver)
        self.assertIn("RKMODULE_GET_MODULE_INFO", driver)

    def test_ov5647_mode_and_control_contract(self):
        driver = re.sub(
            r"/\*.*?\*/|//[^\n]*", "", read_text(DRIVER), flags=re.DOTALL
        )
        for token in (
            "ov5647_2592x1944_10bpp",
            "ov5647_1080p30_10bpp",
            "ov5647_2x2binned_10bpp",
            "ov5647_640x480_10bpp",
            "MEDIA_BUS_FMT_SBGGR10_1X10",
            "V4L2_CID_EXPOSURE",
            "V4L2_CID_ANALOGUE_GAIN",
            "V4L2_CID_VBLANK",
            "V4L2_CID_LINK_FREQ",
            "V4L2_CID_PIXEL_RATE",
            "V4L2_CID_TEST_PATTERN",
            "enum_frame_interval",
            "get_mbus_config",
            "pm_runtime_resume_and_get",
            "SET_RUNTIME_PM_OPS",
            "RKMODULE_GET_MODULE_INFO",
            "RKMODULE_GET_HDR_CFG",
            "RKMODULE_GET_CHANNEL_INFO",
            "devm_regulator_get_optional",
            "regulator_enable",
            "v4l2_async_register_subdev_sensor_common",
        ):
            self.assertIn(token, driver)
        self.assertIn("struct v4l2_subdev_pad_config", driver)
        self.assertNotIn("struct v4l2_subdev_state", driver)
        self.assertNotIn("MEDIA_BUS_FMT_SBGGR8_1X8", driver)

        modes = (
            ("ov5647_2592x1944_10bpp", 2592, 1944, 87500000, 2844,
             "0x7b0", 218750000, 1, 15),
            ("ov5647_1080p30_10bpp", 1920, 1080, 81666700, 2416,
             "0x450", 204166750, 1, 30),
            ("ov5647_2x2binned_10bpp", 1296, 972, 81666700, 1896,
             "0x59b", 204166750, 1, 30),
            ("ov5647_640x480_10bpp", 640, 480, 55000000, 1852,
             "0x1f8", 137500000, 1, 60),
        )
        for name, width, height, pixel_rate, hts, vts, link_freq, num, den in modes:
            pattern = rf"""
                \.format\s*=\s*\{{
                (?:(?!\.reg_list).)*?\.code\s*=\s*MEDIA_BUS_FMT_SBGGR10_1X10
                (?:(?!\.reg_list).)*?\.width\s*=\s*{width}
                (?:(?!\.reg_list).)*?\.height\s*=\s*{height}
                (?:(?!\.reg_list).)*?\.pixel_rate\s*=\s*{pixel_rate}
                (?:(?!\.reg_list).)*?\.hts\s*=\s*{hts}
                (?:(?!\.reg_list).)*?\.vts\s*=\s*{vts}
                (?:(?!\.reg_list).)*?\.link_freq\s*=\s*{link_freq}
                (?:(?!\.reg_list).)*?\.frame_interval\s*=\s*\{{
                \s*\.numerator\s*=\s*{num}\s*,
                \s*\.denominator\s*=\s*{den}\s*,?\s*\}}
                (?:(?!\.reg_list).)*?\.reg_list\s*=\s*{name}\b
                (?:(?!\}}).)*?\.num_regs\s*=\s*ARRAY_SIZE\({name}\)
            """
            self.assertRegex(driver, re.compile(pattern, re.DOTALL | re.VERBOSE))

    def test_ov5647_register_tables_have_verified_geometry(self):
        driver = re.sub(
            r"/\*.*?\*/|//[^\n]*", "", read_text(DRIVER), flags=re.DOTALL
        )
        expected_geometry = {
            "ov5647_2592x1944_10bpp": {
                (0x3808, 0x0A), (0x3809, 0x20), (0x380A, 0x07),
                (0x380B, 0x98), (0x380C, 0x0B), (0x380D, 0x1C),
            },
            "ov5647_1080p30_10bpp": {
                (0x3808, 0x07), (0x3809, 0x80), (0x380A, 0x04),
                (0x380B, 0x38), (0x380C, 0x09), (0x380D, 0x70),
            },
            "ov5647_2x2binned_10bpp": {
                (0x3808, 0x05), (0x3809, 0x10), (0x380A, 0x03),
                (0x380B, 0xCC), (0x380C, 0x07), (0x380D, 0x68),
            },
            "ov5647_640x480_10bpp": {
                (0x3808, 0x02), (0x3809, 0x80), (0x380A, 0x01),
                (0x380B, 0xE0), (0x380C, 0x07), (0x380D, 0x3C),
            },
        }
        for name, geometry in expected_geometry.items():
            match = re.search(
                rf"static(?:\s+const)?\s+struct\s+regval_list\s+{name}"
                rf"\s*\[\s*\]\s*=\s*\{{(?P<body>.*?)\n\}};",
                driver,
                re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            pairs = {
                (int(reg, 16), int(value, 16))
                for reg, value in re.findall(
                    r"\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}",
                    match.group("body"),
                )
            }
            self.assertGreaterEqual(len(pairs), 40, name)
            self.assertTrue({(0x0100, 0x00), (0x0103, 0x01)}.issubset(pairs), name)
            self.assertTrue(geometry.issubset(pairs), name)

    def test_ov5647_binding_matches_formal_module(self):
        binding = read_text(BINDING)
        for token in (
            "vdd-supply:",
            "rockchip,camera-module-index:",
            "rockchip,camera-module-facing:",
            "rockchip,camera-module-name:",
            "rockchip,camera-module-lens-name:",
            "data-lanes:",
        ):
            self.assertIn(token, binding)


if __name__ == "__main__":
    unittest.main()
