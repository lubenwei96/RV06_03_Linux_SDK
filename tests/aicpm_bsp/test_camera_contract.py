import hashlib
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


def c_function_body(text, name):
    match = re.search(
        rf"\b{re.escape(name)}\s*\([^;]*?\)\s*\{{",
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing C function: {name}")

    depth = 1
    for index in range(match.end(), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.end() : index]
    raise AssertionError(f"unterminated C function: {name}")


def strip_c_comments(text):
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.DOTALL)


def c_calls(text, name_pattern):
    calls = []
    for match in re.finditer(rf"\b({name_pattern})\s*\(", text):
        depth = 1
        for index in range(match.end(), len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    calls.append((match.group(1), text[match.end() : index]))
                    break
        else:
            raise AssertionError(f"unterminated C call: {match.group(1)}")
    return calls


def c_case_body(text, control_id):
    match = re.search(
        rf"\bcase\s+{re.escape(control_id)}\s*:(?P<body>.*?\bbreak\s*;)",
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing control case: {control_id}")
    return match.group("body")


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

    # Catches a one-way/wrong CAM0 remote endpoint, a missing CAM0 lane list, or MIPI0 local-reg drift.
    def test_cam0_mipi_endpoint_reg_has_local_address_cells(self):
        reciprocal_pairs = (
            ("ov5647_cam0_out: endpoint", "csi_dphy1_input", "csi_dphy1_input: endpoint", "ov5647_cam0_out"),
            ("csi_dphy1_output: endpoint", "mipi0_csi2_input", "mipi0_csi2_input: endpoint@1", "csi_dphy1_output"),
            ("mipi0_csi2_output: endpoint@0", "cif_mipi_in0", "cif_mipi_in0: endpoint", "mipi0_csi2_output"),
            ("mipi_lvds0_sditf: endpoint", "isp_in0", "isp_in0: endpoint", "mipi_lvds0_sditf"),
        )
        for left, left_remote, right, right_remote in reciprocal_pairs:
            self.assert_endpoint_remote(left, left_remote)
            self.assert_endpoint_remote(right, right_remote)

        for endpoint in (
            "ov5647_cam0_out: endpoint",
            "csi_dphy1_input: endpoint",
        ):
            self.assert_endpoint_lanes(endpoint)

        for mipi, input_reg, output_reg in (("mipi0_csi2", 1, 0),):
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

    def assert_cam1_has_no_media_graph(self, camera):
        labels = (
            "ov5647_cam1_out",
            "csi_dphy2_input",
            "csi_dphy2_output",
            "mipi1_csi2_input",
            "mipi1_csi2_output",
            "cif_mipi_in1",
            "mipi_lvds1_sditf",
            "isp_in1",
        )
        for label in labels:
            self.assertNotRegex(
                camera,
                rf"\b{label}\s*:",
                f"disabled CAM1 graph label must be absent: {label}",
            )
        for header in (
            "&i2c3",
            "&csi2_dphy2",
            "&mipi1_csi2",
            "&rkcif_mipi_lvds1",
            "&rkcif_mipi_lvds1_sditf",
            "&rkisp_vir1",
        ):
            self.assertNotIn(
                "remote-endpoint",
                node_block(camera, header),
                f"disabled CAM1 block must not contain remote-endpoint: {header}",
            )

    # Catches a disabled CAM1 graph being retained and later becoming a dangling DTB phandle.
    def test_cam1_disabled_path_has_no_media_graph(self):
        self.assert_cam1_has_no_media_graph(self.camera)

    # Proves the no-graph contract rejects an accidental CAM1 endpoint reintroduction.
    def test_cam1_no_graph_contract_rejects_endpoint_mutation(self):
        mutated = self.camera + """
&i2c3 {
	port {
		ov5647_cam1_out: endpoint {
			remote-endpoint = <&csi_dphy2_input>;
		};
	};
};
"""
        with self.assertRaisesRegex(
            AssertionError,
            "disabled CAM1 graph label must be absent: ov5647_cam1_out",
        ):
            self.assert_cam1_has_no_media_graph(mutated)

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
        driver = strip_c_comments(read_text(DRIVER))
        for token in (
            "ov5647_2592x1944_10bpp",
            "ov5647_1080p30_10bpp",
            "ov5647_2x2binned_10bpp",
            "ov5647_640x480_10bpp",
            "MEDIA_BUS_FMT_SBGGR10_1X10",
            "V4L2_CID_EXPOSURE_AUTO",
            "V4L2_CID_EXPOSURE",
            "V4L2_CID_ANALOGUE_GAIN",
            "V4L2_CID_VBLANK",
            "V4L2_CID_HBLANK",
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

        def assert_short_transfer(body, call, count):
            transfer = body.find(call)
            short_check = body.find(f"if (ret != {count})", transfer)
            short_eio = body.find(
                "return ret < 0 ? ret : -EIO", short_check
            )
            self.assertGreaterEqual(transfer, 0, call)
            self.assertGreater(short_check, transfer, call)
            self.assertGreater(short_eio, short_check, call)

        write16 = c_function_body(driver, "ov5647_write16")
        assert_short_transfer(
            write16, "ret = i2c_master_send(client, data, 4)", 4
        )
        write8 = c_function_body(driver, "ov5647_write")
        assert_short_transfer(
            write8, "ret = i2c_master_send(client, data, 3)", 3
        )
        read8 = c_function_body(driver, "ov5647_read")
        assert_short_transfer(
            read8, "ret = i2c_master_send(client, data_w, 2)", 2
        )
        assert_short_transfer(
            read8, "ret = i2c_master_recv(client, val, 1)", 1
        )

        probe = c_function_body(driver, "ov5647_probe")
        get_vdd = probe.find('devm_regulator_get_optional(dev, "vdd")')
        vdd_error = probe.find("if (IS_ERR(sensor->vdd))", get_vdd)
        enodev_only = probe.find("if (ret == -ENODEV)", vdd_error)
        vdd_absent = probe.find("sensor->vdd = NULL", enodev_only)
        vdd_other_error = probe.find("return ret", vdd_absent)
        self.assertGreaterEqual(get_vdd, 0)
        self.assertGreater(vdd_error, get_vdd)
        self.assertGreater(enodev_only, vdd_error)
        self.assertGreater(vdd_absent, enodev_only)
        self.assertGreater(vdd_other_error, vdd_absent)

        compat_ioctl = c_function_body(driver, "ov5647_compat_ioctl32")
        compat_ptr_call = compat_ioctl.find("compat_ptr(arg)")
        channel_copy_in = compat_ioctl.find("copy_from_user(karg, up, size)")
        native_ioctl = compat_ioctl.find("ov5647_ioctl(sd, cmd, karg)")
        result_copy_out = compat_ioctl.find("copy_to_user(up, karg, size)")
        self.assertGreaterEqual(compat_ptr_call, 0)
        self.assertGreater(channel_copy_in, compat_ptr_call)
        self.assertGreater(native_ioctl, channel_copy_in)
        self.assertGreater(result_copy_out, native_ioctl)

        test_menu = re.search(
            r"ov5647_test_pattern_menu\s*\[\s*\]\s*=\s*\{(.*?)\};",
            driver,
            re.DOTALL,
        )
        test_values = re.search(
            r"ov5647_test_pattern_val\s*\[\s*\]\s*=\s*\{(.*?)\};",
            driver,
            re.DOTALL,
        )
        self.assertIsNotNone(test_menu)
        self.assertIsNotNone(test_values)
        self.assertEqual(
            re.findall(r'"([^"]+)"', test_menu.group(1)),
            ["Disabled", "Color Bars", "Color Squares", "Random Data"],
        )
        self.assertEqual(
            [
                int(value, 16)
                for value in re.findall(
                    r"0x[0-9a-fA-F]+", test_values.group(1)
                )
            ],
            [0x00, 0x80, 0x82, 0x81],
        )

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

        init_controls = c_function_body(driver, "ov5647_init_controls")
        save_error = re.search(
            r"\bret\s*=\s*sensor->ctrls\.error\s*;", init_controls
        )
        free_error = init_controls.find("v4l2_ctrl_handler_free(&sensor->ctrls)")
        return_error = init_controls.find("return ret", free_error)
        self.assertIsNotNone(
            save_error,
            "control setup errno must be saved before handler_free clears it",
        )
        self.assertGreater(free_error, save_error.start())
        self.assertGreater(return_error, free_error)
        self.assertNotIn("return sensor->ctrls.error", init_controls[free_error:])

        expected_controls = (
            "V4L2_CID_EXPOSURE_AUTO",
            "V4L2_CID_EXPOSURE",
            "V4L2_CID_ANALOGUE_GAIN",
            "V4L2_CID_VBLANK",
            "V4L2_CID_HBLANK",
            "V4L2_CID_LINK_FREQ",
            "V4L2_CID_PIXEL_RATE",
            "V4L2_CID_TEST_PATTERN",
        )
        creation_calls = c_calls(
            init_controls,
            r"v4l2_ctrl_new_(?:std_menu_items|std_menu|std|int_menu)",
        )
        created_controls = []
        for call_name, arguments in creation_calls:
            control_ids = re.findall(r"\bV4L2_CID_[A-Z_]+\b", arguments)
            self.assertEqual(
                len(control_ids),
                1,
                f"{call_name} must create exactly one named control",
            )
            created_controls.append(control_ids[0])
        self.assertCountEqual(created_controls, expected_controls)
        self.assertEqual(len(created_controls), len(expected_controls))

        s_ctrl = c_function_body(driver, "ov5647_s_ctrl")
        dispatched_controls = re.findall(
            r"\bcase\s+(V4L2_CID_[A-Z_]+)\s*:", s_ctrl
        )
        self.assertCountEqual(dispatched_controls, expected_controls)
        self.assertEqual(len(dispatched_controls), len(expected_controls))
        writable_dispatch = {
            "V4L2_CID_EXPOSURE_AUTO":
                "ov5647_s_exposure_auto(sd, ctrl->val)",
            "V4L2_CID_EXPOSURE":
                "ov5647_s_exposure(sd, ctrl->val)",
            "V4L2_CID_ANALOGUE_GAIN":
                "ov5647_s_analogue_gain(sd, ctrl->val)",
            "V4L2_CID_VBLANK":
                "ov5647_write16(sd, OV5647_REG_VTS_HI",
        }
        for control_id, handler_call in writable_dispatch.items():
            self.assertIn(handler_call, c_case_body(s_ctrl, control_id))
        self.assertRegex(
            s_ctrl,
            re.compile(
                r"case\s+V4L2_CID_LINK_FREQ\s*:\s*"
                r"case\s+V4L2_CID_PIXEL_RATE\s*:\s*"
                r"case\s+V4L2_CID_HBLANK\s*:\s*break\s*;",
                re.DOTALL,
            ),
        )
        test_pattern_case = c_case_body(s_ctrl, "V4L2_CID_TEST_PATTERN")
        test_pattern_write = re.search(
            r"\bret\s*=\s*ov5647_write\s*\(\s*sd\s*,\s*"
            r"(?P<reg>OV5647_REG_[A-Z0-9_]*ISP[A-Z0-9_]*3D)\s*,\s*"
            r"ov5647_test_pattern_val\s*\[\s*ctrl->val\s*\]\s*\)",
            test_pattern_case,
            re.DOTALL,
        )
        self.assertIsNotNone(
            test_pattern_write,
            "TEST_PATTERN must write its selected hardware pattern value",
        )
        test_pattern_register = re.search(
            rf"^\s*#define\s+{test_pattern_write.group('reg')}"
            r"\s+(0x[0-9a-fA-F]+)\b",
            driver,
            re.MULTILINE,
        )
        self.assertIsNotNone(test_pattern_register)
        self.assertEqual(
            int(test_pattern_register.group(1), 16),
            0x503D,
        )

        self.assertIn(
            "sensor->ctrls.lock = &sensor->lock;",
            init_controls,
            "the control handler and sensor state must share one lock domain",
        )
        stream_on = c_function_body(driver, "ov5647_stream_on")
        self.assertIn(
            "__v4l2_ctrl_handler_setup(sd->ctrl_handler)", stream_on
        )
        self.assertNotRegex(
            stream_on, r"(?<!_)v4l2_ctrl_handler_setup\s*\("
        )
        set_mode_call = stream_on.find("ret = ov5647_set_mode(sd)")
        setup_call = stream_on.find(
            "ret = __v4l2_ctrl_handler_setup(sd->ctrl_handler)"
        )
        mipi_stream_write = re.search(
            r"ov5647_write\s*\(\s*sd\s*,\s*"
            r"(?P<reg>OV5647_REG_MIPI_CTRL00)\s*,\s*val\s*\)",
            stream_on,
        )
        frame_stream_write = re.search(
            r"ov5647_write\s*\(\s*sd\s*,\s*"
            r"(?P<reg>OV5647_REG_FRAME_OFF_NUMBER)\s*,\s*0x00\s*\)",
            stream_on,
        )
        self.assertGreaterEqual(set_mode_call, 0)
        self.assertGreater(setup_call, set_mode_call)
        self.assertIsNotNone(mipi_stream_write)
        self.assertIsNotNone(frame_stream_write)
        self.assertGreater(mipi_stream_write.start(), setup_call)
        self.assertGreater(frame_stream_write.start(), mipi_stream_write.start())
        for register_name, expected_address in (
            (mipi_stream_write.group("reg"), 0x4800),
            (frame_stream_write.group("reg"), 0x4202),
        ):
            define = re.search(
                rf"^\s*#define\s+{register_name}\s+(0x[0-9a-fA-F]+)\b",
                driver,
                re.MULTILINE,
            )
            self.assertIsNotNone(define, register_name)
            self.assertEqual(int(define.group(1), 16), expected_address)

        set_pad_fmt = c_function_body(driver, "ov5647_set_pad_fmt")
        for helper in ("s_ctrl", "modify_range"):
            self.assertNotRegex(
                set_pad_fmt, rf"(?<!_)v4l2_ctrl_{helper}\s*\("
            )
        self.assertIn("__v4l2_ctrl_s_ctrl", set_pad_fmt)
        self.assertIn("__v4l2_ctrl_modify_range", set_pad_fmt)

        try_then_active_guard = re.search(
            r"if\s*\(\s*format->which\s*==\s*"
            r"V4L2_SUBDEV_FORMAT_TRY\s*\)\s*\{"
            r"(?P<try_body>.*?)\}\s*else\s+if\s*"
            r"\(\s*sensor->streaming\s*\)",
            set_pad_fmt,
            re.DOTALL,
        )
        self.assertIsNotNone(
            try_then_active_guard,
            "streaming guard must be mutually exclusive with TRY format",
        )
        self.assertIn(
            "v4l2_subdev_get_try_format",
            try_then_active_guard.group("try_body"),
        )
        try_format = set_pad_fmt.find(
            "format->which == V4L2_SUBDEV_FORMAT_TRY"
        )
        busy_guard = set_pad_fmt.find("sensor->streaming")
        active_mode_update = set_pad_fmt.find("sensor->mode = mode")
        self.assertGreaterEqual(try_format, 0)
        self.assertGreater(busy_guard, try_format)
        self.assertGreater(active_mode_update, busy_guard)
        self.assertIn("return -EBUSY", set_pad_fmt[busy_guard:active_mode_update])

        channel_info = c_function_body(driver, "ov5647_get_channel_info")
        channel_lock = channel_info.find("mutex_lock(&sensor->lock)")
        channel_mode = channel_info.find("sensor->mode")
        channel_unlock = channel_info.find("mutex_unlock(&sensor->lock)")
        self.assertGreaterEqual(channel_lock, 0)
        self.assertGreater(channel_mode, channel_lock)
        self.assertGreater(channel_unlock, channel_mode)

        s_stream = c_function_body(driver, "ov5647_s_stream")
        normalize = s_stream.find("enable = !!enable;")
        stream_lock = s_stream.find("mutex_lock(&sensor->lock)")
        same_state = s_stream.find("sensor->streaming == enable")
        self.assertGreaterEqual(normalize, 0)
        self.assertGreater(stream_lock, normalize)
        self.assertGreater(same_state, stream_lock)

        runtime_get = s_stream.find("pm_runtime_resume_and_get(&client->dev)")
        stream_on_call = s_stream.find("ret = ov5647_stream_on(sd)", runtime_get)
        stream_state_set = s_stream.find("sensor->streaming = true", stream_on_call)
        start_rollback = s_stream.find("error_pm:")
        start_pm_put = s_stream.find(
            "pm_runtime_put(&client->dev)", start_rollback
        )
        self.assertGreater(runtime_get, same_state)
        self.assertGreater(stream_on_call, runtime_get)
        self.assertGreater(stream_state_set, stream_on_call)
        self.assertGreater(start_rollback, stream_state_set)
        self.assertGreater(start_pm_put, start_rollback)
        self.assertIn("goto error_pm", s_stream[stream_on_call:stream_state_set])

        stream_off = s_stream.find("ret = ov5647_stream_off(sd)")
        stop_pm_put = s_stream.find("pm_runtime_put(&client->dev)", stream_off)
        stop_state_clear = s_stream.find("sensor->streaming = false", stream_off)
        self.assertGreaterEqual(stream_off, 0)
        self.assertGreater(stop_pm_put, stream_off)
        self.assertGreater(stop_state_clear, stop_pm_put)
        self.assertNotIn("goto ", s_stream[stream_off:stop_pm_put])
        normal_path = s_stream[:s_stream.find("error_pm:")]
        final_unlock = normal_path.rfind("mutex_unlock(&sensor->lock)")
        final_return = normal_path.find("return ret", final_unlock)
        self.assertGreater(final_unlock, stop_state_clear)
        self.assertGreater(final_return, final_unlock)
        self.assertNotIn("return 0", normal_path[final_unlock:])

        power_on = c_function_body(driver, "ov5647_power_on")
        power_on_cleanup = power_on[power_on.find("error_regulator_disable:"):]
        cleanup_disable = power_on_cleanup.find(
            "regulator_ret = regulator_disable(sensor->vdd)"
        )
        cleanup_check = power_on_cleanup.find("if (regulator_ret)", cleanup_disable)
        cleanup_log = power_on_cleanup.find("dev_err(", cleanup_check)
        cleanup_return = power_on_cleanup.find("return ret", cleanup_log)
        self.assertGreaterEqual(cleanup_disable, 0)
        self.assertGreater(cleanup_check, cleanup_disable)
        self.assertGreater(cleanup_log, cleanup_check)
        self.assertGreater(cleanup_return, cleanup_log)
        self.assertNotIn("return regulator_ret", power_on_cleanup)

        power_off = c_function_body(driver, "ov5647_power_off")
        regulator_disable = power_off.find(
            "regulator_ret = regulator_disable(sensor->vdd)"
        )
        disable_failure = power_off.find("if (regulator_ret)", regulator_disable)
        restore_clock = power_off.find(
            "ret = clk_prepare_enable(sensor->xclk)", disable_failure
        )
        restore_log = power_off.find("dev_err(", restore_clock)
        restore_pwdn = power_off.find(
            "gpiod_set_value_cansleep(sensor->pwdn, 0)", restore_clock
        )
        restore_delay = power_off.find("msleep(PWDN_ACTIVE_DELAY_MS)", restore_pwdn)
        rollback_error = power_off.find("return ret", restore_delay)
        original_error = power_off.find("return regulator_ret", rollback_error)
        self.assertGreaterEqual(regulator_disable, 0)
        self.assertGreater(disable_failure, regulator_disable)
        self.assertGreater(restore_clock, disable_failure)
        self.assertGreater(restore_log, restore_clock)
        self.assertGreater(restore_pwdn, restore_clock)
        self.assertGreater(restore_delay, restore_pwdn)
        self.assertGreater(rollback_error, restore_delay)
        self.assertGreater(original_error, rollback_error)
        rollback_path = power_off[restore_clock:]
        self.assertRegex(
            rollback_path,
            re.compile(r"if\s*\(ret\).*?dev_err\(", re.DOTALL),
        )
        self.assertRegex(
            rollback_path,
            re.compile(
                r"if\s*\(ret\)\s*return\s+ret\s*;\s*return\s+regulator_ret",
                re.DOTALL,
            ),
        )

        enum_interval = c_function_body(driver, "ov5647_enum_frame_interval")
        self.assertRegex(
            enum_interval,
            re.compile(
                r"fie->code\s*!=\s*MEDIA_BUS_FMT_SBGGR10_1X10"
                r".*?fie->index\s*!=\s*0",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            enum_interval,
            r"for\s*\(\s*i\s*=\s*0\s*;\s*i\s*<\s*ARRAY_SIZE\(ov5647_modes\)",
        )
        self.assertIn("mode = &ov5647_modes[i]", enum_interval)
        self.assertIn("fie->width != mode->format.width", enum_interval)
        self.assertIn("fie->height != mode->format.height", enum_interval)
        interval_copy = enum_interval.find("fie->interval = mode->frame_interval")
        invalid_size = enum_interval.rfind("return -EINVAL")
        self.assertGreaterEqual(interval_copy, 0)
        self.assertGreater(invalid_size, interval_copy)
        self.assertNotIn("&ov5647_modes[fie->index]", enum_interval)
        self.assertNotRegex(
            enum_interval,
            r"fie->(?:code|width|height)\s*=(?!=)",
        )

    def test_ov5647_register_tables_match_ordered_upstream_fingerprints(self):
        driver = strip_c_comments(read_text(DRIVER))
        expected_fingerprints = {
            "ov5647_2592x1944_10bpp":
                (86, "f19251e277fc1f3c8bf312c502c2f2420ca823007cfcf5151ee13043c9a6533e"),
            "ov5647_1080p30_10bpp":
                (86, "ae5dd78b7e71c7beaf0b38e5f9ce202d5ea3b73b800db991cd11d2f3ff7fae5c"),
            "ov5647_2x2binned_10bpp":
                (90, "963300a50cb3b67a42623f222ef4cd64aa607f4740002918d2251ffbe2f42507"),
            "ov5647_640x480_10bpp":
                (87, "3b472e1567de196350f3661fdaca5853b504ed7668a6c8207b0f41a4112c0efc"),
        }
        for name, (expected_count, expected_digest) in expected_fingerprints.items():
            match = re.search(
                rf"static(?:\s+const)?\s+struct\s+regval_list\s+{name}"
                rf"\s*\[\s*\]\s*=\s*\{{(?P<body>.*?)\n\}};",
                driver,
                re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            pairs = [
                (int(reg, 16), int(value, 16))
                for reg, value in re.findall(
                    r"\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}",
                    match.group("body"),
                )
            ]
            canonical = ";".join(
                f"{reg:04x}:{value:02x}" for reg, value in pairs
            )
            self.assertEqual(len(pairs), expected_count, name)
            self.assertEqual(
                hashlib.sha256(canonical.encode()).hexdigest(),
                expected_digest,
                name,
            )

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

        required_match = re.search(
            r"^required:\s*\n(?P<body>(?:\s+-\s+\S+\s*\n)+)",
            binding,
            re.MULTILINE,
        )
        self.assertIsNotNone(required_match)
        required = re.findall(
            r"^\s+-\s+(\S+)\s*$",
            required_match.group("body"),
            re.MULTILINE,
        )
        self.assertEqual(required, ["compatible", "reg", "clocks", "port"])
        for optional in (
            "vdd-supply",
            "rockchip,camera-module-index",
            "rockchip,camera-module-facing",
            "rockchip,camera-module-name",
            "rockchip,camera-module-lens-name",
        ):
            self.assertNotIn(optional, required)

        lanes = re.search(
            r"(?ms)^[ ]{10}data-lanes:\s*\n(?P<body>.*?)(?=^[ ]{10}\S)",
            binding,
        )
        self.assertIsNotNone(lanes)
        lane_values = [
            int(value) for value in re.findall(r"- const:\s*(\d+)", lanes.group("body"))
        ]
        self.assertEqual(lane_values, [1, 2])


if __name__ == "__main__":
    unittest.main()
