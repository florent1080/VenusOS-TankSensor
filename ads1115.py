#!/usr/bin/python3 -u
"""
ads1115.py — Minimal ADS1115 16-bit ADC driver using raw I2C (ioctl).
No dependency on smbus/smbus2/Adafruit. Works on Venus OS with fcntl + struct.

Usage:
    from ads1115 import ADS1115
    adc = ADS1115(bus=1, address=0x48)
    voltage = adc.read_voltage(channel=0)
"""

import fcntl
import struct
import time

# I2C ioctl constants (from linux/i2c-dev.h)
I2C_SLAVE = 0x0703

# ADS1115 register addresses
_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# Config register bits
_OS_SINGLE = 0x8000     # Start single conversion
_MUX_SINGLE = {         # Single-ended input channels
    0: 0x4000,  # AIN0 vs GND
    1: 0x5000,  # AIN1 vs GND
    2: 0x6000,  # AIN2 vs GND
    3: 0x7000,  # AIN3 vs GND
}

# PGA (Programmable Gain Amplifier) full-scale range
_PGA_MAP = {
    6.144: 0x0000,
    4.096: 0x0200,
    2.048: 0x0400,  # default
    1.024: 0x0600,
    0.512: 0x0800,
    0.256: 0x0A00,
}

# Data rate (samples per second)
_DR_128SPS = 0x0080

# Mode
_MODE_SINGLE = 0x0100

# Comparator disable
_COMP_DISABLE = 0x0003


class ADS1115:
    """Minimal ADS1115 driver via raw Linux I2C ioctl."""

    def __init__(self, bus=1, address=0x48, gain=4.096):
        """
        Args:
            bus: I2C bus number (1 for RPi GPIO header)
            address: I2C address (0x48 default, ADDR→GND)
            gain: PGA full-scale voltage (4.096V recommended for 3.3V divider)
        """
        self.address = address
        self.gain = gain
        self._fs = gain  # full-scale voltage for conversion
        self._pga_bits = _PGA_MAP.get(gain, 0x0200)
        self._fd = None
        self._bus_path = '/dev/i2c-%d' % bus

    def open(self):
        """Open the I2C bus. Returns True on success."""
        try:
            self._fd = open(self._bus_path, 'rb+', buffering=0)
            fcntl.ioctl(self._fd, I2C_SLAVE, self.address)
            return True
        except (OSError, IOError) as e:
            self._fd = None
            raise RuntimeError('Cannot open I2C bus %s: %s' % (self._bus_path, e))

    def close(self):
        """Close the I2C bus."""
        if self._fd:
            try:
                self._fd.close()
            except:
                pass
            self._fd = None

    def _write_register(self, reg, value):
        """Write a 16-bit value to a register."""
        data = struct.pack('>BH', reg, value)
        self._fd.write(data)

    def _read_register(self, reg):
        """Read a 16-bit value from a register."""
        self._fd.write(bytes([reg]))
        raw = self._fd.read(2)
        return struct.unpack('>h', raw)[0]

    def read_raw(self, channel=0):
        """Start a single-shot conversion and return the raw 16-bit signed value."""
        if self._fd is None:
            raise RuntimeError('I2C bus not open')
        if channel not in _MUX_SINGLE:
            raise ValueError('Channel must be 0-3')

        config = (_OS_SINGLE |
                  _MUX_SINGLE[channel] |
                  self._pga_bits |
                  _MODE_SINGLE |
                  _DR_128SPS |
                  _COMP_DISABLE)

        self._write_register(_REG_CONFIG, config)

        # Wait for conversion (~8ms at 128SPS)
        time.sleep(0.01)

        # Poll until conversion ready (bit 15 of config = 1)
        for _ in range(10):
            cfg = self._read_register(_REG_CONFIG) & 0xFFFF
            if cfg & 0x8000:
                break
            time.sleep(0.005)

        return self._read_register(_REG_CONVERSION)

    def read_voltage(self, channel=0):
        """Read voltage on the specified channel (0-3). Returns volts."""
        raw = self.read_raw(channel)
        # ADS1115: 16-bit signed, FSR = gain value
        # Max positive value = 32767 = +FS
        return raw * self._fs / 32767.0
