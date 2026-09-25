import unittest
import tempfile
from pathlib import Path
import numpy as np
from iqscan.protocol_decode import _fcs, decode_ax25_afsk


def packet_iq(bad_crc=False, fs=48000, carrier=730, seed=9):
    def address(call, last):
        return bytes([ord(c) << 1 for c in call.ljust(6)] + [0x60 | last])
    payload = address('APRS', 0) + address('N0CALL', 1) + b'\x03\xf0test 12345'
    checksum = _fcs(payload) ^ int(bad_crc)
    packet = payload + checksum.to_bytes(2, 'little')
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    stuffed, ones = [], 0
    for byte in packet:
        for i in range(8):
            bit = byte >> i & 1
            stuffed.append(bit)
            ones = ones + 1 if bit else 0
            if ones == 5:
                stuffed.append(0)
                ones = 0
    bits = flag * 20 + stuffed + flag * 10
    state, tones = 0, []
    for bit in bits:
        if not bit:
            state ^= 1
        tones.append(1200 if state else 2200)
    frequencies = np.repeat(tones, fs // 1200)
    audio = np.sin(2 * np.pi * np.cumsum(frequencies) / fs)
    z = .6 * np.exp(2j * np.pi * np.cumsum(carrier + 2400 * audio) / fs)
    rng = np.random.default_rng(seed)
    return z + .006 * (rng.normal(size=len(z)) + 1j * rng.normal(size=len(z)))


class ProtocolTests(unittest.TestCase):
    def test_x25_check_vector(self):
        self.assertEqual(_fcs(b'123456789'), 0x906e)

    def test_valid_fm_packet_with_offset_noise_and_timing_shift(self):
        for fs in (24000, 48000):
            with self.subTest(fs=fs):
                result = decode_ax25_afsk(packet_iq(fs=fs)[13:], fs)
                self.assertIsNotNone(result)
                self.assertEqual(result['name'], 'AX.25')
                self.assertEqual(result['status'], 'confirmed')

    def test_corrupt_packet_noise_tone_and_short_input_not_confirmed(self):
        self.assertIsNone(decode_ax25_afsk(packet_iq(bad_crc=True), 48000))
        rng = np.random.default_rng(4)
        for z in (rng.normal(size=48000) + 1j * rng.normal(size=48000),
                  np.exp(2j * np.pi * np.arange(48000) * .1), np.ones(100)):
            self.assertIsNone(decode_ax25_afsk(z, 48000))

    def test_event_analysis_confirms_valid_packet_without_frequency_catalog(self):
        from iqscan.signal_analysis import analyze_events
        fs = 48000
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                z = packet_iq(bad_crc=corrupt, carrier=7000)
                path = Path(directory) / 'terrestrial.cs16'
                np.round(np.column_stack((z.real, z.imag)) * 32767).astype('<i2').tofile(path)
                event = dict(start_s=0, end_s=len(z)/fs, peak_time_s=len(z)/fs/2,
                             center_offset_hz=7000, bandwidth_hz=12000)
                meta = dict(input=str(path), sample_rate=fs, format='cs16', data_offset=0)
                analyze_events(meta, [event])
                result = event['signal_analysis']
                self.assertEqual(result['protocol']['status'], 'unconfirmed' if corrupt else 'confirmed')
                if not corrupt:
                    self.assertEqual(result['protocol']['name'], 'AX.25')
                    self.assertEqual(result['symbol_rate_baud'], 1200)

    def test_high_rate_packet_survives_streaming_decimation(self):
        from scipy.signal import resample_poly
        from iqscan.signal_analysis import MAX_SAMPLES, analyze_events
        fs = 2_400_000
        base = packet_iq(carrier=730)
        z = resample_poly(base, 50, 1)
        z *= np.exp(2j * np.pi * 7000 * np.arange(len(z)) / fs)
        self.assertGreater(len(z), MAX_SAMPLES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'high_rate.cs16'
            np.clip(np.round(np.column_stack((z.real, z.imag)) * 32767),
                    -32768, 32767).astype('<i2').tofile(path)
            event = dict(start_s=0, end_s=len(z)/fs, peak_time_s=len(z)/fs/2,
                         center_offset_hz=7000, bandwidth_hz=12000)
            meta = dict(input=str(path), sample_rate=fs, format='cs16',
                        data_offset=0, samples=len(z), bytes_per_complex=4)
            result = analyze_events(meta, [event])[0]['signal_analysis']
            self.assertEqual(result['protocol']['status'], 'confirmed')
            self.assertEqual(result['symbol_rate_baud'], 1200)
            window = result['features']['analysis_windows'][0]
            self.assertGreater(window['end_s'] - window['start_s'], .35)
            self.assertEqual(window['sample_rate_hz'], 48000)
            self.assertLessEqual(window['n_samples'], MAX_SAMPLES)

    def test_detected_packet_burst_is_decoded_despite_off_air_envelope(self):
        from iqscan import iq_scan
        from iqscan.signal_analysis import analyze_events
        fs = 48000
        rng = np.random.default_rng(14)
        z = .003 * (rng.normal(size=fs*2) + 1j*rng.normal(size=fs*2))
        packet = packet_iq(carrier=7000)
        z[24000:24000+len(packet)] += packet
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'terrestrial_48000SPS.cs16'
            np.clip(np.round(np.column_stack((z.real, z.imag))*32767), -32768, 32767).astype('<i2').tofile(path)
            args = iq_scan.parser().parse_args([str(path), '--fft-size', '1024', '--time-bin', '.032',
                                                '--min-duration', '.06', '--dc-exclude', '1000'])
            meta = iq_scan.metadata(args)
            f, norm, _reference, dt = iq_scan.spectrum(meta, args)
            events = iq_scan.detect(meta, args, f, norm, dt)
            analyze_events(meta, events)
            self.assertTrue(any(e['signal_analysis']['protocol']['status'] == 'confirmed' for e in events))
