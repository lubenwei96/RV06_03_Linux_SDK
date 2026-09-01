import re
import unittest

from tests.aicpm_bsp.common import read_text


DTS_PATH = "sysdrv/source/kernel/arch/arm/boot/dts/rv1106g-aicpm-v1.dts"
DTS_MAKEFILE = "sysdrv/source/kernel/arch/arm/boot/dts/Makefile"
VENDOR_PREFIXES = "sysdrv/source/kernel/Documentation/devicetree/bindings/vendor-prefixes.yaml"
BOARD_SCHEMA = "sysdrv/source/kernel/Documentation/devicetree/bindings/arm/aicpm.yaml"


class DtsContract(unittest.TestCase):
    def setUp(self):
        self.dts = read_text(DTS_PATH)
        self.dts_makefile = read_text(DTS_MAKEFILE)

    def node_body(self, node):
        match = re.search(
            rf"{re.escape(node)}\s*\{{(?P<body>[^{{}}]*)\}};",
            self.dts,
        )
        self.assertIsNotNone(match, f"missing DTS node {node}")
        return match.group("body")

    def test_independent_board(self):
        self.assertIn('#include "rv1106.dtsi"', self.dts)
        self.assertIn('#include "rv1106g-aicpm-v1-camera.dtsi"', self.dts)
        for forbidden in (
            "rv1106g-lubancat-rv06",
            "screen-rgb",
            "pca9535",
            "gpio5",
            "AIC8800",
            "wlan-platdata",
            "pwm-backlight",
            "goodix",
        ):
            self.assertNotIn(forbidden, self.dts)

    def test_single_fiq_console_at_115200(self):
        self.assertNotIn("stdout-path", self.dts)
        self.assertIn(
            'earlycon=uart8250,mmio32,0xff4c0000,115200n8 console=ttyFIQ0',
            self.dts,
        )
        self.assertIn("&fiq_debugger", self.dts)
        self.assertIn("rockchip,baudrate = <115200>", self.dts)
        self.assertNotIn("1500000", self.dts)

    def test_power_and_boot_media(self):
        self.assertIn('regulator-name = "vcc5v0_sys"', self.dts)
        self.assertGreaterEqual(self.dts.count("<5000000>"), 2)
        vcc_wlan = self.node_body("vcc_wlan: vcc-wlan")
        # Catches a descriptor-polarity mutation to active high or another GPIO.
        self.assertIn("gpio = <&gpio1 RK_PA0 GPIO_ACTIVE_LOW>", vcc_wlan)
        # Catches removal of the USB-enumeration supply's always-on policy.
        self.assertIn("regulator-always-on", vcc_wlan)
        # Catches removal of the USB-enumeration supply's boot-on policy.
        self.assertIn("regulator-boot-on", vcc_wlan)
        # Catches the fixed-regulator property that inverts descriptor polarity.
        self.assertNotIn("enable-active-high", vcc_wlan)
        # Retained guard for the non-binding property rejected by the contract.
        self.assertNotIn("enable-active-low", self.dts)
        self.assertIn('compatible = "spi-nand"', self.dts)
        self.assertIn("spi-max-frequency = <24000000>", self.dts)
        self.assertIn("max-frequency = <50000000>", self.dts)
        self.assertIn("no-1-8-v", self.dts)
        self.assertNotIn("mmcblk0p5", self.dts)
        self.assertNotIn("rootfstype=ext4", self.dts)

    def test_power_and_dtb_registration(self):
        self.assertIn("rv1106g-aicpm-v1.dtb", self.dts_makefile)
        self.assertNotIn("vdd_0v9", self.dts)
        self.assertNotIn("cpu-supply", self.dts)

    def test_board_compatible_is_registered(self):
        self.assertIn('"^aicpm,.*"', read_text(VENDOR_PREFIXES))
        schema = read_text(BOARD_SCHEMA)
        self.assertIn("const: aicpm,rv1106g-v1", schema)
        self.assertIn("const: rockchip,rv1106", schema)

    def test_product_keys_leds_and_pa(self):
        for token in (
            "<&gpio3 RK_PC6 GPIO_ACTIVE_LOW>",
            "<&gpio3 RK_PC4 GPIO_ACTIVE_LOW>",
            "linux,code = <KEY_F13>",
            "linux,code = <KEY_SETUP>",
            "<&gpio1 RK_PA4 GPIO_ACTIVE_HIGH>",
            "<&gpio1 RK_PA1 GPIO_ACTIVE_HIGH>",
            "<&gpio1 RK_PA3 GPIO_ACTIVE_HIGH>",
            "pa-ctl-gpios = <&gpio4 RK_PA7 GPIO_ACTIVE_HIGH>",
        ):
            self.assertIn(token, self.dts)
        self.assertEqual(self.dts.count("linux,code ="), 2)
        self.assertNotIn('compatible = "adc-keys"', self.dts)

    def test_usb_host_and_frozen_unsafe_resources(self):
        self.assertIn('dr_mode = "host"', self.dts)
        pwm5 = self.node_body("&pwm5")
        pwm6 = self.node_body("&pwm6")
        # Catches PWM5 being enabled while another disabled node masks it.
        self.assertIn('status = "disabled"', pwm5)
        # Catches PWM6 being enabled while another disabled node masks it.
        self.assertIn('status = "disabled"', pwm6)
        # Catches an L9110S motor-driver consumer being added to this release.
        self.assertNotRegex(self.dts, r'(?i)compatible\s*=\s*"l9110s"')
        # Catches a consumer that reuses either safety-disabled PWM output.
        self.assertNotRegex(self.dts, r"pwms\s*=\s*<&pwm[56]\b")
        # Catches an unreviewed CAM GPIO/LED function mapping in the base DTS.
        self.assertNotRegex(self.dts, r"(?i)cam[01]_(gpio|led_on)\b")
        self.assertIn("j9_feed_detect_gpio: j9-feed-detect-gpio", self.dts)
        self.assertIn("<0 RK_PA4 RK_FUNC_GPIO &pcfg_pull_none>", self.dts)
        self.assertIn("j11_bin_present_gpio: j11-bin-present-gpio", self.dts)
        self.assertIn("<1 RK_PB0 RK_FUNC_GPIO &pcfg_pull_none>", self.dts)
        self.assertNotIn("gpios = <&gpio0 RK_PA4", self.dts)
        self.assertNotIn("gpios = <&gpio1 RK_PB0", self.dts)


if __name__ == "__main__":
    unittest.main()
