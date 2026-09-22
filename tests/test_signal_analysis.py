import sys
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from signal_analysis import MAX_SAMPLES, analyze_events, analyze_samples


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
        from signal_analysis import _channelize
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
