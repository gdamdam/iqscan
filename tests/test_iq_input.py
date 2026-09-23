import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iq_input import fingerprint, read_metadata, read_samples, relocated_source, verify_source, write_sigmf


class InputTests(unittest.TestCase):
    def test_raw_formats_and_component_order(self):
        cases = (
            ("cs8", np.array([[64, -64], [-128, 127]], dtype="i1"), 128.0),
            ("cs16", np.array([[16384, -16384], [-32768, 32767]], dtype="<i2"), 32768.0),
            ("cu8", np.array([[255, 0], [128, 127]], dtype="u1"), 127.5),
            ("cf32_le", np.array([[.25, -.5], [1.25, -.125]], dtype="<f4"), 1.0),
            ("cf32_be", np.array([[.25, -.5], [1.25, -.125]], dtype=">f4"), 1.0),
        )
        with tempfile.TemporaryDirectory() as directory:
            for fmt, pairs, scale in cases:
                path = Path(directory) / f"test_32000SPS_455000000Hz.{fmt}"
                path.write_bytes(pairs.tobytes())
                for order in ("IQ", "QI"):
                    meta = read_metadata(path, iq_order=order)
                    self.assertEqual(meta["format"], fmt)
                    self.assertEqual(meta["bytes_per_complex"], pairs.dtype.itemsize * 2)
                    self.assertEqual(meta["samples"], 2)
                    self.assertEqual(meta["center_frequency_hz"], 455000000)
                    expected = pairs.astype(float)
                    if fmt == "cu8":
                        expected = (expected - 127.5) / scale
                    elif fmt != "cf32_le" and fmt != "cf32_be":
                        expected = expected / scale
                    if order == "QI":
                        expected = expected[:, ::-1]
                    np.testing.assert_allclose(read_samples(meta, 0, 2), expected[:, 0] + 1j * expected[:, 1])
                    np.testing.assert_allclose(read_samples(meta, 1, 10), (expected[:, 0] + 1j * expected[:, 1])[1:])
                    self.assertEqual(len(read_samples(meta, 2, 4)), 0)
            alias = Path(directory) / "recording.cf32"
            alias.write_bytes(cases[3][1].tobytes())
            self.assertEqual(read_metadata(alias, sample_rate=32000)["format"], "cf32_le")

    def test_invalid_metadata_and_float_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.cs8"
            path.write_bytes(b"\0\1\2")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                read_metadata(path, sample_rate=32000)
            path.write_bytes(b"\0\1")
            for rate in (float("nan"), float("inf"), 0):
                with self.assertRaisesRegex(ValueError, "positive and finite"):
                    read_metadata(path, sample_rate=rate)
            with self.assertRaisesRegex(ValueError, "iq_order"):
                read_metadata(path, sample_rate=32000, iq_order="bad")
            floating = Path(directory) / "recording.cf32_be"
            floating.write_bytes(np.array([[1, 2], [float("nan"), 0]], dtype=">f4").tobytes())
            with self.assertRaisesRegex(ValueError, "non-finite"):
                read_metadata(floating, sample_rate=32000)
            floating.write_bytes(np.array([[1, 2]], dtype=">f4").tobytes())
            meta = read_metadata(floating, sample_rate=32000)
            with floating.open("r+b") as handle:
                handle.seek(4)
                handle.write(struct.pack(">f", float("inf")))
            with self.assertRaisesRegex(ValueError, "non-finite"):
                read_samples(meta, 0, 1)

    def test_sigmf_metadata_and_capture_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "test"
            metadata = base.with_suffix(".sigmf-meta")
            payload = base.with_suffix(".sigmf-data")
            payload.write_bytes(np.array([[1, -1], [2, -2]], dtype="<i2").tobytes())
            document = {"global": {"core:datatype": "ci16_le", "core:sample_rate": 48000},
                        "captures": [{"core:sample_start": 0, "core:frequency": 137900000}]}
            metadata.write_text(json.dumps(document))
            for path in (metadata, payload):
                meta = read_metadata(path)
                self.assertEqual(meta["input"], str(payload.resolve()))
                self.assertEqual((meta["format"], meta["sample_rate"], meta["center_frequency_hz"]),
                                 ("cs16", 48000, 137900000))
                np.testing.assert_allclose(read_samples(meta, 0, 2), np.array([1-1j, 2-2j]) / 32768)
            with self.assertRaisesRegex(ValueError, "conflicts"):
                read_metadata(metadata, sample_rate=32000)
            document["captures"].append({"core:sample_start": 1, "core:frequency": 138000000})
            metadata.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "multiple captures"):
                read_metadata(metadata)
            document["captures"] = [{"core:sample_start": 1, "core:frequency": 137900000}]
            metadata.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "offsets"):
                read_metadata(metadata)
            document["captures"] = [{"core:sample_start": 0, "core:header_bytes": 4,
                                     "core:frequency": 137900000}]
            metadata.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "header bytes"):
                read_metadata(metadata)
            document["captures"][0].pop("core:header_bytes")
            for key, value, message in (("core:trailing_bytes", 4, "trailing bytes"),
                                        ("core:num_channels", 2, "multiple interleaved channels")):
                document["global"][key] = value
                metadata.write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, message):
                    read_metadata(metadata)
                document["global"].pop(key)

    def test_wav_payload_and_source_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = np.arange(8, dtype="<i2").tobytes()
            fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 32000, 128000, 4, 16)
            body = b"WAVE" + fmt + b"data" + struct.pack("<I", len(payload)) + payload + b"JUNK" + struct.pack("<I", 4) + b"abcd"
            path = root / "one_137900000Hz.wav"
            path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
            meta = read_metadata(path)
            self.assertEqual((meta["data_offset"], meta["bytes"], meta["samples"]), (44, len(payload), 4))
            np.testing.assert_allclose(read_samples(meta, 1, 2), np.array([2+3j, 4+5j]) / 32768)
            meta["source_fingerprint"] = fingerprint(meta)
            moved = root / "two_137900000Hz.wav"
            shutil.copyfile(path, moved)
            self.assertTrue(verify_source(meta))
            self.assertTrue(verify_source(meta, moved))
            with moved.open("r+b") as handle:
                handle.seek(48)
                handle.write(b"\xff")
            self.assertFalse(verify_source(meta, moved))
            moved.write_bytes(path.read_bytes())
            with moved.open("r+b") as handle:
                handle.seek(24)
                handle.write(struct.pack("<I", 44100))
            self.assertFalse(verify_source(meta, moved))
            region = dict(meta)
            region.update(data_offset=meta["data_offset"] + 4, bytes=8, samples=2, duration_s=2/32000)
            region["source_fingerprint"] = fingerprint(region)
            self.assertTrue(verify_source(region, path))
            self.assertFalse(verify_source(region, moved))

    def test_region_fingerprint_retains_offset_and_sigmf_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "one_32000SPS.cs8"
            data.write_bytes(bytes(range(64)))
            meta = read_metadata(data)
            meta.update(data_offset=8, bytes=16, samples=8, duration_s=8/32000)
            meta["source_fingerprint"] = fingerprint(meta)
            moved = root / "two_32000SPS.cs8"
            shutil.copyfile(data, moved)
            self.assertTrue(verify_source(meta, moved))
            modified = bytearray(moved.read_bytes())
            modified[8] ^= 1
            moved.write_bytes(modified)
            self.assertFalse(verify_source(meta, moved))
            base = root / "sig"
            sigdata = base.with_suffix(".sigmf-data")
            sigmeta = base.with_suffix(".sigmf-meta")
            sigdata.write_bytes(bytes(range(8)))
            doc = {"global": {"core:datatype": "ci8", "core:sample_rate": 32000},
                   "captures": [{"core:sample_start": 0, "core:frequency": 137900000}]}
            sigmeta.write_text(json.dumps(doc))
            sig = read_metadata(sigmeta)
            sig["source_fingerprint"] = fingerprint(sig)
            self.assertTrue(verify_source(sig))
            doc["captures"][0]["core:frequency"] = 138000000
            sigmeta.write_text(json.dumps(doc))
            self.assertFalse(verify_source(sig))

    def test_sigmf_sidecar_roundtrip_for_nonstandard_data_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "clip.cs16"
            payload.write_bytes(np.array([[32767, -32768], [8192, -8192]], dtype="<i2").tobytes())
            sidecar = write_sigmf(payload, 32000, 137900000, "cs16")
            self.assertEqual(sidecar.name, "clip.cs16.sigmf-meta")
            document = json.loads(sidecar.read_text())
            self.assertEqual(document["global"]["core:dataset"], "clip.cs16")
            for path in (payload, sidecar):
                meta = read_metadata(path)
                self.assertEqual(meta["input"], str(payload.resolve()))
                self.assertEqual(meta["format"], "cs16")
                np.testing.assert_allclose(read_samples(meta, 0, 2),
                                           np.array([32767-32768j, 8192-8192j]) / 32768)
            meta["source_fingerprint"] = fingerprint(meta)
            new_payload = root / "moved.cs16"
            new_sidecar = root / "moved.cs16.sigmf-meta"
            shutil.copyfile(payload, new_payload)
            write_sigmf(new_payload, 32000, 137900000, "ci16_le")
            self.assertTrue(new_sidecar.exists())
            self.assertTrue(verify_source(meta, new_sidecar))
            self.assertEqual(relocated_source(meta, new_sidecar)["input"], str(new_payload.resolve()))
            with self.assertRaisesRegex(ValueError, "I then Q"):
                write_sigmf(payload, 32000, 137900000, "cs16", iq_order="QI")

    def test_sigmf_compliant_data_name_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "channel.sigmf-data"
            payload.write_bytes(np.array([[.25, -.5]], dtype="<f4").tobytes())
            sidecar = write_sigmf(payload, 48000, None, "cf32_le")
            self.assertEqual(sidecar.name, "channel.sigmf-meta")
            self.assertNotIn("core:dataset", json.loads(sidecar.read_text())["global"])
            meta = read_metadata(sidecar)
            np.testing.assert_allclose(read_samples(meta, 0, 1), [.25-.5j])
            write_sigmf(payload, 48000, 0, "cf32_le")
            self.assertEqual(read_metadata(sidecar)["center_frequency_hz"], 0)
            write_sigmf(payload, 48000, -24000, "cf32_le")
            self.assertEqual(read_metadata(sidecar)["center_frequency_hz"], -24000)

    def test_explicit_raw_metadata_survives_opaque_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "wrong_12000SPS_100Hz.data"
            path.write_bytes(bytes(range(32)))
            meta = read_metadata(path, format="cs8", sample_rate=32000, center_frequency=137900000)
            meta["source_fingerprint"] = fingerprint(meta)
            self.assertTrue(verify_source(meta))
            moved = root / "renamed_blob"
            shutil.copyfile(path, moved)
            self.assertTrue(verify_source(meta, moved))
            self.assertEqual(relocated_source(meta, moved)["input"], str(moved.resolve()))


if __name__ == "__main__":
    unittest.main()
