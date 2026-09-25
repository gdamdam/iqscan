import contextlib
import csv
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from iqscan import iq_scan


class ReleaseTests(unittest.TestCase):
    def fixture(self, root):
        fs=32000
        t=np.arange(fs*3)/fs
        z=.008*(np.random.default_rng(32).normal(size=len(t))+1j*np.random.default_rng(33).normal(size=len(t)))
        z += .3*np.exp(2j*np.pi*7000*t)*((t>=1)&(t<1.6))
        path=root/'source_32000SPS.cs16'
        np.round(np.column_stack((z.real,z.imag))*32767).astype('<i2').tofile(path)
        return path

    def run_scan(self, arguments):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return iq_scan.main(arguments+['--bandplan','none','--known-signals','none'])

    def test_changed_source_and_relocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.fixture(root);cache=root/'cache'
            with patch('iqscan.iq_scan.report'):
                self.assertEqual(self.run_scan([str(path),'--output',str(cache),'--save-spectrum','--clips','0','--fft-size','1024','--time-bin','.032']),0)
            relocated=root/'moved.cs16';shutil.copyfile(path,relocated)
            path.write_bytes(bytes(path.stat().st_size))
            self.assertEqual(self.run_scan(['--redetect',str(cache),'--source',str(path),'--output',str(root/'wrong-source')]),2)
            with patch('iqscan.iq_scan.report') as report:
                self.assertEqual(self.run_scan(['--redetect',str(cache),'--output',str(root/'changed'),'--analyze-signals']),0)
                self.assertFalse((root/'changed'/'clips').exists())
                self.assertEqual(report.call_args.args[2][0]['signal_analysis']['status'],'unknown')
            with patch('iqscan.iq_scan.report'):
                self.assertEqual(self.run_scan(['--redetect',str(cache),'--source',str(relocated),'--output',str(root/'relocated')]),0)
                self.assertTrue(any((root/'relocated'/'clips').iterdir()))

    def test_region_and_float_unsigned_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for fmt,dtype in [('cf32_le','<f4'),('cf32_be','>f4'),('cu8','u1')]:
                fs=32000;t=np.arange(fs*2)/fs;z=.2*np.exp(2j*np.pi*7000*t)
                pairs=np.column_stack((z.real,z.imag))
                if fmt=='cu8':pairs=np.round(pairs*128+128)
                path=root/f'input_32000SPS.{fmt}';pairs.astype(dtype).tofile(path)
                args=iq_scan.parser().parse_args([str(path),'--start','.5','--duration','.5','--fft-size','1024','--min-offset','6000','--max-offset','8000'])
                meta=iq_scan.metadata(args)
                self.assertEqual(meta['samples'],16000)
                with contextlib.redirect_stderr(io.StringIO()):f,norm,ref,dt=iq_scan.spectrum(meta,args)
                events=iq_scan.detect(meta,args,f,norm,dt)
                self.assertTrue(events)
                self.assertTrue(all(6000 <= e['center_offset_hz'] <= 8000 for e in events))
                out=root/f'clips-{fmt}';out.mkdir();iq_scan.clips(meta,args,events,out)
                event=events[0]
                original=path.read_bytes();first=round(event['clip_start_original_s']*fs)*meta['bytes_per_complex']
                exported=(out/event['clip']).read_bytes()
                self.assertEqual(exported,original[first:first+len(exported)])

    def test_portable_report_and_channel_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.fixture(root);out=root/'portable'
            self.assertEqual(self.run_scan([str(path),'--output',str(out),'--portable','--channel-clips','1','--fft-size','1024','--time-bin','.032']),0)
            doc=json.loads((out/'events.json').read_text())
            self.assertTrue(doc['metadata']['portable_report'])
            for name in ('events.json','events.csv','OPEN-CLIPS.txt','report.html'):
                self.assertNotIn(str(root), (out/name).read_text())
            channel=doc['events'][0]['channel_clip'];self.assertTrue((out/channel).exists())
            sidecar=json.loads((out/doc['events'][0]['channel_sigmf']).read_text())
            self.assertEqual(sidecar['annotations'][0]['core:sample_count'],doc['events'][0]['channel_metadata']['samples'])
            exact=json.loads((out/doc['events'][0]['clip_metadata']).read_text())
            self.assertIn('candidate',exact['annotations'][0]['core:label'])
            with (out/'events.csv').open() as file:
                rows=list(csv.DictReader(file))
            self.assertEqual([float(e['start_s']) for e in rows],sorted(float(e['start_s']) for e in rows))

    def test_channel_export_cap_keeps_the_event(self):
        from argparse import Namespace
        from iqscan.iq_export import export_channels
        # Wide channel: no decimation, so the 262144-sample cap is ~0.52 s of source.
        fs=500_000
        meta=dict(sample_rate=fs,duration_s=3.0,samples=3*fs,center_frequency_hz=None)
        event=dict(id=1,start_s=1.0,end_s=1.5,peak_time_s=1.25,center_offset_hz=10000.0,bandwidth_hz=400000.0)
        args=Namespace(channel_clips=1,padding=1.0,max_clip_seconds=30.0)
        calls=[]
        def channelize(meta,first,count,center,bw,max_output_samples):
            calls.append((first,count))
            return np.zeros(4,complex),float(fs),[]
        with tempfile.TemporaryDirectory() as tmp, patch('iqscan.signal_analysis._stream_channelize',channelize), \
                patch('iqscan.iq_input.write_sigmf',return_value=Path(tmp)/'x.sigmf-meta'):
            export_channels(meta,args,[event],Path(tmp))
        first,count=calls[0]
        self.assertLessEqual(count,262144)
        self.assertLessEqual(first/fs,event['start_s'])
        self.assertGreaterEqual((first+count)/fs,event['end_s'])

    def test_baseband_zero_frequency_and_portable_nested_warnings(self):
        from iqscan.iq_export import portable_report
        args=iq_scan.parser().parse_args(['--reference-cache','/private/test-cache'])
        meta,args,events=portable_report(dict(input='/private/raw.cs8',spectrum_context={'warnings':['Cache failed: /private/test-cache/catalog.json']}),args,[])
        self.assertNotIn('/private/',json.dumps(meta))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.fixture(root);out=root/'baseband'
            self.assertEqual(self.run_scan([str(path),'--center-frequency','0','--output',str(out),'--fft-size','1024','--time-bin','.032']),0)
            document=json.loads((out/'events.json').read_text())
            self.assertEqual(document['metadata']['center_frequency_hz'],0)
            self.assertIn('Center: 0.0 Hz', (out/'report.html').read_text())
            event=document['events'][0]
            self.assertIn(f"{event['frequency_hz']/1e6:.6f} MHz", (out/'report.html').read_text())

    def test_integer_rail_warning_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'rail_32000SPS.cs8';path.write_bytes(bytes([127])*8192)
            out=root/'scan'
            self.assertEqual(self.run_scan([str(path),'--output',str(out),'--clips','0']),0)
            self.assertIn('Clipping warning', (out/'report.html').read_text())
