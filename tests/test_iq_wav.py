import struct,tempfile,unittest,warnings,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from iqscan import iq_scan
from iqscan.iq_wav import read_header

class WavTests(unittest.TestCase):
 def test_riff_rf64_payload_and_clips(self):
  payload=bytes(range(256))*128
  fmt=b'fmt '+struct.pack('<IHHIIHH',16,1,2,32000,128000,4,16)
  for rf64 in (False,True):
   with tempfile.TemporaryDirectory() as d:
    p=Path(d)/'SDRconnect_IQ_20260921_160100_455000000HZ.wav'
    ds=b'ds64'+struct.pack('<IQQQI',28,0,len(payload),len(payload)//4,0) if rf64 else b''
    body=b'WAVE'+ds+fmt+b'data'+struct.pack('<I',0xffffffff if rf64 else len(payload))+payload+b'JUNK'+struct.pack('<I',4)+b'abcd'
    p.write_bytes((b'RF64' if rf64 else b'RIFF')+struct.pack('<I',0xffffffff if rf64 else len(body))+body)
    args=iq_scan.parser().parse_args([str(p),'--padding','0']);m=iq_scan.metadata(args)
    self.assertEqual(m['bytes'],len(payload));self.assertEqual(m['sample_rate'],32000);self.assertEqual(m['center_frequency_hz'],455000000)
    out=Path(d)/'out';out.mkdir();ev=[dict(id=1,start_s=0,end_s=.01,peak_time_s=.005)]
    iq_scan.clips(m,args,ev,out);self.assertEqual((out/ev[0]['clip']).read_bytes(),payload[:1280])
 def test_short_data_warns_and_mono_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'test.wav'
   fmt=b'fmt '+struct.pack('<IHHIIHH',16,1,2,32000,128000,4,16)
   p.write_bytes(b'RIFF'+struct.pack('<I',52)+b'WAVE'+fmt+b'data'+struct.pack('<I',16)+bytes(12))
   with warnings.catch_warnings(record=True) as w:
    m=read_header(p);self.assertEqual(m['bytes'],12);self.assertTrue(w)
   raw=bytearray(p.read_bytes());struct.pack_into('<H',raw,22,1);p.write_bytes(raw)
   with self.assertRaisesRegex(ValueError,'two-channel'):read_header(p)
