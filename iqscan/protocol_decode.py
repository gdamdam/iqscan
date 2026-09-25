"""Conservative AX.25 UI frame check for Bell 202 AFSK1200 over FM.

No payload identity is inferred from a frequency or modulation estimate. A result
requires HDLC flags, valid bit stuffing, AX.25 addresses and a correct X.25 FCS.
Reference: https://tapr.org/pdf/AX25.2.2.pdf
"""
from fractions import Fraction
import math

import numpy as np
from scipy.signal import resample_poly


def _fcs(data):
    crc = 0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc ^ 0xffff


def _valid_ui(frame):
    if len(frame) < 18 or _fcs(frame[:-2]) != int.from_bytes(frame[-2:], 'little'):
        return False
    end = None
    for index in range(10):
        offset = index * 7
        if offset + 7 > len(frame) - 4:
            return False
        address = frame[offset:offset + 7]
        chars = [byte >> 1 for byte in address[:6]]
        if any(byte & 1 for byte in address[:6]) or any(
                not (char == 32 or 48 <= char <= 57 or 65 <= char <= 90) for char in chars):
            return False
        if chars[0] == 32 or address[6] & 0x60 != 0x60:
            return False
        if address[6] & 1:
            if index < 1:
                return False
            end = offset + 7
            break
    return end is not None and len(frame) >= end + 4 and frame[end] & 0xef == 0x03


def _frames(bits):
    flag = np.array([0, 1, 1, 1, 1, 1, 1, 0], dtype=np.uint8)
    if len(bits) < 160:
        return set()
    windows = np.lib.stride_tricks.sliding_window_view(bits, 8)
    flags = np.flatnonzero(np.all(windows == flag, axis=1))
    found = set()
    for left, right in zip(flags, flags[1:]):
        stuffed = bits[left + 8:right]
        if not 144 <= len(stuffed) <= 8192:
            continue
        decoded, ones, valid = [], 0, True
        for value in stuffed:
            bit = int(value)
            if ones == 5:
                if bit:
                    valid = False
                    break
                ones = 0
                continue
            decoded.append(bit)
            ones = ones + 1 if bit else 0
        if not valid or ones == 5 or len(decoded) % 8:
            continue
        frame = np.packbits(np.asarray(decoded, dtype=np.uint8), bitorder='little').tobytes()
        if _valid_ui(frame):
            found.add(frame)
    return found


def decode_ax25_afsk(samples, sample_rate):
    """Return protocol evidence, or None. Only clean AFSK1200 UI frames supported.

    Samples must contain one isolated complex FM channel. Exhausting the small
    fixed timing search is not evidence that a recording lacks AX.25 traffic.
    """
    if not math.isfinite(sample_rate) or sample_rate < 6000:
        return None
    z = np.asarray(samples)
    if z.ndim != 1 or len(z) < sample_rate * .05 or not np.isfinite(z).all():
        return None
    # FM discriminator removes a constant carrier offset after mean subtraction.
    audio = np.angle(z[1:] * z[:-1].conj())
    audio -= np.mean(audio)
    if np.std(audio) < 1e-8:
        return None
    ratio = Fraction(9600 / float(sample_rate)).limit_denominator(10000)
    audio = resample_poly(audio, ratio.numerator, ratio.denominator)
    # Eight samples per symbol. Limit rate mismatch introduced by rational rounding.
    if abs(sample_rate * ratio.numerator / ratio.denominator - 9600) > .01:
        return None
    t = np.arange(len(audio)) / 9600
    kernel = np.ones(8) / 8
    mark = np.convolve(audio * np.exp(-2j * np.pi * 1200 * t), kernel, mode='same')
    space = np.convolve(audio * np.exp(-2j * np.pi * 2200 * t), kernel, mode='same')
    decision = np.abs(mark) ** 2 > np.abs(space) ** 2
    frames = set()
    for phase in range(8):
        tones = decision[phase::8]
        bits = (tones[1:] == tones[:-1]).astype(np.uint8)  # NRZI: transition = 0
        frames.update(_frames(bits))
    if not frames:
        return None
    return {'name': 'AX.25', 'status': 'confirmed', 'evidence': [
        f'{len(frames)} distinct UI frame(s) passed HDLC framing, address and CRC-16/X-25 validation',
        'Bell 202 AFSK, 1200 baud, FM discriminator; payload application not identified',
    ]}
