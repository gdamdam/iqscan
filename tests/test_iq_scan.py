import sys,unittest,tempfile
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import iq_scan

class ScannerTests(unittest.TestCase):
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

    def test_output_directory_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'test_32000SPS.cs8';p.write_bytes(bytes(8192))
            self.assertEqual(iq_scan.main([str(p),'--output',tmp]),2)

if __name__=='__main__':unittest.main()
