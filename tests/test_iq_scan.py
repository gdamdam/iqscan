import json,sys,unittest,tempfile
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from iqscan import iq_scan

class ScannerTests(unittest.TestCase):
    def test_long_event_window_note_is_not_presented_as_receiver_warning(self):
        event={'signal_analysis': {'status': 'unknown', 'warnings': ['long event analyzed in bounded windows']}}
        self.assertIn('note: long event sampled', iq_scan.signal_analysis_brief(event))
        self.assertNotIn('warning:', iq_scan.signal_analysis_brief(event))

    def test_cs8_cs16_detect_and_exact_clips(self):
        fs=32000;n=fs*3;t=np.arange(n)/fs
        rng=np.random.default_rng(4)
        z=.02*(rng.normal(size=n)+1j*rng.normal(size=n))
        z+=.12*np.exp(2j*np.pi*7000*t)*((t>=1)&(t<1.6))
        z+=.06*np.exp(-2j*np.pi*8000*t)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for fmt,scale,dtype in [('cs8',128,'i1'),('cs16',32768,'<i2')]:
                path=root/f'test_32000SPS_137900000Hz.{fmt}'
                samples=np.column_stack((z.real,z.imag));np.clip(np.round(samples*scale),-scale,scale-1).astype(dtype).tofile(path)
                args=iq_scan.parser().parse_args([str(path),'--fft-size','1024','--time-bin','.032','--top','20','--clips','1'])
                meta=iq_scan.metadata(args);f,norm,ref,dt=iq_scan.spectrum(meta,args);events=iq_scan.detect(meta,args,f,norm,dt)
                burst=[e for e in events if e['kind']=='transient' and abs(e['center_offset_hz']-7000)<100]
                self.assertTrue(burst,events);self.assertAlmostEqual(burst[0]['start_s'],1,delta=.08);self.assertAlmostEqual(burst[0]['end_s'],1.6,delta=.08)
                self.assertTrue(any(e['kind']=='persistent' and abs(e['center_offset_hz']+8000)<100 for e in events))
                out=root/fmt;out.mkdir();iq_scan.clips(meta,args,burst,out)
                e=burst[0];raw=path.read_bytes();first=round(e['clip_start_original_s']*fs)*meta['bytes_per_complex'];clip=(out/e['clip']).read_bytes()
                self.assertEqual(clip,raw[first:first+len(clip)])

    def test_metadata_requires_rate_and_complete_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'unknown.cs8';p.write_bytes(bytes(8192))
            with self.assertRaisesRegex(ValueError,'Sample rate missing'):iq_scan.metadata(iq_scan.parser().parse_args([str(p)]))
            p.write_bytes(bytes(8193))
            with self.assertRaisesRegex(ValueError,'incomplete'):iq_scan.metadata(iq_scan.parser().parse_args([str(p),'--sample-rate','32000']))

    def test_final_partial_time_bin_uses_actual_recording_end(self):
        args=iq_scan.parser().parse_args(['--sample-rate','32000','--threshold','1','--min-duration','0','--dc-exclude','0'])
        meta={'sample_rate':32000,'duration_s':1.0,'frequency_bin_hz':1000,'center_frequency_hz':None}
        f=np.array([-8000.,-4000.,4000.,8000.]);norm=np.array([[0.,0.,0.,0.],[10.,10.,10.,10.]])
        events=iq_scan.detect(meta,args,f,norm,.64)
        self.assertEqual(len(events),1)
        self.assertEqual((events[0]['start_s'],events[0]['end_s'],events[0]['peak_time_s']),(.64,1.0,.82))

    def test_output_directory_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'test_32000SPS.cs8';p.write_bytes(bytes(8192))
            self.assertEqual(iq_scan.main([str(p),'--output',tmp]),2)

    def test_spectrum_cache_roundtrip_and_redetect(self):
        fs=32000;n=fs*3;t=np.arange(n)/fs
        rng=np.random.default_rng(4)
        z=.02*(rng.normal(size=n)+1j*rng.normal(size=n))
        z+=.12*np.exp(2j*np.pi*7000*t)*((t>=1)&(t<1.6))
        shape=['--fft-size','1024','--time-bin','.032']
        quiet=['--bandplan','none','--known-signals','none','--clips','0']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'test_32000SPS_137900000Hz.cs8'
            np.clip(np.round(np.column_stack((z.real,z.imag))*128),-128,127).astype('i1').tofile(path)
            base=root/'first'
            self.assertEqual(iq_scan.main([str(path),'--output',str(base),*shape,*quiet,'--save-spectrum']),0)
            self.assertTrue((base/'spectrum.npz').exists())
            # The cached matrix reproduces a freshly computed one, so redetect is faithful.
            meta,f,norm,ref,dt,spectrum_args=iq_scan.load_spectrum(base)
            self.assertEqual(spectrum_args,{'fft_size':1024,'time_bin':.032,'max_rows':2000,'reference_band':[.22,.4]})
            args=iq_scan.parser().parse_args([str(path),*shape])
            fresh=iq_scan.spectrum(iq_scan.metadata(args),args)[1]
            np.testing.assert_allclose(norm,fresh,rtol=0,atol=1e-4)
            # A stricter threshold reshapes regions, so redetect must differ from the source scan.
            strict=root/'strict'
            self.assertEqual(iq_scan.main(['--redetect',str(base),'--output',str(strict),*quiet,'--threshold','25']),0)
            self.assertEqual(iq_scan.main(['--redetect',str(base),'--output',str(root/'bad-top'),*quiet,'--top','0']),2)
            self.assertEqual(iq_scan.main(['--redetect',str(base),'--output',str(root/'bad-dc'),*quiet,'--dc-exclude','nan']),2)
            first=json.loads((base/'events.json').read_text())['events']
            after=json.loads((strict/'events.json').read_text())['events']
            self.assertTrue(first and after)
            # Threshold reshapes the region itself, so the same burst comes back narrower.
            self.assertLess(after[0]['bandwidth_hz'],first[0]['bandwidth_hz'])
            self.assertEqual(json.loads((strict/'events.json').read_text())['metadata']['redetected_from'],str(base.resolve()))
            # FFT shaping comes from the cache even when the command line disagrees.
            ignored=root/'ignored'
            self.assertEqual(iq_scan.main(['--redetect',str(base),'--output',str(ignored),*quiet,'--fft-size','256']),0)
            self.assertEqual(json.loads((ignored/'events.json').read_text())['metadata']['frequency_bin_hz'],meta['frequency_bin_hz'])
            # A directory without a cached spectrum says what to do about it.
            with self.assertRaisesRegex(ValueError,'--save-spectrum'):iq_scan.load_spectrum(strict)

    def test_redetect_without_the_recording_skips_clips(self):
        fs=32000;n=fs*2;t=np.arange(n)/fs
        z=.02*np.random.default_rng(1).normal(size=n)+.1*np.exp(2j*np.pi*5000*t)*((t>=.5)&(t<1.2))
        quiet=['--bandplan','none','--known-signals','none']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'gone_32000SPS_137900000Hz.cs8'
            np.clip(np.round(np.column_stack((z.real,z.imag))*128),-128,127).astype('i1').tofile(path)
            base=root/'first'
            self.assertEqual(iq_scan.main([str(path),'--output',str(base),'--fft-size','1024','--time-bin','.032','--clips','0',*quiet,'--save-spectrum']),0)
            path.unlink()
            again=root/'again'
            self.assertEqual(iq_scan.main(['--redetect',str(base),'--output',str(again),*quiet,'--clips','5']),0)
            self.assertFalse((again/'clips').exists())
            self.assertTrue((again/'report.html').exists())

if __name__=='__main__':unittest.main()
