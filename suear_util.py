#!/usr/bin/env python3
# Author: Sean Pesce

import math
import os
import platform
import socket
import sys


def ping(host, timeout=1):
    """
    Returns True if the target host sent an ICMP response within the specified timeout interval
    """
    # Protect against command injection
    if type(host) != str:
        raise TypeError(f'Non-string type for "host" argument: {type(host)}')
    # Alphabet for IP addresses and host names
    safe_alphabet = '.:0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_'
    for c in host:
        if c not in safe_alphabet:
            raise ValueError(f'Invalid character in "host" argument: "{c}"')
    host = socket.gethostbyname(host)
    # Determine argument syntax (Linux vs Windows)
    count_flag = 'c'
    dev_null = '/dev/null'
    quote_char = '\''
    wait_flag = 'w'
    if 'windows' in platform.system().lower():
        count_flag = 'n'
        dev_null = 'NUL'
        quote_char = '"'
    if 'darwin' in platform.system().lower():
        wait_flag = 'W'

    cmd = f'ping -{count_flag} 1 -{wait_flag} {int(timeout)} {quote_char}{host}{quote_char} 2>&1 > {dev_null}'
    retcode = os.system(cmd)
    return retcode == 0


def accel_axis(packed, shift, sign_bit):
    """
    One accelerometer axis from the packed sample: 9-bit magnitude plus a
    separate sign bit (~130 counts per g)
    """
    magnitude = (packed >> shift) & 0x1ff
    return -magnitude if (packed >> sign_bit) & 1 else magnitude


def roll_degrees(packed):
    """
    Roll angle in degrees [0, 360) of the probe around its long axis, from the
    packed accelerometer sample in the video header. The vendor library computes
    atan(y / z) and fixes the quadrant from the sign bits.

    Returns None when the probe points straight up or down (y == z == 0) and the
    roll is undefined.
    """
    y = accel_axis(packed, shift=10, sign_bit=19)
    z = accel_axis(packed, shift=0, sign_bit=9)
    if y == 0 and z == 0:
        return None
    return math.degrees(math.atan2(y, z)) % 360.0


def slope_degrees(packed):
    """Tilt of the probe's long axis against the horizontal, in degrees"""
    x = accel_axis(packed, shift=20, sign_bit=29)
    y = accel_axis(packed, shift=10, sign_bit=19)
    z = accel_axis(packed, shift=0, sign_bit=9)
    return math.degrees(math.atan2(x, math.hypot(y, z)))


def mount_offset_degrees(product):
    """
    The vendor app adds a fixed 180 degrees for products whose sensor is mounted
    upside down relative to the lens (BK7231U-XRH-FBPRO / -R1)
    """
    product = (product or '').upper()
    return 180.0 if ('FBPRO' in product or 'R1' in product) else 0.0
