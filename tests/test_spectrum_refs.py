import argparse,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import spectrum_refs as refs

class ReferenceTests(unittest.TestCase):
    def args(self,*argv):
        p=argparse.ArgumentParser();refs.add_arguments(p);return p.parse_args(argv)

    def test_plan_parsing_and_comment_context(self):
        data=b'<ArrayOfRangeEntry><RangeEntry minFrequency="100" maxFrequency="200" mode="FM">Ch.8</RangeEntry><!-- Example service channel 8 --></ArrayOfRangeEntry>'
        rows=refs.bandplan(data)
        self.assertEqual(rows[0]['low_hz'],100)
        self.assertIn('Example service',rows[0]['name'])

    def test_boundary_and_tolerance(self):
        args=self.args('--match-tolerance','5')
        catalog=dict(bands=[dict(low_hz=100,high_hz=200,name='A'),dict(low_hz=200,high_hz=300,name='B')],signals=[dict(low_hz=214,high_hz=214,name='near'),dict(low_hz=216,high_hz=216,name='far')],sources=[],warnings=[],region='us')
        meta=dict(center_frequency_hz=200,sample_rate=100)
        events=[dict(low_offset_hz=0,high_offset_hz=10)]
        refs.describe(meta,events,catalog,args)
        self.assertEqual([r['name'] for r in events[0]['band_references']],['B'])
        self.assertEqual([r['name'] for r in events[0]['known_signal_hints']],['near'])
        self.assertEqual(events[0]['known_signal_hints'][0]['frequency_gap_hz'],4)

    def test_unknown_center_and_custom_catalog(self):
        args=self.args('--bandplan','none','--known-signals','none')
        catalog=refs.load(args);meta=dict(center_frequency_hz=None)
        refs.describe(meta,[],catalog,args);self.assertIn('No absolute',meta['spectrum_context']['warning'])
        self.assertEqual(refs.custom(b'[{"name":"Test","frequency_hz":123}]')[0]['high_hz'],123)
        with self.assertRaises(ValueError):refs.custom(b'[{"name":"Bad","low_hz":200,"high_hz":100}]')

    def test_offline_never_fetches_and_cache_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=self.args('--reference-cache',tmp,'--offline-references')
            with patch('spectrum_refs.urlopen',side_effect=AssertionError('network forbidden')):
                with self.assertRaisesRegex(ValueError,'no cached'):refs.download('x','https://example.com',refs.read_json,args)
                path=Path(tmp)/'x.json'
                path.write_text(json.dumps(dict(source_url='https://example.com',downloaded_utc='2020-01-01T00:00:00+00:00',raw='[{"name":"a"}]')))
                rows,info=refs.download('x','https://example.com',refs.read_json,args)
                self.assertEqual(rows[0]['name'],'a');self.assertIn('stale',info['warning'])
            args.offline_references=False
            with patch('spectrum_refs.urlopen',side_effect=OSError('network down')):
                rows,info=refs.download('x','https://example.com',refs.read_json,args)
                self.assertIn('Download failed',info['warning'])

    def test_html_escapes_custom_names(self):
        c=dict(bands=[dict(name='<script>alert(1)</script>',low_hz=0,high_hz=10)],known_signals=[],sources=[],warnings=[])
        output=refs.html_context({'spectrum_context':c})
        self.assertNotIn('<script>',output);self.assertIn('&lt;script&gt;',output)

if __name__=='__main__':unittest.main()
