import sys
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iqscan.signal_analysis import MAX_SAMPLES, analyze_events, analyze_samples


class SignalAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fs = 48_000.0
        self.rng = np.random.default_rng(12)

    def test_noise_is_unknown_and_json_safe(self):
        z = self.rng.normal(size=12_000) + 1j * self.rng.normal(size=12_000)
        result = analyze_samples(z, self.fs)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(result["symbol_rate_baud"])
        self.assertEqual(result["protocol"]["status"], "unconfirmed")

    def test_tone_and_short_signal(self):
        t = np.arange(12_000) / self.fs
        tone = 0.6 * np.exp(2j * np.pi * 6_000 * t)
        result = analyze_samples(tone, self.fs)
        self.assertEqual(result["status"], "candidate")
        self.assertIn("carrier/tone", {x["modulation"] for x in result["candidates"]})
        result = analyze_samples(tone[:100], self.fs)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(any("short" in x for x in result["warnings"]))

    def test_am_and_frequency_shifted_fsk_are_family_candidates(self):
        n = 24_000
        t = np.arange(n) / self.fs
        symbol = ((np.arange(n) // 240) % 2).astype(float)
        am = (0.12 + 0.52 * symbol) * np.exp(2j * np.pi * 5_000 * t)
        am_result = analyze_samples(am, self.fs)
        self.assertEqual(am_result["status"], "candidate")
        self.assertIn("OOK", {x["modulation"] for x in am_result["candidates"]})

        freq = 4_000 + 1_000 * (2 * symbol - 1)
        fsk = 0.45 * np.exp(2j * np.pi * np.cumsum(freq) / self.fs)
        fsk_result = analyze_samples(fsk, self.fs)
        self.assertEqual(fsk_result["status"], "candidate")
        self.assertTrue({x["modulation"] for x in fsk_result["candidates"]} & {"FM", "FSK"})

    def test_invalid_and_bounded_input(self):
        result = analyze_samples(np.ones(MAX_SAMPLES + 100, dtype=np.complex64), self.fs)
        self.assertEqual(result["features"]["n_samples"], MAX_SAMPLES)
        self.assertTrue(any("bounded" in x for x in result["warnings"]))
        result = analyze_samples(np.ones(100, dtype=np.float32), self.fs)
        self.assertEqual(result["status"], "unknown")

    def test_cs8_cs16_wav_and_adjacent_channel_filtering(self):
        n = 48_000
        t = np.arange(n) / self.fs
        # The wanted event is at +6 kHz and a much stronger adjacent signal is
        # at +15 kHz. Mixing and the event bandwidth should suppress the latter.
        z = 0.18 * np.exp(2j * np.pi * 6_000 * t) + 0.75 * np.exp(2j * np.pi * 15_000 * t)
        event = {"start_s": 0.25, "end_s": 0.75, "peak_time_s": 0.5,
                 "center_offset_hz": 6_000, "bandwidth_hz": 2_000}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for fmt, scale, dtype in (("cs8", 128, np.int8), ("cs16", 32768, np.int16)):
                raw = np.column_stack((z.real, z.imag))
                encoded = np.clip(np.rint(raw * scale), -scale, scale - 1).astype(dtype)
                path = root / ("capture." + fmt)
                encoded.tofile(path)
                meta = {"input": str(path), "format": fmt, "data_offset": 0,
                        "sample_rate": self.fs}
                result = analyze_events(meta, [dict(event)])[0]["signal_analysis"]
                self.assertEqual(result["status"], "candidate")
                self.assertLess(abs(result["features"].get("dominant_frequency_hz", 99_999)), 500)

            # IQ WAV: 44-byte PCM16 header plus the same interleaved payload.
            wav_path = root / "capture.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(int(self.fs))
                handle.writeframes(np.clip(np.rint(np.column_stack((z.real, z.imag)) * 32768), -32768, 32767).astype("<i2").tobytes())
            meta = {"input": str(wav_path), "format": "wav", "data_offset": 44,
                    "sample_rate": self.fs}
            result = analyze_events(meta, [dict(event)])[0]["signal_analysis"]
            self.assertLess(abs(result["features"].get("dominant_frequency_hz", 99_999)), 500)

    def test_missing_and_tiny_event_are_unknown(self):
        event = {"start_s": 0, "end_s": .001, "peak_time_s": .0005, "center_offset_hz": 0}
        result = analyze_events({"input": "/does/not/exist.cs8", "format": "cs8", "sample_rate": 48_000}, [event])[0]
        self.assertEqual(result["signal_analysis"]["status"], "unknown")
        self.assertTrue(result["signal_analysis"]["warnings"])

    def test_filtered_noise_and_analog_fm_do_not_invent_psk_or_baud(self):
        from iqscan.signal_analysis import _channelize
        n = 24000
        t = np.arange(n) / self.fs
        noise = np.random.default_rng(25).normal(size=n) + 1j*np.random.default_rng(29).normal(size=n)
        for bandwidth in (1000, 10000, 20000):
            z, fs, _ = _channelize(noise, self.fs, 0, bandwidth)
            result = analyze_samples(z, fs, analysis_bandwidth_hz=bandwidth)
            self.assertEqual(result['status'], 'unknown')
            json.dumps(result, allow_nan=False)
        for deviation in (1000, 3000):
            fm = np.exp(2j*np.pi*np.cumsum(deviation*np.sin(2*np.pi*1000*t))/self.fs)
            result = analyze_samples(fm, self.fs)
            self.assertIsNone(result['symbol_rate_baud'])
            self.assertFalse({'PSK','BPSK','QPSK'} & {c['modulation'] for c in result['candidates']})

    def test_stream_filter_rejects_adjacent_tone_before_decimation(self):
        from iqscan.signal_analysis import _stream_channelize
        fs = 2_400_000
        n = 240_000
        t = np.arange(n) / fs
        wanted = .25 * np.exp(2j * np.pi * 7000 * t)
        adjacent = .75 * np.exp(2j * np.pi * 110000 * t)
        z = wanted + adjacent
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'adjacent.cf32_le'
            np.column_stack((z.real, z.imag)).astype('<f4').tofile(path)
            meta = dict(input=str(path), format='cf32_le', sample_rate=fs,
                        bytes_per_complex=8, samples=n, data_offset=0, iq_order='IQ')
            channel, out_fs, warnings = _stream_channelize(meta, 0, n, 7000, 12000)
            self.assertEqual(out_fs, 48000)
            self.assertEqual(warnings, [])
            self.assertTrue(np.isfinite(channel).all())
            # The FIR state crosses the 65,536-source-sample read boundary.
            boundary = 65536 // 50
            self.assertLess(np.max(abs(np.diff(channel[boundary-10:boundary+10]))), .01)
            limited, _, bound_warnings = _stream_channelize(
                meta, 0, n, 7000, 12000, max_output_samples=1000)
            self.assertEqual(len(limited), 1000)
            self.assertTrue(any('sample limit' in warning for warning in bound_warnings))
            spectrum = abs(np.fft.fft(channel[1000:]))
            self.assertLess(np.max(spectrum[abs(np.fft.fftfreq(len(spectrum), 1/out_fs)) > 2000]),
                            np.max(spectrum) * .01)

    def test_long_event_windows_obey_work_and_output_limits(self):
        fs = 48000
        n = fs * 10
        z = .5 * np.exp(2j * np.pi * 7000 * np.arange(n) / fs)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'long.cs16'
            np.round(np.column_stack((z.real, z.imag)) * 32767).astype('<i2').tofile(path)
            meta = dict(input=str(path), format='cs16', sample_rate=fs,
                        bytes_per_complex=4, samples=n, data_offset=0)
            event = dict(start_s=0, end_s=10, peak_time_s=5,
                         center_offset_hz=7000, bandwidth_hz=2000)
            result = analyze_events(meta, [event], max_samples=20000,
                                    duration_seconds=1.5)[0]['signal_analysis']
            windows = result['features']['analysis_windows']
            self.assertEqual(len(windows), 3)
            self.assertLessEqual(sum(w['source_samples'] for w in windows), fs * 1.5)
            self.assertTrue(all(w['n_samples'] <= 20000 for w in windows))
            self.assertIn('duration_limit', result['features']['truncation_reasons'])
            json.dumps(result, allow_nan=False)

    def test_declared_payload_end_and_float_qi_are_honored(self):
        fs = 48000
        n = 4800
        t = np.arange(n) / fs
        wanted = .5 * np.exp(2j * np.pi * 6000 * t)
        trailing = .9 * np.exp(2j * np.pi * 12000 * t)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bounded.cf32_be'
            z = np.r_[wanted, trailing]
            np.column_stack((z.imag, z.real)).astype('>f4').tofile(path)
            meta = dict(input=str(path), format='cf32_be', sample_rate=fs,
                        bytes_per_complex=8, samples=n, data_offset=0, iq_order='QI')
            event = dict(start_s=0, end_s=.3, peak_time_s=.05,
                         center_offset_hz=6000, bandwidth_hz=2000)
            result = analyze_events(meta, [event])[0]['signal_analysis']
            self.assertLessEqual(result['features']['analysis_windows'][0]['end_s'], .1)
            self.assertLess(abs(result['features']['dominant_frequency_hz']), 500)

    def test_high_rate_filtered_noise_and_invalid_duration(self):
        fs = 2_400_000
        n = 240_000
        rng = np.random.default_rng(81)
        noise = .1 * (rng.normal(size=n) + 1j * rng.normal(size=n))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'noise.cs16'
            np.round(np.column_stack((noise.real, noise.imag)) * 32767).astype('<i2').tofile(path)
            meta = dict(input=str(path), format='cs16', sample_rate=fs,
                        bytes_per_complex=4, samples=n, data_offset=0)
            event = dict(start_s=0, end_s=n/fs, peak_time_s=n/fs/2,
                         center_offset_hz=7000, bandwidth_hz=12000)
            result = analyze_events(meta, [dict(event)])[0]['signal_analysis']
            self.assertEqual(result['protocol']['status'], 'unconfirmed')
            self.assertEqual(result['status'], 'unknown')
            bad = analyze_events(meta, [dict(event)], duration_seconds=0)[0]['signal_analysis']
            self.assertEqual(bad['status'], 'unknown')
            self.assertTrue(any('duration' in w for w in bad['warnings']))

    def test_psk_candidates_survive_carrier_offset_and_amplitude_scaling(self):
        n = 24000
        t = np.arange(n) / self.fs
        rng = np.random.default_rng(18)
        for order, name in ((2, 'BPSK'), (4, 'QPSK')):
            symbols = np.repeat(rng.integers(0, order, n//24), 24)
            noise = .01*(rng.normal(size=n)+1j*rng.normal(size=n))
            for offset in (0, 3300):
                for scale in (.1, .8):
                    z = scale*(np.exp(2j*np.pi*(symbols/order+offset*t))+noise)
                    result = analyze_samples(z, self.fs)
                    self.assertIn(name, {c['modulation'] for c in result['candidates']})
                    self.assertIsNone(result['symbol_rate_baud'])
                    self.assertEqual(result['protocol']['status'], 'unconfirmed')


if __name__ == "__main__":
    unittest.main()
