#!/usr/bin/python3 -u
"""
test_tank_sim.py — Inject simulated tank level values for testing.
Writes to /tmp/tank-sim-level which dbus-tank-ads1115.py reads in --simulate mode.

Usage:
    python3 test_tank_sim.py              # Interactive: type a number 0-100
    python3 test_tank_sim.py 75           # Set level to 75% and exit
    python3 test_tank_sim.py ramp         # Auto-ramp 0→100→0 in a loop
    python3 test_tank_sim.py random       # Random values every 2 seconds
"""

import sys
import os
import time
import random

SIM_FILE = '/tmp/tank-sim-level'


def set_level(level):
    """Write a level value to the simulation file."""
    level = max(0.0, min(100.0, float(level)))
    with open(SIM_FILE, 'w') as f:
        f.write('%.1f\n' % level)
    print('Tank level set to %.1f%%' % level)


def mode_interactive():
    """Interactive mode: type a number to set the level."""
    print('Interactive tank simulator. Type a number 0-100 (or q to quit):')
    while True:
        try:
            val = input('Level> ').strip()
            if val.lower() in ('q', 'quit', 'exit'):
                break
            set_level(float(val))
        except ValueError:
            print('  Invalid number, try again')
        except (KeyboardInterrupt, EOFError):
            break
    print('Stopped.')


def mode_ramp():
    """Ramp level from 0 to 100 and back, in steps of 5, every 2 seconds."""
    print('Ramping tank level 0→100→0 (Ctrl+C to stop)...')
    try:
        while True:
            for level in range(0, 101, 5):
                set_level(level)
                time.sleep(2)
            for level in range(100, -1, -5):
                set_level(level)
                time.sleep(2)
    except KeyboardInterrupt:
        print('\nStopped.')


def mode_random():
    """Random tank levels every 2 seconds."""
    print('Random tank levels (Ctrl+C to stop)...')
    try:
        while True:
            set_level(random.uniform(0, 100))
            time.sleep(2)
    except KeyboardInterrupt:
        print('\nStopped.')


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == 'ramp':
            mode_ramp()
        elif arg == 'random':
            mode_random()
        else:
            try:
                set_level(float(arg))
            except ValueError:
                print('Usage: test_tank_sim.py [0-100 | ramp | random]')
                sys.exit(1)
    else:
        mode_interactive()


if __name__ == '__main__':
    main()
