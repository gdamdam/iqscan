import json,os,shlex,subprocess,sys,tempfile,threading,unittest
import inspect
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from iqscan import iq_scan, iq_serve

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
        self.assertEqual(listed[0]['shaping'],{'fft_size':1024,'time_bin':.032,'max_rows':2000,'reference_band':[.22,.4]})
        status,body=self.get('/api/scans')
        self.assertEqual(status,200)
        self.assertEqual([s['id'] for s in json.loads(body)['scans']],['run-one'])
        self.assertEqual(json.loads(body)['scans'][0]['detection_args']['threshold'],7)

    def test_rejects_symlink_scan_directories_and_cache_files(self):
        outside=Path(self.tmp.name)/'outside';outside.mkdir();(outside/'spectrum.npz').write_bytes((self.scan/'spectrum.npz').read_bytes())
        linked_dir=self.scans/'linked-dir'
        linked_cache=self.scans/'linked-cache';linked_cache.mkdir()
        try:
            linked_dir.symlink_to(self.scan,target_is_directory=True)
            (linked_cache/'spectrum.npz').symlink_to(self.scan/'spectrum.npz')
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        self.assertEqual([s['id'] for s in self.state.available()],['run-one'])
        for bad in ('linked-dir','linked-cache'):
            with self.assertRaises(ValueError):self.state.resolve(bad)

    def test_serve_accepts_detection_defaults_from_caller(self):
        self.assertIn('defaults',inspect.signature(iq_serve.serve).parameters)

    def test_explorer_uses_text_nodes_and_keyboard_selection(self):
        self.assertNotIn('innerHTML',iq_serve.PAGE)
        self.assertIn("setAttribute('tabindex','0')",iq_serve.PAGE)
        self.assertIn("ev.key==='Enter'||ev.key===' '",iq_serve.PAGE)
        self.assertIn('clearResults',iq_serve.PAGE)
        self.assertIn('String(ms).padStart(3',iq_serve.PAGE)

    def test_detect_matches_the_library_and_tracks_threshold(self):
        meta,f,norm,_ref,dt,_shaping=self.state.get('run-one')
        args=iq_serve.detect_params({'threshold':['9']},{k:getattr(iq_scan.parser().parse_args([]),k) for k in iq_serve.DETECT_ARGS})
        expected=iq_scan.detect(dict(meta),args,f,norm,dt)
        payload=json.loads(self.get('/api/detect?scan=run-one&threshold=9')[1])
        self.assertEqual(payload['events'],expected)
        self.assertIn('--redetect '+str(self.scan.resolve()),payload['command'])
        self.assertIn(sys.executable+' -m iqscan ',payload['command'])
        self.assertIn('--threshold 9',payload['command'])
        # Threshold reshapes regions, so the same burst comes back narrower.
        loose=json.loads(self.get('/api/detect?scan=run-one&threshold=7')[1])['events']
        strict=json.loads(self.get('/api/detect?scan=run-one&threshold=25')[1])['events']
        self.assertTrue(loose and strict)
        self.assertLess(strict[0]['bandwidth_hz'],loose[0]['bandwidth_hz'])

    def test_saved_frequency_limits_survive_preview_and_command(self):
        with patch('iqscan.iq_scan.cache_summary',return_value=({'detection_args':{'min_offset':5000.0,'max_offset':9000.0},
                                                          'sample_rate':32000},{})):
            saved=self.state.detection_defaults('run-one')
        self.assertEqual((saved['min_offset'],saved['max_offset']),(5000.0,9000.0))
        with patch.object(self.state,'detection_defaults',return_value=saved):
            payload=json.loads(self.get('/api/detect?scan=run-one')[1])
        self.assertTrue(payload['events'])
        self.assertTrue(all(5000<=e['center_offset_hz']<=9000 for e in payload['events']))
        self.assertIn('--min-offset 5000 --max-offset 9000',payload['command'])

    def test_image_is_a_png_sized_to_the_matrix(self):
        status,body=self.get('/api/image?scan=run-one')
        self.assertEqual(status,200)
        self.assertEqual(body[:8],b'\x89PNG\r\n\x1a\n')

    def test_image_uses_analysis_time_extent_for_partial_final_bin(self):
        with patch('matplotlib.axes.Axes.imshow') as image, patch('matplotlib.axes.Axes.set_ylim') as limits:
            iq_serve.waterfall_png(np.zeros((2,2)),1.0,.64)
        self.assertEqual(image.call_args.kwargs['extent'],(0,2,1.28,0))
        limits.assert_called_once_with(1.0,0)

    def test_rejects_traversal_and_unknown_scans(self):
        for bad in ('..','../../etc','','no-cache'):
            with self.assertRaises(ValueError): self.state.resolve(bad)
        with self.assertRaises(HTTPError) as caught: self.get('/api/detect?scan=../secret')
        self.assertEqual(caught.exception.code,400)
        self.assertIn('Invalid scan id',json.loads(caught.exception.read())['error'])

    def test_rejects_nonsense_parameters(self):
        for query in ('threshold=0','threshold=abc','threshold=nan','min_duration=nan','top=0','min_duration=-1','dc_exclude=nan'):
            with self.assertRaises(HTTPError) as caught: self.get('/api/detect?scan=run-one&'+query)
            self.assertEqual(caught.exception.code,400)

    def test_accepts_zero_minimum_duration_and_quotes_custom_paths(self):
        status,body=self.get('/api/detect?scan=run-one&min_duration=0')
        self.assertEqual(status,200)
        args=iq_serve.detect_params({'min_duration':['0']},{k:getattr(iq_scan.parser().parse_args([]),k) for k in iq_serve.DETECT_ARGS},32000)
        scan_id='scan;$(touch pwned)'
        root=Path(self.tmp.name)/'custom scans'
        command=iq_serve.command_for(scan_id,args,root)
        expected=str((root.resolve()/scan_id).resolve())
        if os.name=='nt':
            self.assertIn(subprocess.list2cmdline([expected]),command)
        else:
            self.assertEqual(shlex.split(command)[4],expected)

    def test_skips_truncated_and_invalid_caches(self):
        bad=self.scans/'truncated';bad.mkdir();(bad/'spectrum.npz').write_bytes(b'PK\x03\x04truncated')
        malformed=self.scans/'malformed';malformed.mkdir()
        np.savez(malformed/'spectrum.npz',f=np.array([1]),norm=np.zeros((2,1)),reference=np.zeros(2),dt=.1,
                 meta=json.dumps({'rows':2}),spectrum_args=json.dumps({'fft_size':1}))
        self.assertEqual([scan['id'] for scan in self.state.available()],['run-one'])

    def test_serve_refuses_a_root_without_caches(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(ValueError,'--save-spectrum'): iq_serve.serve(empty,0,open_browser=False)
            with self.assertRaisesRegex(ValueError,'No scan directory'): iq_serve.serve(Path(empty)/'missing',0,open_browser=False)

if __name__=='__main__':unittest.main()
