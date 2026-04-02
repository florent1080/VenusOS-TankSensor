#!/usr/bin/python3 -u
"""
dbus-tank-ads1115.py — Venus OS D-Bus tank level service using ADS1115 ADC.
Reads a resistive tank sender (e.g. Osculati 10-180 ohm) via a voltage divider
connected to an ADS1115 I2C ADC, and publishes tank level on D-Bus.

Supports a --simulate mode for testing without hardware.

Publishes: com.victronenergy.tank.ads1115_tank0
"""

import os
import sys
import signal
import logging
import collections

sys.path.insert(1, os.path.join(os.path.dirname(__file__),
    '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))

from gi.repository import GLib
import dbus
import dbus.mainloop.glib

from vedbus import VeDbusService
from settingsdevice import SettingsDevice

VERSION = '1.0'
SERVICE_NAME = 'com.victronenergy.tank.ads1115_tank0'
DEVICE_INSTANCE = 20
PRODUCT_NAME = 'DIY Tank Sensor (ADS1115)'

# Default config
# NOTE: Capacity is in m³ (cubic meters). Venus OS uses SI units internally.
# 0.08 m³ = 80 liters, 0.1 m³ = 100 liters
DEFAULTS = {
    'CAPACITY': 0.08,
    'FLUID_TYPE': 1,
    'R_REF': 100,
    'R_EMPTY': 10,
    'R_FULL': 180,
    'I2C_BUS': 1,
    'I2C_ADDR': 0x48,
    'ADC_CHANNEL': 0,
    'POLL_INTERVAL': 3,
    'SAMPLES': 5,
}

CONFIG_FILE = '/data/dbus-tank-ads1115/tank-sensor.conf'
SIMULATE_PIPE = '/tmp/tank-sim'

log = logging.getLogger('dbus-tank-ads1115')
logging.basicConfig(format='%(levelname)-8s %(message)s', level=logging.INFO)


def load_config():
    """Load config from file, falling back to defaults."""
    cfg = dict(DEFAULTS)
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip()
                    if key in cfg:
                        # Parse type from default
                        if isinstance(DEFAULTS.get(key), int):
                            if val.startswith('0x') or val.startswith('0X'):
                                cfg[key] = int(val, 16)
                            else:
                                cfg[key] = int(val)
                        elif isinstance(DEFAULTS.get(key), float):
                            cfg[key] = float(val)
                        else:
                            cfg[key] = val
    return cfg


class TankService:
    def __init__(self, simulate=False):
        self.simulate = simulate
        self.adc = None
        self.dbus_svc = None
        self.settings = None
        self.cfg = load_config()
        self.samples = collections.deque(maxlen=self.cfg['SAMPLES'])

    def start(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        # --- D-Bus service ---
        self.dbus_svc = VeDbusService(SERVICE_NAME, register=False)

        # Management paths
        self.dbus_svc.add_path('/Mgmt/ProcessName', __file__)
        self.dbus_svc.add_path('/Mgmt/ProcessVersion', VERSION)
        self.dbus_svc.add_path('/Mgmt/Connection', 'Simulated' if self.simulate else 'I2C ADS1115')

        # Device identification
        self.dbus_svc.add_path('/DeviceInstance', DEVICE_INSTANCE)
        self.dbus_svc.add_path('/ProductId', 0)
        self.dbus_svc.add_path('/ProductName', PRODUCT_NAME)
        self.dbus_svc.add_path('/FirmwareVersion', VERSION)
        self.dbus_svc.add_path('/Connected', 1)

        # Tank-specific paths (Capacity and FluidType writable from GUI)
        self.dbus_svc.add_path('/Level', None)
        self.dbus_svc.add_path('/FluidType', self.cfg['FLUID_TYPE'],
                               writeable=True, onchangecallback=self._on_fluidtype_changed)
        self.dbus_svc.add_path('/Capacity', self.cfg['CAPACITY'],
                               writeable=True, onchangecallback=self._on_capacity_changed)
        self.dbus_svc.add_path('/Remaining', None)
        self.dbus_svc.add_path('/Status', 0)  # 0=OK

        self.dbus_svc.register()
        log.info('Registered on D-Bus as %s (DeviceInstance=%d)', SERVICE_NAME, DEVICE_INSTANCE)

        # --- Settings ---
        tank_settings = {
            'capacity': ['/Settings/Tank/%d/Capacity' % DEVICE_INSTANCE,
                         self.cfg['CAPACITY'], 0, 1.0],
            'fluidtype': ['/Settings/Tank/%d/FluidType' % DEVICE_INSTANCE,
                          self.cfg['FLUID_TYPE'], 0, 5],
        }
        self.settings = SettingsDevice(self.dbus_svc.dbusconn, tank_settings,
                                       self._setting_changed, timeout=10)
        log.info('Settings registered')

        # --- ADC ---
        if not self.simulate:
            try:
                from ads1115 import ADS1115
                self.adc = ADS1115(bus=self.cfg['I2C_BUS'],
                                   address=self.cfg['I2C_ADDR'],
                                   gain=4.096)
                self.adc.open()
                log.info('ADS1115 opened on bus %d addr 0x%02x',
                         self.cfg['I2C_BUS'], self.cfg['I2C_ADDR'])
            except Exception as e:
                log.error('Failed to open ADS1115: %s', e)
                self.dbus_svc['/Status'] = 1  # Disconnected
                self.dbus_svc['/Connected'] = 0
        else:
            log.info('SIMULATION mode — reading from %s', SIMULATE_PIPE)
            # Create named pipe for external value injection
            if not os.path.exists(SIMULATE_PIPE):
                try:
                    os.mkfifo(SIMULATE_PIPE)
                except OSError:
                    pass
            self._sim_level = None

        # Start polling
        poll_ms = self.cfg['POLL_INTERVAL'] * 1000
        GLib.timeout_add(poll_ms, self._poll)

        # Initial update
        self._poll()

        return True

    def _setting_changed(self, setting, old, new):
        log.info('Setting %s changed: %s -> %s', setting, old, new)
        if setting == 'capacity':
            self.cfg['CAPACITY'] = new
            self.dbus_svc['/Capacity'] = new
            self._update_remaining()
        elif setting == 'fluidtype':
            self.cfg['FLUID_TYPE'] = new
            self.dbus_svc['/FluidType'] = new

    def _on_capacity_changed(self, path, value):
        """Called when GUI writes to /Capacity (value in m³)."""
        value = float(value)
        log.info('Capacity changed via D-Bus: %s m³', value)
        self.cfg['CAPACITY'] = value
        self._update_remaining()
        return True  # accept the change

    def _on_fluidtype_changed(self, path, value):
        """Called when GUI writes to /FluidType."""
        value = int(value)
        log.info('FluidType changed via D-Bus: %s', value)
        self.cfg['FLUID_TYPE'] = value
        return True  # accept the change

    def _poll(self):
        """Periodic poll — read ADC or simulated value, update D-Bus."""
        try:
            if self.simulate:
                level = self._read_simulated()
            else:
                level = self._read_adc()

            if level is not None:
                # Clamp to 0-100
                level = max(0.0, min(100.0, level))
                self.dbus_svc['/Level'] = round(level, 1)
                self.dbus_svc['/Status'] = 0
                self.dbus_svc['/Connected'] = 1
                self._update_remaining()
            # If level is None, keep previous values (no update on read error)
        except Exception as e:
            log.error('Poll error: %s', e)

        return True  # Keep GLib timer alive

    def _read_adc(self):
        """Read ADS1115, convert voltage to resistance to level percentage."""
        if self.adc is None:
            self.dbus_svc['/Status'] = 1
            self.dbus_svc['/Connected'] = 0
            return None

        try:
            voltage = self.adc.read_voltage(channel=self.cfg['ADC_CHANNEL'])
        except Exception as e:
            log.warning('ADC read error: %s', e)
            self.dbus_svc['/Status'] = 1
            self.dbus_svc['/Connected'] = 0
            return None

        # Voltage divider: V_adc = V_supply * R_sensor / (R_ref + R_sensor)
        # => R_sensor = R_ref * V_adc / (V_supply - V_adc)
        v_supply = 3.3
        r_ref = self.cfg['R_REF']

        if voltage <= 0.01:
            # Near zero — possible short or disconnected
            self.dbus_svc['/Status'] = 2  # Short
            return None

        if voltage >= (v_supply - 0.05):
            # Near supply — open circuit (sensor disconnected)
            self.dbus_svc['/Status'] = 1  # Disconnected
            return None

        r_sensor = r_ref * voltage / (v_supply - voltage)

        # Map resistance to level percentage
        r_empty = self.cfg['R_EMPTY']
        r_full = self.cfg['R_FULL']

        if r_full == r_empty:
            return 0.0

        level = (r_sensor - r_empty) / (r_full - r_empty) * 100.0

        # Add to sliding average
        self.samples.append(level)
        avg = sum(self.samples) / len(self.samples)

        log.debug('V=%.3fV R=%.1f ohm raw=%.1f%% avg=%.1f%%',
                  voltage, r_sensor, level, avg)

        return avg

    def _read_simulated(self):
        """Read simulated level from file (non-blocking)."""
        # Read from a simple file instead of a pipe (easier to write to)
        sim_file = '/tmp/tank-sim-level'
        try:
            if os.path.isfile(sim_file):
                with open(sim_file) as f:
                    val = f.read().strip()
                if val:
                    level = float(val)
                    self.samples.append(level)
                    avg = sum(self.samples) / len(self.samples)
                    return avg
        except (ValueError, IOError) as e:
            log.debug('Sim read error: %s', e)

        return None

    def _update_remaining(self):
        """Update /Remaining based on /Level and /Capacity (both in m³)."""
        level = self.dbus_svc['/Level']
        capacity = self.cfg['CAPACITY']
        if level is not None and capacity:
            self.dbus_svc['/Remaining'] = round(level / 100.0 * capacity, 4)

    def stop(self):
        if self.adc:
            self.adc.close()
        log.info('Service stopped')


mainloop = None

def sigterm(s, f):
    global mainloop
    log.info('Signal received, stopping')
    if mainloop:
        mainloop.quit()


def main():
    global mainloop

    simulate = '--simulate' in sys.argv

    log.info('Starting dbus-tank-ads1115 v%s (simulate=%s)', VERSION, simulate)

    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    mainloop = GLib.MainLoop()

    svc = TankService(simulate=simulate)
    if not svc.start():
        sys.exit(1)

    log.info('Tank service running')
    mainloop.run()

    svc.stop()
    log.info('Stopped')


if __name__ == '__main__':
    main()
