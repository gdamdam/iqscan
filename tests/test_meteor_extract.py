import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import iq_scan
import meteor_extract


class MeteorExtractionTests(unittest.TestCase):
    def test_cli_style_detection_and_override(self):
        with patch('meteor_extract.subprocess.run', return_value=Mock(stdout='SatDump v2.0.0-alpha\nSUBCOMMANDS:', stderr='')):
            self.assertEqual(meteor_extract.cli_style(Path('/fake/satdump'), 'auto'), 'v2')
        with patch('meteor_extract.subprocess.run', return_value=Mock(stdout='', stderr='Usage : /Applications/_RADIO/SatDump.app/Contents/MacOS/satdump [pipeline_id] [input_level] [input_file]')):
            self.assertEqual(meteor_extract.cli_style(Path('/fake/satdump'), 'auto'), 'stable')
        self.assertEqual(meteor_extract.cli_style(Path('/fake/satdump'), 'stable'), 'stable')

    def test_decoder_products_survive_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            recording = root / '2026-09-23_22-41-11_1000000SPS_137500000Hz.cs16'
            recording.write_bytes(bytes(100_000 * 4))
            fake = root / 'satdump-fake'
            fake.write_text('#!' + sys.executable + '\n'
                            'import json,sys\n'
                            'from pathlib import Path\n'
                            'out=Path(sys.argv[5]); (out/"MSU-MR").mkdir(parents=True)\n'
                            '(out/"MSU-MR"/"MSU-MR-1.png").write_bytes(b"PNG")\n'
                            '(out/"telemetry.json").write_text(json.dumps([{"samples": 2}]))\n'
                            '(out/"meteor_m2-x_lrpt.cadu").write_bytes(b"CADU")\n'
                            'print("partial decode before final crash")\n'
                            'sys.exit(9)\n')
            fake.chmod(0o755)
            out = root / 'result'
            rc = iq_scan.main(['meteor', str(recording), '--satellite', 'M2-4',
                               '--satdump', str(fake), '--satdump-cli', 'v2', '--output', str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual((out / 'images' / 'MSU-MR-1.png').read_bytes(), b'PNG')
            doc = json.loads((out / 'extraction.json').read_text())
            self.assertEqual(doc['satdump_exit_code'], 9)
            self.assertEqual(doc['images'], ['images/MSU-MR-1.png'])
            self.assertTrue(any(x['path'] == 'satdump/telemetry.json' for x in doc['data_products']))
            self.assertEqual(doc['data_summary']['telemetry_records'], 1)
            self.assertIn('partial decode', (out / 'satdump.log').read_text())
            self.assertEqual(iq_scan.main(['meteor', str(recording), '--satellite', 'M2-4',
                                           '--satdump', str(fake), '--satdump-cli', 'v2', '--output', str(out)]), 2)

    def test_wrong_band_is_rejected_before_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            recording = root / 'capture_1000000SPS_137500000Hz.cs16'
            recording.write_bytes(bytes(4096))
            out = root / 'new'
            rc = meteor_extract.main([str(recording), '--satellite', 'M2-3',
                                      '--frequency', '139000000', '--output', str(out)])
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()


class MeteorVideoFontTests(unittest.TestCase):
    def test_missing_system_fonts_fall_back_to_bundled_font(self):
        try:
            import meteor_video
        except ImportError as exc:
            self.skipTest(f'video dependencies unavailable: {exc}')
        font = meteor_video.load_font(('/nonexistent/Arial.ttf', 'no-such-font.ttf'), 21)
        self.assertIsNotNone(font.getbbox('Meteor'))
