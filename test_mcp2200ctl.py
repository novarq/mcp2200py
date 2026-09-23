import importlib.util
import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout


MODULE_PATH = Path(__file__).with_name("mcp2200ctl.py")
SPEC = importlib.util.spec_from_file_location("mcp2200ctl", MODULE_PATH)
assert SPEC and SPEC.loader
mcp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mcp
SPEC.loader.exec_module(mcp)


class ProtocolTests(unittest.TestCase):
    def test_no_arguments_prints_help(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mcp.main([]), 0)
        self.assertIn("get-config", output.getvalue())

    def test_write_commands_without_arguments_print_command_help(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mcp.main(["set-gpio"]), 0)
        self.assertIn("--set SET", output.getvalue())

    def test_boot_mode_without_argument_prints_mode_help(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mcp.main(["set-tactical-1000-boot-mode"]), 0)
        self.assertIn("qspi0", output.getvalue())
        self.assertIn("reset the board", output.getvalue().lower())

    def test_list_uses_hidraw_when_hidapi_is_unavailable(self):
        original_hid = mcp.Device._hid
        original_find = mcp.Device._find_hidraw_paths
        mcp.Device._hid = staticmethod(lambda: (_ for _ in ()).throw(mcp.MCP2200Error("missing hidapi")))
        mcp.Device._find_hidraw_paths = lambda _self: ["/dev/hidraw7"]
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                result = mcp.command_list(type("Arguments", (), {"vid": 0x04D8, "pid": 0x00DF})())
        finally:
            mcp.Device._hid = original_hid
            mcp.Device._find_hidraw_paths = original_find
        self.assertEqual(result, 0)
        self.assertIn('"backend": "hidraw"', output.getvalue())

    def test_open_resolves_hid_path_before_opening(self):
        opened_paths = []

        class FakeHandle:
            def open_path(self, path):
                opened_paths.append(path)

            def close(self):
                pass

        class FakeHid:
            @staticmethod
            def device():
                return FakeHandle()

            @staticmethod
            def enumerate(vid, pid):
                self.assertEqual((vid, pid), (0x04D8, 0x00DF))
                return [{"path": b"7-5:1.2", "serial_number": ""}]

        original_hid = mcp.Device._hid
        mcp.Device._hid = staticmethod(lambda: FakeHid)
        try:
            with mcp.Device(None, 0x04D8, 0x00DF, None, 1000):
                pass
        finally:
            mcp.Device._hid = original_hid
        self.assertEqual(opened_paths, [b"7-5:1.2"])

    def test_read_all_decoding_and_configure_encoding(self):
        response = [0x80, 0x12, 0, 0x34, 0xA5, 0xCC, 0x3C, 0x63, 0, 103, 0x77, 0, 0, 0, 0, 0]
        config = mcp.Configuration.from_read_all(response)
        self.assertEqual(config.baud_rate, 115_384)
        self.assertEqual(config.eeprom_address, 0x12)
        self.assertEqual(config.eeprom_value, 0x34)
        self.assertEqual(
            list(config.configure_report()),
            [0x10, 0, 0, 0, 0xA5, 0xCC, 0x3C, 0x63, 0, 103, 0, 0, 0, 0, 0, 0],
        )

    def test_read_all_rejects_wrong_opcode(self):
        with self.assertRaises(mcp.MCP2200Error):
            mcp.Configuration.from_read_all([0] * 16)

    def test_hex_binary_rendering(self):
        self.assertEqual(mcp.hex_binary(0x3F), "0x3F (0b00111111)")
        self.assertEqual(mcp.hex_binary(103, 16), "0x0067 (0b0000000001100111)")

    def test_set_clear_gpio_report_layout(self):
        self.assertEqual(
            list(mcp.report(mcp.CMD_SET_CLEAR_OUTPUTS, {11: 0x04, 12: 0x08})),
            [0x08, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x04, 0x08, 0, 0, 0],
        )

    def test_gpio_role_decoding(self):
        config = mcp.Configuration(0x3F, 0x0C, 0, 0, 0, 0, 0, 0)
        self.assertEqual(mcp.gpio_role(config, 6), "RxLED (USB receive)")
        self.assertEqual(mcp.gpio_role(config, 2), "GPIO")

    def test_eeprom_dump_layout(self):
        output = io.StringIO()
        with redirect_stdout(output):
            mcp.print_eeprom_dump(0x10, [0, 1, 0xFE], False)
        self.assertIn("10       00 01 FE", output.getvalue())

    def test_novarq_tactical_1000_profile(self):
        existing = mcp.Configuration(0xFF, 0xFF, 0xFF, 0xC3, 1249, 0, 0, 0)
        profile = mcp.novarq_tactical_1000_config(existing)
        self.assertEqual((profile.io_directions, profile.alt_pins, profile.default_values), (0x02, 0x0C, 0x01))
        self.assertEqual((profile.alt_options, profile.baud_divisor), (0x20, 103))

    def test_novarq_yes_option(self):
        args = mcp.build_parser().parse_args(["init-novarq-tactical-1000", "--yes"])
        self.assertTrue(args.yes)

    def test_tactical_1000_boot_mode_mapping(self):
        self.assertEqual(mcp.boot_mode("qspi0"), 0x4)
        self.assertEqual(mcp.boot_mode("0xb"), 0xB)
        self.assertEqual(mcp.tactical_1000_boot_masks(0x4), (0x10, 0x2C))

    def test_reset_duration_validation(self):
        self.assertEqual(mcp.reset_duration_ms("100"), 100)
        with self.assertRaises(Exception):
            mcp.reset_duration_ms("0")

    def test_updated_config_preserves_unspecified_values(self):
        current = mcp.Configuration(1, 2, 3, 4, 5, 6, 7, 8)
        args = type("Arguments", (), {"io": None, "alt_pins": None, "default_values": 0xFE, "alt_options": None, "baud": 9600})()
        desired = mcp.updated_config(current, args)
        self.assertEqual((desired.io_directions, desired.alt_pins, desired.default_values, desired.alt_options), (1, 2, 0xFE, 4))
        self.assertEqual(desired.baud_divisor, 1249)


if __name__ == "__main__":
    unittest.main()
