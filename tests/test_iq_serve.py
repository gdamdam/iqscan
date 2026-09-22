import json,sys,tempfile,threading,unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import iq_scan, iq_serve

QUIET=['--bandplan','none','--known-signals','none','--clips','0']

def recording(root,fs=32000,secs=3):
    t=np.arange(fs*secs)/fs
    rng=np.random.default_rng(4)
    z=.02*(rng.normal(size=fs*secs)+1j*rng.normal(size=fs*secs))
    z+=.12*np.exp(2j*np.pi*7000*t)*((t>=1)&(t<1.6))
    path=root/f'test_{fs}SPS_137900000Hz.cs8'
    np.clip(np.round(np.column_stack((z.real,z.imag))*128),-128,127).astype('i1').tofile(path)
    return path

class ServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.scans=root/'scans';self.scans.mkdir()
        path=recording(root)
        self.scan=self.scans/'run-one'
        assert iq_scan.main([str(path),'--output',str(self.scan),'--fft-size','1024','--time-bin','.032',*QUIET,'--save-spectrum'])==0
        (self.scans/'no-cache').mkdir()
        self.state=iq_serve.Spectra(self.scans)
        defaults={k:getattr(iq_scan.parser().parse_args([]),k) for k in iq_serve.DETECT_ARGS}
        self.server=ThreadingHTTPServer(('127.0.0.1',0),iq_serve.handler_for(self.state,defaults))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()

    def get(self,path):
        with urlopen(self.base+path,timeout=10) as r: return r.status,r.read()

    def test_lists_only_directories_holding_a_cached_spectrum(self):
        listed=self.state.available()
        self.assertEqual([s['id'] for s in listed],['run-one'])
        self.assertEqual(listed[0]['shaping'],{'fft_size':1024,'time_bin':.032,'max_rows':2000})
        status,body=self.get('/api/scans')
        self.assertEqual(status,200)
        self.assertEqual([s['id'] for s in json.loads(body)['scans']],['run-one'])

    def test_detect_matches_the_library_and_tracks_threshold(self):
        meta,f,norm,_ref,dt,_shaping=self.state.get('run-one')
        args=iq_serve.detect_params({'threshold':['9']},{k:getattr(iq_scan.parser().parse_args([]),k) for k in iq_serve.DETECT_ARGS})
        expected=iq_scan.detect(dict(meta),args,f,norm,dt)
        payload=json.loads(self.get('/api/detect?scan=run-one&threshold=9')[1])
        self.assertEqual(payload['events'],expected)
        self.assertIn('--redetect scans/run-one',payload['command'])
        self.assertIn('--threshold 9',payload['command'])
        # Threshold reshapes regions, so the same burst comes back narrower.
        loose=json.loads(self.get('/api/detect?scan=run-one&threshold=7')[1])['events']
        strict=json.loads(self.get('/api/detect?scan=run-one&threshold=25')[1])['events']
        self.assertTrue(loose and strict)
        self.assertLess(strict[0]['bandwidth_hz'],loose[0]['bandwidth_hz'])

    def test_image_is_a_png_sized_to_the_matrix(self):
        status,body=self.get('/api/image?scan=run-one')
        self.assertEqual(status,200)
        self.assertEqual(body[:8],b'\x89PNG\r\n\x1a\n')

    def test_rejects_traversal_and_unknown_scans(self):
        for bad in ('..','../../etc','','no-cache'):
            with self.assertRaises(ValueError): self.state.resolve(bad)
        with self.assertRaises(HTTPError) as caught: self.get('/api/detect?scan=../secret')
        self.assertEqual(caught.exception.code,400)
        self.assertIn('Invalid scan id',json.loads(caught.exception.read())['error'])

    def test_rejects_nonsense_parameters(self):
        for query in ('threshold=0','threshold=abc','top=0','min_duration=-1'):
            with self.assertRaises(HTTPError) as caught: self.get('/api/detect?scan=run-one&'+query)
            self.assertEqual(caught.exception.code,400)

    def test_serve_refuses_a_root_without_caches(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(ValueError,'--save-spectrum'): iq_serve.serve(empty,0,open_browser=False)
            with self.assertRaisesRegex(ValueError,'No scan directory'): iq_serve.serve(Path(empty)/'missing',0,open_browser=False)

if __name__=='__main__':unittest.main()
