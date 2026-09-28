# MCP2200 Python control utility

`mcp2200ctl.py` is a small Linux command-line tool for reading and writing
the MCP2200 configuration and its 256-byte user EEPROM through the device's
HID interface. It implements the 16-byte reports documented in Microchip
TB3066 (DS93066A).

## Install

No Python package is required for normal Linux use: the utility talks directly
to `/dev/hidrawN`. Optionally, install `hidapi` if you want its additional USB
enumeration metadata:

```sh
python3 -m pip install -r requirements.txt
```

The account running the command must have read/write access to the MCP2200's
`/dev/hidraw*` node. A udev rule is normally the right permanent solution;
using `sudo` is useful only for an initial check.

For example, save this as `/etc/udev/rules.d/70-mcp2200.rules` on a desktop
Fedora system, then unplug/replug the device (or reload udev rules):

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="00df", TAG+="uaccess"
```

Linux 6.8 and later ship `hid_mcp2200`, a GPIO driver that binds to the
MCP2200's HID interface. While it is loaded there is no `/dev/hidrawN` for
the device, and `hid-generic` will not take the interface over. Blacklist
the module, then unplug/replug the device:

```sh
echo 'blacklist hid_mcp2200' | sudo tee /etc/modprobe.d/mcp2200.conf
sudo rmmod hid_mcp2200
```

The serial port is a separate `cdc_acm` interface and is not affected.

The utility first tries HIDAPI and, if its libusb backend cannot claim the
composite device's HID interface, automatically falls back to the matching
Linux `hidraw` node.

## Use

List devices with the factory IDs:

```sh
python3 mcp2200ctl.py list
```

Read persistent configuration:

```sh
python3 mcp2200ctl.py get-config --path /dev/hidraw4
```

Change only the default UART baud rate and GPIO-direction bitmap, then verify
the NVRAM write. `1` means input and `0` means output in `--io`.

```sh
python3 mcp2200ctl.py set-config --path /dev/hidraw4 --baud 115200 --io 0x03 --verify
```

Read or write one EEPROM byte:

```sh
python3 mcp2200ctl.py read-eeprom --path /dev/hidraw4 0x10
python3 mcp2200ctl.py write-eeprom --path /dev/hidraw4 0x10 0x5A --verify
python3 mcp2200ctl.py dump-eeprom --path /dev/hidraw4
```

`dump-eeprom` reads all 256 bytes by default. Use `--start 0x80 --length 32`
to read a smaller range.

Read the instantaneous GPIO states, or change ordinary GPIO output pins without
altering persistent configuration:

```sh
python3 mcp2200ctl.py get-gpio --path /dev/hidraw4
python3 mcp2200ctl.py set-gpio --path /dev/hidraw4 --set 0x04 --clear 0x08 --readback
```

`--set` and `--clear` are bitmaps (bit 0 is GP0). `--value 0xNN` is a shortcut
to set the complete GPIO output bitmap. Pins configured as inputs or alternate
functions do not respond to runtime GPIO writes.

`set-config` first performs `READ_ALL`, so omitted options are preserved. The
configuration report is stored in NVRAM and takes effect after reset. EEPROM
and configuration writes have no acknowledgement report, so use `--verify`
when a write must be confirmed.

## Novarq Tactical 1000 profile

Apply the board's MCP2200 pin profile and verify it by reading NVRAM back:

```sh
python3 mcp2200ctl.py init-novarq-tactical-1000 --path /dev/hidraw4
```

This sets GP1 as input and GP0/GP2-GP7 as outputs, assigns GP6/GP7 as
RxLED/TxLED, sets GP0's default high and GP2-GP5 defaults low. It sets slow
LED blinking, disables hardware flow control and UART/RTS/CTS inversion, and
sets the default UART baud rate to nominal 115200 (115384 actual). Both RxLED
and TxLED are forced to blink. Reset the MCP2200 after applying it.

The command displays the profile and asks for confirmation before writing. Use
`--yes` only for an intentional non-interactive/programming workflow.

## Tactical 1000 boot mode

GP2-GP5 drive the LAN969x `VCORE0-VCORE3` boot straps. Set a supported live
boot mode, then reset the LAN969x so it samples the new values:

```sh
python3 mcp2200ctl.py set-tactical-1000-boot-mode qspi0
```

Supported named modes are `emmc-trace`, `qspi0-trace`, `sdcard-trace`, `emmc`,
`qspi0`, `sdcard`, `qspi0-hs-trace`, `tfa-monitor`, `tfa-monitor-hs`,
`qspi0-hs`, and `spi-client`. Numeric supported values such as `0x4` are also
accepted. The command confirms the selected mode and verifies the GPIO levels
before asking you to reset the LAN969x.

## Tactical 1000 reset

The board profile uses GP0 as an active-low reset output. Issue a confirmed
100 ms reset pulse with:

```sh
python3 mcp2200ctl.py reset-tactical-1000
```

Use `--duration-ms 250` to choose a 1-5000 ms low pulse, or `--yes` for a
deliberate non-interactive reset.

Factory VID/PID (`04D8:00DF`) are defaults. If the device has custom IDs, pass
`--vid` and `--pid`; `--path` is the most explicit selection method.

## Bitmap fields

`--alt-pins`: bit 7 SSPND (GP0), bit 6 USBCFG (GP1), bit 3 RxLED (GP6), bit 2
TxLED (GP7). `--alt-options`: bit 7 Rx toggle, bit 6 Tx toggle, bit 5 LED
slow blink, bit 1 invert UART/RTS/CTS, bit 0 hardware flow control.

Run `python3 mcp2200ctl.py --help` for the complete CLI reference.
