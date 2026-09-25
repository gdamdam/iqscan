import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from iqscan import iq_scan
from iqscan.signal_analysis import analyze_events


class SignalIntegrationTests(unittest.TestCase):
    def test_cli_flag_is_opt_in_and_missing_source_is_unknown(self):
        args = iq_scan.parser().parse_args(["capture.cs8", "--sample-rate", "32000", "--analyze-signals"])
        self.assertTrue(args.analyze_signals)
        event = dict(id=1, start_s=0.1, end_s=0.3, peak_time_s=0.2, center_offset_hz=1000.0)
        analyze_events({"input": "/does/not/exist.cs8", "format": "cs8", "sample_rate": 32000}, [event])
        self.assertEqual(event["signal_analysis"]["status"], "unknown")
        self.assertIn("recording is unavailable", event["signal_analysis"]["warnings"])

    def test_report_exports_analysis_and_escapes_html_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "capture_16000SPS.cs8"
            source.write_bytes(bytes(4096))
            out = root / "report"
            out.mkdir()
            meta = dict(input=str(source), sample_rate=16000.0, format="cs8", center_frequency_hz=None,
                        duration_s=1.0, frequency_bin_hz=1000.0)
            args = iq_scan.parser().parse_args([str(source), "--sample-rate", "16000", "--clips", "0"])
            event = dict(id=1, kind="transient", start_s=.1, end_s=.6, duration_s=.5,
                         peak_time_s=.3, low_offset_hz=1000.0, high_offset_hz=3000.0,
                         center_offset_hz=2000.0, bandwidth_hz=2000.0, contrast_db=12.0,
                         frequency_hz=None,
                         signal_analysis={"status": "candidate", "candidates": [{
                             "modulation": "AM", "confidence": "low",
                             "evidence": ["</script><script>alert(1)</script>"]}],
                             "features": {}, "symbol_rate_baud": 1200.0,
                             "protocol": {"name": "AX.25", "status": "confirmed", "evidence": ["valid HDLC frame and FCS"]},
                             "warnings": []})
            f = np.linspace(-8000, 7000, 16)
            norm = np.zeros((2, 16), dtype=float)
            reference = np.zeros(2, dtype=float)
            iq_scan.report(meta, args, [event], f, norm, reference, .5, out)
            page = (out / "report.html").read_text()
            self.assertIn("Signal analysis (candidate evidence)", page)
            self.assertIn("&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;", page)
            self.assertIn("AX.25 (confirmed): valid HDLC frame and FCS", page)
            self.assertNotIn("<td>AM (low): </script>", page)
            payload = json.loads((out / "events.json").read_text())["events"][0]
            self.assertEqual(payload["signal_analysis"]["symbol_rate_baud"], 1200.0)
            with (out / "events.csv").open(newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertIn("signal_analysis", row)
            self.assertIn("AX.25", row["signal_analysis"])


if __name__ == "__main__":
    unittest.main()
