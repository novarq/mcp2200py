#!/usr/bin/env python3
"""Linux command-line configurator for the Microchip MCP2200 HID interface.

The protocol is documented in Microchip TB3066 (DS93066A).  All commands and
responses are 16-byte HID reports; this program deliberately sends those
reports unchanged (there is no separate report-ID byte on the MCP2200).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import select
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


REPORT_SIZE = 16
CMD_CONFIGURE = 0x10
CMD_SET_CLEAR_OUTPUTS = 0x08
CMD_READ_EEPROM = 0x20
CMD_WRITE_EEPROM = 0x40
CMD_READ_ALL = 0x80
DEFAULT_VID = 0x04D8
DEFAULT_PID = 0x00DF

# LAN969x VCORE[3:0] strapping values. VCORE0 is the least-significant bit.
BOOT_MODES: dict[int, tuple[str, str]] = {
    0x0: ("emmc-trace", "eMMC boot with FlexCOM0 trace"),
    0x1: ("qspi0-trace", "QSPI0 boot with FlexCOM0 trace"),
    0x2: ("sdcard-trace", "SD-card boot with FlexCOM0 trace"),
    0x3: ("emmc", "eMMC boot"),
    0x4: ("qspi0", "QSPI0 boot"),
    0x5: ("sdcard", "SD-card boot"),
    0x8: ("qspi0-hs-trace", "QSPI0-HS boot with FlexCOM0 trace"),
    0xA: ("tfa-monitor", "TF-A monitor on FlexCOM0"),
    0xB: ("tfa-monitor-hs", "TF-A monitor on high-speed FlexCOM0"),
    0xD: ("qspi0-hs", "QSPI0-HS boot"),
    0xF: ("spi-client", "SPI client; LAN969x internal CPU disabled"),
}
BOOT_MODE_NAMES = {name: value for value, (name, _) in BOOT_MODES.items()}


class MCP2200Error(RuntimeError):
    """A transport or protocol error from an MCP2200."""


def integer(value: str) -> int:
    """Parse decimal or 0x-prefixed command-line integers."""
    return int(value, 0)


def byte(value: str) -> int:
    result = integer(value)
    if not 0 <= result <= 0xFF:
        raise argparse.ArgumentTypeError("must be in the range 0..255")
    return result


def baud(value: str) -> int:
    result = integer(value)
    if result <= 0 or not 0 <= (12_000_000 // result - 1) <= 0xFFFF:
        raise argparse.ArgumentTypeError("must produce a 16-bit MCP2200 baud divisor")
    return result


def eeprom_length(value: str) -> int:
    result = integer(value)
    if not 1 <= result <= 256:
        raise argparse.ArgumentTypeError("must be in the range 1..256")
    return result


def reset_duration_ms(value: str) -> int:
    result = integer(value)
    if not 1 <= result <= 5_000:
        raise argparse.ArgumentTypeError("must be in the range 1..5000 ms")
    return result


def boot_mode(value: str) -> int:
    normalized = value.lower().replace("_", "-")
    if normalized in BOOT_MODE_NAMES:
        return BOOT_MODE_NAMES[normalized]
    try:
        numeric = integer(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"unknown boot mode: {value}") from error
    if numeric not in BOOT_MODES:
        raise argparse.ArgumentTypeError("unsupported/reserved boot mode")
    return numeric


def report(command: int, values: dict[int, int] | None = None) -> bytearray:
    """Build a zero-filled 16-byte MCP2200 command report."""
    result = bytearray(REPORT_SIZE)
    result[0] = command
    for index, value in (values or {}).items():
        if not 0 <= index < REPORT_SIZE or not 0 <= value <= 0xFF:
            raise ValueError("invalid MCP2200 report byte")
        result[index] = value
    return result


def hex_binary(value: int, width: int = 8) -> str:
    """Render an unsigned field in the two useful hardware-facing bases."""
    digits = (width + 3) // 4
    return f"0x{value:0{digits}X} (0b{value:0{width}b})"


def named_bits(value: int, names: dict[int, tuple[str, str]]) -> str:
    """Return comma-separated names selected by a bitmap."""
    return ", ".join(f"{name}={set_name if value & (1 << bit) else clear_name}" for bit, (name, set_name, clear_name) in names.items())


@dataclass(frozen=True)
class Configuration:
    """The persistent configuration fields returned by READ_ALL."""

    io_directions: int
    alt_pins: int
    default_values: int
    alt_options: int
    baud_divisor: int
    gpio_values: int
    eeprom_address: int
    eeprom_value: int

    @property
    def baud_rate(self) -> int:
        return 12_000_000 // (self.baud_divisor + 1)

    @classmethod
    def from_read_all(cls, response: Sequence[int]) -> "Configuration":
        validate_response(response, CMD_READ_ALL)
        return cls(
            io_directions=response[4],
            alt_pins=response[5],
            default_values=response[6],
            alt_options=response[7],
            baud_divisor=(response[8] << 8) | response[9],
            gpio_values=response[10],
            eeprom_address=response[1],
            eeprom_value=response[3],
        )

    def configure_report(self) -> bytearray:
        return report(
            CMD_CONFIGURE,
            {
                4: self.io_directions,
                5: self.alt_pins,
                6: self.default_values,
                7: self.alt_options,
                8: self.baud_divisor >> 8,
                9: self.baud_divisor & 0xFF,
            },
        )

    def display(self) -> dict[str, Any]:
        directions = named_bits(
            self.io_directions,
            {bit: (f"GP{bit}", "input", "output") for bit in range(7, -1, -1)},
        )
        defaults = named_bits(
            self.default_values,
            {bit: (f"GP{bit}", "high", "low") for bit in range(7, -1, -1)},
        )
        levels = named_bits(
            self.gpio_values,
            {bit: (f"GP{bit}", "high", "low") for bit in range(7, -1, -1)},
        )
        roles = {
            "GP0": "SSPND: USB suspended indicator" if self.alt_pins & 0x80 else "GPIO",
            "GP1": "USBCFG: USB configured indicator" if self.alt_pins & 0x40 else "GPIO",
            "GP2": "GPIO",
            "GP3": "GPIO",
            "GP4": "GPIO",
            "GP5": "GPIO",
            "GP6": "RxLED: USB receive indicator" if self.alt_pins & 0x08 else "GPIO",
            "GP7": "TxLED: USB transmit indicator" if self.alt_pins & 0x04 else "GPIO",
        }
        options = {
            "hardware flow control (RTS/CTS)": "enabled" if self.alt_options & 0x01 else "disabled",
            "UART/RTS/CTS signal inversion": "enabled" if self.alt_options & 0x02 else "disabled",
            "LED blink period": "slow (200 ms)" if self.alt_options & 0x20 else "fast (100 ms)",
            "TxLED mode": "toggle" if self.alt_options & 0x40 else "blink",
            "RxLED mode": "toggle" if self.alt_options & 0x80 else "blink",
        }
        return {
            "GPIO directions, GP7 through GP0": hex_binary(self.io_directions),
            "GPIO directions decoded": directions,
            "Alternate pin assignments": hex_binary(self.alt_pins),
            "Alternate pin roles": ", ".join(f"{pin}={role}" for pin, role in roles.items()),
            "Default GPIO output levels, GP7 through GP0": hex_binary(self.default_values),
            "Default GPIO output levels decoded": defaults,
            "Special-function options": hex_binary(self.alt_options),
            "Special-function options decoded": ", ".join(f"{name}={state}" for name, state in options.items()),
            "UART baud divisor": hex_binary(self.baud_divisor, 16),
            "Actual UART baud rate": f"{self.baud_rate} (0x{self.baud_rate:X}, 0b{self.baud_rate:b})",
            "Current GPIO levels, GP7 through GP0": hex_binary(self.gpio_values),
            "Current GPIO levels decoded": levels,
            "EEPROM address returned by READ_ALL": hex_binary(self.eeprom_address),
            "EEPROM value returned by READ_ALL": hex_binary(self.eeprom_value),
        }


def validate_response(response: Sequence[int], expected_command: int) -> None:
    if len(response) != REPORT_SIZE:
        raise MCP2200Error(f"expected {REPORT_SIZE}-byte HID response, received {len(response)} bytes")
    if response[0] != expected_command:
        raise MCP2200Error(
            f"unexpected HID response opcode 0x{response[0]:02X}; expected 0x{expected_command:02X}"
        )


class Device:
    """Small adapter around the Python hidapi package."""

    def __init__(self, path: str | None, vid: int, pid: int, serial: str | None, timeout_ms: int):
        self.path = path
        self.vid = vid
        self.pid = pid
        self.serial = serial
        self.timeout_ms = timeout_ms
        self._device: Any = None
        self._hidraw_fd: int | None = None

    @staticmethod
    def _hid() -> Any:
        try:
            import hid  # type: ignore[import-not-found]
        except ImportError as error:
            raise MCP2200Error("Python package 'hidapi' is required; install it with: python3 -m pip install hidapi") from error
        return hid

    def __enter__(self) -> "Device":
        if self.path and self.path.startswith("/dev/hidraw"):
            self._open_hidraw(self.path)
            return self

        hid_error: Exception | None = None
        try:
            hid = self._hid()
            self._device = hid.device()
            # Opening by VID/PID is unreliable with some Linux HIDAPI
            # backends for a composite device.  Enumeration gives us the HID
            # interface path (not the CDC interface), so always open it.
            path = self.path
            if path is None:
                matches = hid.enumerate(self.vid, self.pid)
                if self.serial is not None:
                    matches = [item for item in matches if item.get("serial_number") == self.serial]
                if not matches:
                    raise MCP2200Error(f"no MCP2200 HID interface found for {self.vid:04X}:{self.pid:04X}")
                if len(matches) > 1:
                    raise MCP2200Error("multiple MCP2200 devices found; select one with --path or --serial")
                path = matches[0]["path"]
            if isinstance(path, str):
                path = path.encode()
            self._device.open_path(path)
            return self
        except (MCP2200Error, OSError) as error:
            hid_error = error
            self._device = None
        # The libusb HIDAPI backend can enumerate MCP2200 but may not claim
        # its interface while the Linux hid-generic driver owns it.  hidraw
        # is the native kernel interface and avoids that conflict.
        paths = self._find_hidraw_paths()
        if len(paths) == 1:
            self._open_hidraw(paths[0])
            return self
        target = self.path or f"{self.vid:04X}:{self.pid:04X}"
        suffix = "no matching /dev/hidraw node was found" if not paths else "multiple matching /dev/hidraw nodes were found; use --path"
        raise MCP2200Error(f"could not open MCP2200 {target}: {hid_error}; {suffix}") from hid_error

    def __exit__(self, *_: object) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None
        if self._hidraw_fd is not None:
            os.close(self._hidraw_fd)
            self._hidraw_fd = None

    def _find_hidraw_paths(self) -> list[str]:
        """Find hidraw nodes by their HID_ID sysfs property."""
        expected = f"HID_ID=0003:0000{self.vid:04X}:0000{self.pid:04X}"
        paths: list[str] = []
        for uevent in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
            try:
                if expected in Path(uevent).read_text(encoding="ascii").upper():
                    paths.append(f"/dev/{Path(uevent).parent.parent.name}")
            except OSError:
                continue
        return paths

    def _open_hidraw(self, path: str) -> None:
        try:
            self._hidraw_fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except OSError as error:
            raise MCP2200Error(f"could not open {path}: {error}") from error

    def exchange(self, command: Sequence[int], reply: bool = False) -> list[int] | None:
        if self._device is None and self._hidraw_fd is None:
            raise MCP2200Error("device is not open")
        if len(command) != REPORT_SIZE:
            raise ValueError("MCP2200 commands must be 16 bytes")
        try:
            if self._hidraw_fd is None:
                written = self._device.write(list(command))
            else:
                written = os.write(self._hidraw_fd, bytes(command))
            if written != REPORT_SIZE:
                raise MCP2200Error(f"short HID write: wrote {written} of {REPORT_SIZE} bytes")
            if not reply:
                return None
            if self._hidraw_fd is None:
                response = list(self._device.read(REPORT_SIZE, self.timeout_ms))
            else:
                readable, _, _ = select.select([self._hidraw_fd], [], [], self.timeout_ms / 1000)
                response = list(os.read(self._hidraw_fd, REPORT_SIZE)) if readable else []
        except OSError as error:
            raise MCP2200Error(f"HID I/O failed: {error}") from error
        if not response:
            raise MCP2200Error(f"timed out after {self.timeout_ms} ms waiting for MCP2200 response")
        return response

    def read_config(self) -> Configuration:
        response = self.exchange(report(CMD_READ_ALL), reply=True)
        assert response is not None
        return Configuration.from_read_all(response)

    def write_config(self, config: Configuration) -> None:
        self.exchange(config.configure_report())

    def get_gpio(self) -> int:
        """Read the instantaneous eight-bit GPIO/alternate-pin state."""
        return self.read_config().gpio_values

    def set_gpio(self, set_mask: int, clear_mask: int) -> None:
        """Change selected output pins without changing NVRAM configuration."""
        self.exchange(report(CMD_SET_CLEAR_OUTPUTS, {11: set_mask, 12: clear_mask}))

    def read_eeprom(self, address: int) -> int:
        response = self.exchange(report(CMD_READ_EEPROM, {1: address}), reply=True)
        assert response is not None
        validate_response(response, CMD_READ_EEPROM)
        if response[1] != address:
            raise MCP2200Error(f"EEPROM response address 0x{response[1]:02X} does not match 0x{address:02X}")
        return response[3]

    def write_eeprom(self, address: int, value: int) -> None:
        self.exchange(report(CMD_WRITE_EEPROM, {1: address, 2: value}))


def device_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", help="HID device path, e.g. /dev/hidraw4")
    parser.add_argument("--vid", type=integer, default=DEFAULT_VID, help="USB vendor ID (default: 0x04D8)")
    parser.add_argument("--pid", type=integer, default=DEFAULT_PID, help="USB product ID (default: 0x00DF)")
    parser.add_argument("--serial", help="USB serial number when selecting by VID/PID")
    parser.add_argument("--timeout-ms", type=int, default=1000, help="response timeout (default: 1000)")


def print_config(config: Configuration, as_json: bool) -> None:
    if as_json:
        print(json.dumps(config.display(), indent=2, sort_keys=True))
        return
    roles = {
        0: "SSPND (USB suspend)" if config.alt_pins & 0x80 else "GPIO",
        1: "USBCFG (USB configured)" if config.alt_pins & 0x40 else "GPIO",
        2: "GPIO",
        3: "GPIO",
        4: "GPIO",
        5: "GPIO",
        6: "RxLED (USB receive)" if config.alt_pins & 0x08 else "GPIO",
        7: "TxLED (USB transmit)" if config.alt_pins & 0x04 else "GPIO",
    }

    print("MCP2200 persistent configuration")
    print("=" * 31)
    print()
    print("GPIO bitmaps (bit order: GP7 to GP0)")
    print(f"  Directions:     {hex_binary(config.io_directions)}  (1=input, 0=output)")
    print(f"  Default outputs: {hex_binary(config.default_values)}  (power-up level; 1=high, 0=low)")
    print(f"  Current levels: {hex_binary(config.gpio_values)}  (1=high, 0=low)")
    print()
    print("GPIO pins")
    print("  Pin  Direction  Role                    Current  Default level")
    for pin in range(7, -1, -1):
        direction = "input" if config.io_directions & (1 << pin) else "output"
        level = "high" if config.gpio_values & (1 << pin) else "low"
        default = "high" if config.default_values & (1 << pin) else "low"
        if direction == "input" or roles[pin] != "GPIO":
            default += " (ignored)"
        print(f"  GP{pin}  {direction:<9}  {roles[pin]:<22}  {level:<7}  {default}")
    print()
    print("Special functions")
    print(f"  Options bitmap: {hex_binary(config.alt_options)}")
    print("  Bit  Mask  Function                         Current setting")
    option_rows = (
        (0, "Hardware flow control (RTS/CTS)", "enabled" if config.alt_options & 0x01 else "disabled"),
        (1, "UART/RTS/CTS signal inversion", "enabled" if config.alt_options & 0x02 else "disabled"),
        (5, "LED blink period", "slow (200 ms)" if config.alt_options & 0x20 else "fast (100 ms)"),
        (6, "TxLED behavior", "toggle" if config.alt_options & 0x40 else "blink"),
        (7, "RxLED behavior", "toggle" if config.alt_options & 0x80 else "blink"),
    )
    for bit, function, setting in option_rows:
        print(f"  {bit:<3}  0x{1 << bit:02X}  {function:<31}  {setting}")
    print()
    print("UART")
    print(f"  Baud divisor: {hex_binary(config.baud_divisor, 16)}")
    print(f"  Actual rate:  {config.baud_rate} baud (0x{config.baud_rate:X}, 0b{config.baud_rate:b})")
    print()
    print("READ_ALL EEPROM snapshot")
    print(f"  Address: {hex_binary(config.eeprom_address)}")
    print(f"  Value:   {hex_binary(config.eeprom_value)}")


def gpio_role(config: Configuration, pin: int) -> str:
    alternate_roles = {
        0: (0x80, "SSPND (USB suspend)"),
        1: (0x40, "USBCFG (USB configured)"),
        6: (0x08, "RxLED (USB receive)"),
        7: (0x04, "TxLED (USB transmit)"),
    }
    mask_role = alternate_roles.get(pin)
    return mask_role[1] if mask_role and config.alt_pins & mask_role[0] else "GPIO"


def print_gpio(config: Configuration, as_json: bool) -> None:
    value = config.gpio_values
    decoded = {
        f"GP{pin}": {
            "level": "high" if value & (1 << pin) else "low",
            "direction": "input" if config.io_directions & (1 << pin) else "output",
            "role": gpio_role(config, pin),
        }
        for pin in range(7, -1, -1)
    }
    if as_json:
        print(json.dumps({"levels": hex_binary(value), "pins": decoded}, indent=2))
        return
    print(f"GPIO levels (GP7 to GP0): {hex_binary(value)}")
    print("  Pin  Level  Direction  Role                  Runtime write")
    for pin, details in decoded.items():
        controllable = "yes" if details["direction"] == "output" and details["role"] == "GPIO" else "no"
        print(f"  {pin}  {details['level']:<5}  {details['direction']:<9}  {details['role']:<20}  {controllable}")


def print_eeprom_dump(start: int, values: list[int], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"start": start, "length": len(values), "values": values}, indent=2))
        return
    print("EEPROM user-data dump")
    print("  Address  " + " ".join(f"{offset:X}" for offset in range(16)))
    for row_offset in range(0, len(values), 16):
        row = values[row_offset : row_offset + 16]
        cells = " ".join(f"{value:02X}" for value in row)
        print(f"  {start + row_offset:02X}       {cells}")


def command_list(args: argparse.Namespace) -> int:
    try:
        hid = Device._hid()
        devices = hid.enumerate(args.vid, args.pid)
    except MCP2200Error:
        # hidraw is sufficient on Linux and remains available when sudo uses
        # a Python installation without the optional hidapi package.
        paths = Device(None, args.vid, args.pid, None, 1000)._find_hidraw_paths()
        for path in paths:
            print(json.dumps({"path": path, "serial": None, "manufacturer": None, "product": None, "backend": "hidraw"}))
        return 0 if paths else 1

    for item in devices:
        path = item.get("path", b"")
        if isinstance(path, bytes):
            path = path.decode(errors="replace")
        print(json.dumps({"path": path, "serial": item.get("serial_number"), "manufacturer": item.get("manufacturer_string"), "product": item.get("product_string"), "backend": "hidapi"}))
    return 0 if devices else 1


def updated_config(current: Configuration, args: argparse.Namespace) -> Configuration:
    divisor = current.baud_divisor if args.baud is None else 12_000_000 // args.baud - 1
    return Configuration(
        io_directions=current.io_directions if args.io is None else args.io,
        alt_pins=current.alt_pins if args.alt_pins is None else args.alt_pins,
        default_values=current.default_values if args.default_values is None else args.default_values,
        alt_options=current.alt_options if args.alt_options is None else args.alt_options,
        baud_divisor=divisor,
        gpio_values=current.gpio_values,
        eeprom_address=current.eeprom_address,
        eeprom_value=current.eeprom_value,
    )


def novarq_tactical_1000_config(current: Configuration) -> Configuration:
    """Return the MCP2200 persistent pin configuration for Tactical 1000."""
    return Configuration(
        # GP1 is input; GP0 and GP2 through GP7 are outputs.
        io_directions=0x02,
        # GP6 and GP7 are respectively the RxLED and TxLED alternate pins.
        alt_pins=0x0C,
        # GP0 powers up high; GP2 through GP5 power up low.  GP1 is input and
        # GP6/GP7 are LEDs, so their default-output bits do not apply.
        default_values=0x01,
        # Force slow blink (bit 5), force both LED modes to blink (bits 6/7
        # clear), and disable flow control/inversion (bits 0/1 clear).
        alt_options=0x20,
        # Nominal 115200 baud: MCP2200's 12 MHz divider yields 115384 actual.
        baud_divisor=12_000_000 // 115_200 - 1,
        gpio_values=current.gpio_values,
        eeprom_address=current.eeprom_address,
        eeprom_value=current.eeprom_value,
    )


def print_novarq_tactical_1000_profile(config: Configuration) -> None:
    """Show exactly the persistent fields the Novarq initializer will write."""
    print("Novarq Tactical 1000 configuration to write")
    print("=" * 44)
    print(f"  GPIO directions:        {hex_binary(config.io_directions)}")
    print("    GP1=input; GP0 and GP2-GP7=output")
    print(f"  Default output levels:  {hex_binary(config.default_values)}")
    print("    GP0=high; GP2-GP5=low")
    print(f"  Alternate pin roles:    {hex_binary(config.alt_pins)}")
    print("    GP6=RxLED; GP7=TxLED")
    print(f"  Special options:        {hex_binary(config.alt_options)}")
    print("    slow blink; RxLED/TxLED=blink; HW_FLOW=off; INVERT=off")
    print(f"  Default UART baud:      115200 nominal ({config.baud_rate} actual)")
    print()
    print("This writes persistent NVRAM. Reset the MCP2200 for the profile to take effect.")


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise MCP2200Error("confirmation requires a terminal; use --yes for non-interactive use")
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def tactical_1000_boot_masks(mode: int) -> tuple[int, int]:
    """Return SET/CLEAR masks for GP5:GP2 = VCORE3:VCORE0."""
    return (mode << 2) & 0x3C, ((~mode) << 2) & 0x3C


def print_tactical_1000_boot_mode(mode: int) -> None:
    name, description = BOOT_MODES[mode]
    print("Tactical 1000 LAN969x boot mode to set")
    print("=" * 40)
    print(f"  Mode:           {name} - {description}")
    print(f"  VCORE3:VCORE0:  {mode:04b} (0x{mode:X})")
    print(f"  GP5:GP2:        {mode:04b} (GP5=VCORE3, GP2=VCORE0)")
    print()
    print("This changes live GPIO levels only. Reset the LAN969x after writing so it samples the straps.")


def print_tactical_1000_reset(duration_ms: int) -> None:
    print("Tactical 1000 reset to issue")
    print("=" * 27)
    print("  Reset line: GP0 (active-low)")
    print(f"  Pulse:      drive low for {duration_ms} ms, then release high")


def run(args: argparse.Namespace) -> int:
    if args.command == "list":
        return command_list(args)
    with Device(args.path, args.vid, args.pid, args.serial, args.timeout_ms) as device:
        if args.command == "get-config":
            print_config(device.read_config(), args.json)
        elif args.command == "set-config":
            current = device.read_config()
            desired = updated_config(current, args)
            device.write_config(desired)
            if args.verify:
                time.sleep(0.020)
                actual = device.read_config()
                if actual.configure_report() != desired.configure_report():
                    raise MCP2200Error("configuration verification failed: read-back differs from requested values")
                print_config(actual, args.json)
        elif args.command == "init-novarq-tactical-1000":
            desired = novarq_tactical_1000_config(device.read_config())
            print_novarq_tactical_1000_profile(desired)
            if not confirm("Write this configuration to the MCP2200? [y/N]: ", args.yes):
                print("Initialization cancelled; no configuration was written.")
                return 0
            device.write_config(desired)
            time.sleep(0.020)
            actual = device.read_config()
            if actual.configure_report() != desired.configure_report():
                raise MCP2200Error("Novarq Tactical 1000 configuration verification failed")
            print_config(actual, args.json)
        elif args.command == "set-tactical-1000-boot-mode":
            config = device.read_config()
            if config.io_directions & 0x3C:
                raise MCP2200Error("GP2-GP5 must be configured as outputs; run init-novarq-tactical-1000 first")
            print_tactical_1000_boot_mode(args.mode)
            if not confirm("Set this live LAN969x boot-mode strap value? [y/N]: ", args.yes):
                print("Boot-mode change cancelled; no GPIO levels were changed.")
                return 0
            set_mask, clear_mask = tactical_1000_boot_masks(args.mode)
            device.set_gpio(set_mask, clear_mask)
            time.sleep(0.010)
            actual = device.read_config().gpio_values
            if actual & 0x3C != set_mask:
                raise MCP2200Error("boot-mode GPIO verification failed")
            print(f"Verified live VCORE3:VCORE0: {((actual >> 2) & 0x0F):04b} (0x{(actual >> 2) & 0x0F:X})")
            print("Next step: reset the Tactical 1000/LAN969x board to sample this boot-mode strap value.")
        elif args.command == "reset-tactical-1000":
            config = device.read_config()
            if config.io_directions & 0x01 or config.alt_pins & 0x80:
                raise MCP2200Error("GP0 must be a normal GPIO output; run init-novarq-tactical-1000 first")
            print_tactical_1000_reset(args.duration_ms)
            if not confirm("Pulse the Tactical 1000 reset line now? [y/N]: ", args.yes):
                print("Reset cancelled; GP0 was not changed.")
                return 0
            device.set_gpio(0x00, 0x01)
            try:
                time.sleep(args.duration_ms / 1000)
            finally:
                # Always try to release reset, including after an interrupt.
                device.set_gpio(0x01, 0x00)
            actual = device.read_config().gpio_values
            if not actual & 0x01:
                raise MCP2200Error("reset-line verification failed: GP0 did not return high")
            print("Reset pulse complete; GP0 is high (reset released).")
        elif args.command == "get-gpio":
            print_gpio(device.read_config(), args.json)
        elif args.command == "set-gpio":
            if args.value is not None:
                if args.set is not None or args.clear is not None:
                    raise MCP2200Error("--value cannot be combined with --set or --clear")
                set_mask, clear_mask = args.value, args.value ^ 0xFF
            else:
                set_mask, clear_mask = args.set or 0, args.clear or 0
                if not set_mask and not clear_mask:
                    raise MCP2200Error("set-gpio needs --set, --clear, or --value")
            if set_mask & clear_mask:
                raise MCP2200Error("the same GPIO bit cannot appear in both --set and --clear")
            device.set_gpio(set_mask, clear_mask)
            if args.readback:
                time.sleep(0.010)
                print_gpio(device.read_config(), args.json)
        elif args.command == "dump-eeprom":
            if args.start + args.length > 256:
                raise MCP2200Error("EEPROM range exceeds address 0xFF")
            values = [device.read_eeprom(address) for address in range(args.start, args.start + args.length)]
            print_eeprom_dump(args.start, values, args.json)
        elif args.command == "read-eeprom":
            print(hex_binary(device.read_eeprom(args.address)))
        elif args.command == "write-eeprom":
            device.write_eeprom(args.address, args.value)
            if args.verify:
                time.sleep(0.020)
                actual = device.read_eeprom(args.address)
                if actual != args.value:
                    raise MCP2200Error(f"EEPROM verification failed: read 0x{actual:02X}, expected 0x{args.value:02X}")
                print(hex_binary(actual))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read and configure MCP2200 HID settings on Linux.")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="list matching MCP2200 HID interfaces")
    list_parser.add_argument("--vid", type=integer, default=DEFAULT_VID)
    list_parser.add_argument("--pid", type=integer, default=DEFAULT_PID)

    get_parser = subparsers.add_parser("get-config", help="read persistent configuration")
    device_options(get_parser)
    get_parser.add_argument("--json", action="store_true", help="print JSON")

    set_parser = subparsers.add_parser("set-config", help="change only the supplied persistent configuration values")
    device_options(set_parser)
    set_parser.add_argument("--io", type=byte, help="GPIO directions bitmap: 1=input, 0=output")
    set_parser.add_argument("--default-values", type=byte, help="default GPIO output-value bitmap")
    set_parser.add_argument("--alt-pins", type=byte, help="alternate-pin bitmap")
    set_parser.add_argument("--alt-options", type=byte, help="alternate-function-options bitmap")
    set_parser.add_argument("--baud", type=baud, help="default UART baud rate")
    set_parser.add_argument("--verify", action="store_true", help="read configuration back after writing")
    set_parser.add_argument("--json", action="store_true", help="with --verify, print read-back JSON")

    gpio_get_parser = subparsers.add_parser("get-gpio", help="read current GPIO levels")
    device_options(gpio_get_parser)
    gpio_get_parser.add_argument("--json", action="store_true", help="print JSON")

    gpio_set_parser = subparsers.add_parser("set-gpio", help="change current GPIO output levels (not persistent)")
    device_options(gpio_set_parser)
    gpio_set_parser.add_argument("--set", type=byte, help="bitmap of GPIO bits to set high")
    gpio_set_parser.add_argument("--clear", type=byte, help="bitmap of GPIO bits to set low")
    gpio_set_parser.add_argument("--value", type=byte, help="set all GPIO output bits to this value")
    gpio_set_parser.add_argument("--readback", action="store_true", help="read and print levels after writing")
    gpio_set_parser.add_argument("--json", action="store_true", help="with --readback, print JSON")

    dump_parser = subparsers.add_parser("dump-eeprom", help="read all or part of the 256-byte user EEPROM")
    device_options(dump_parser)
    dump_parser.add_argument("--start", type=byte, default=0, help="first EEPROM address (default: 0)")
    dump_parser.add_argument("--length", type=eeprom_length, default=256, help="number of bytes to read (default: 256)")
    dump_parser.add_argument("--json", action="store_true", help="print JSON")

    read_parser = subparsers.add_parser("read-eeprom", help="read one user EEPROM byte")
    device_options(read_parser)
    read_parser.add_argument("address", type=byte)

    write_parser = subparsers.add_parser("write-eeprom", help="write one user EEPROM byte")
    device_options(write_parser)
    write_parser.add_argument("address", type=byte)
    write_parser.add_argument("value", type=byte)
    write_parser.add_argument("--verify", action="store_true", help="read byte back after writing")

    boot_parser = subparsers.add_parser(
        "set-tactical-1000-boot-mode",
        help="set and verify live LAN969x VCORE3:0 boot straps on Tactical 1000",
        description=(
            "Set and verify live LAN969x VCORE3:0 boot straps on Tactical 1000.\n"
            "GP5:GP2 maps to VCORE3:VCORE0.\n"
            "Reset the board after setting a mode so the LAN969x samples the straps."
        ),
        epilog="After a successful write, reset the Tactical 1000/LAN969x board to apply the selected boot mode.\n\nSupported modes:\n" + "\n".join(
            f"  {name:<20} 0x{value:X}  {description}" for value, (name, description) in BOOT_MODES.items()
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    device_options(boot_parser)
    boot_parser.add_argument("mode", type=boot_mode, metavar="MODE", help="mode name or supported numeric value (for example: qspi0 or 0x4)")
    boot_parser.add_argument("--yes", action="store_true", help="write without the interactive confirmation")

    reset_parser = subparsers.add_parser(
        "reset-tactical-1000",
        help="pulse the active-low Tactical 1000 reset line on GP0",
        description="Pulse the active-low Tactical 1000 reset line on GP0, then verify it is released high.",
    )
    device_options(reset_parser)
    reset_parser.add_argument("--duration-ms", type=reset_duration_ms, default=100, help="low pulse duration, 1..5000 ms (default: 100)")
    reset_parser.add_argument("--yes", action="store_true", help="reset without the interactive confirmation")

    novarq_parser = subparsers.add_parser(
        "init-novarq-tactical-1000",
        help="apply and verify the Novarq Tactical 1000 MCP2200 pin profile",
    )
    device_options(novarq_parser)
    novarq_parser.add_argument("--yes", action="store_true", help="write without the interactive confirmation")
    novarq_parser.add_argument("--json", action="store_true", help="print verified configuration as JSON")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        parser.print_help()
        return 0
    # These commands cannot do useful work with no extra arguments. Showing
    # their local help is clearer (and safer) than opening a device first.
    if len(arguments) == 1 and arguments[0] in {"set-config", "set-gpio", "read-eeprom", "write-eeprom", "set-tactical-1000-boot-mode"}:
        subparsers_action = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        subparsers_action.choices[arguments[0]].print_help()
        return 0
    args = parser.parse_args(arguments)
    try:
        return run(args)
    except MCP2200Error as error:
        print(f"mcp2200ctl: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
