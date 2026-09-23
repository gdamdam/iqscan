#!/usr/bin/env python3
"""Local explorer for cached spectra: re-run detection live over spectrum.npz.

Exploration only. The CLI still produces the shareable report, and the app hands
you the exact --redetect command for any parameter set you settle on. Binds to
loopback; there is no authentication and none is intended.
"""
import io
import json
import shlex
import sys
import threading
import webbrowser
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import iq_scan

DETECT_ARGS = {'threshold': float, 'min_duration': float, 'dc_exclude': float, 'top': int}
MAX_IMAGE = (1600, 900)


class Spectra:
    """Keeps decoded matrices in memory so a slider drag costs milliseconds."""

    def __init__(self, root, keep=2):
        self.root, self.keep = root, keep
        self.lock = threading.RLock()   # image() renders while holding it and calls get()
        self.loaded, self.images = {}, {}

    def available(self):
        found = []
        for directory in sorted((p for p in self.root.iterdir()
                                 if p.is_dir() and not p.is_symlink()), reverse=True):
            path = directory/'spectrum.npz'
            if path.is_symlink() or not path.is_file():
                continue
            try:
                meta, shaping = iq_scan.cache_summary(directory)
            except (OSError, ValueError, KeyError, TypeError, EOFError):
                continue                                             # a half-written cache is skipped, not fatal
            found.append(dict(id=directory.name, name=Path(meta['input']).name, duration_s=meta['duration_s'],
                sample_rate=meta['sample_rate'], center_frequency_hz=meta['center_frequency_hz'],
                rows=meta.get('rows'), frequency_bin_hz=meta['frequency_bin_hz'], shaping=shaping,
                detection_args=self.detection_defaults(directory.name)))
        return found

    def resolve(self, scan_id):
        # Only a direct child of the scan root, by name: no traversal, no absolute paths.
        if not scan_id or '/' in scan_id or '\\' in scan_id or scan_id in ('.', '..'):
            raise ValueError('Invalid scan id')
        directory = self.root/scan_id
        cache = directory/'spectrum.npz'
        if (directory.is_symlink() or not directory.is_dir() or cache.is_symlink()
                or not cache.is_file()):
            raise ValueError(f'No cached spectrum for {scan_id}')
        return directory

    def detection_defaults(self, scan_id):
        """Return saved detection settings from cache metadata or its report."""
        try:
            directory = self.resolve(scan_id)
            meta, _ = iq_scan.cache_summary(directory)
            settings = meta.get('detection_args', {})
            if not settings:
                report = directory/'events.json'
                if report.is_symlink() or not report.is_file():
                    return {}
                payload = json.loads(report.read_text(encoding='utf-8'))
                settings = (payload.get('settings') or payload.get('detection_args')
                            or payload.get('metadata', {}).get('detection_args', {}))
            values = {key: DETECT_ARGS[key](settings[key]) for key in DETECT_ARGS if key in settings}
            if not values: return {}
            base = {key: getattr(iq_scan.parser().parse_args([]), key) for key in DETECT_ARGS}
            base.update(values)
            iq_scan.validate_detection_args(Namespace(**base), meta['sample_rate'])
            return values
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def get(self, scan_id):
        with self.lock:
            if scan_id not in self.loaded:
                if len(self.loaded) >= self.keep:
                    self.loaded.pop(next(iter(self.loaded)))
                self.loaded[scan_id] = iq_scan.load_spectrum(self.resolve(scan_id))
            return self.loaded[scan_id]

    def image(self, scan_id):
        with self.lock:
            if scan_id not in self.images:
                meta, _f, norm, _reference, dt, _shaping = self.get(scan_id)
                self.images[scan_id] = waterfall_png(norm, meta['duration_s'], dt)
                if len(self.images) > self.keep:
                    self.images.pop(next(iter(self.images)))
            return self.images[scan_id]


def waterfall_png(norm, duration_s=None, dt=None):
    """Bare data pixels, no axes or padding, so the browser can map events linearly."""
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    rows, cols = norm.shape
    width, height = min(cols, MAX_IMAGE[0]), min(rows, MAX_IMAGE[1])
    figure = plt.figure(figsize=(width/100, height/100), dpi=100)
    axes = figure.add_axes([0, 0, 1, 1])
    axes.set_axis_off()
    extent=(0,cols,rows*dt if dt is not None else rows,0)
    axes.imshow(norm, extent=extent, aspect='auto', cmap='magma', vmin=-3, vmax=max(12, float(np.percentile(norm, 99.5))))
    if duration_s is not None: axes.set_ylim(duration_s,0)
    buffer = io.BytesIO()
    figure.savefig(buffer, format='png', dpi=100)
    plt.close(figure)
    return buffer.getvalue()


def detect_params(query, defaults, sample_rate=None):
    args = Namespace(**defaults)
    for key, cast in DETECT_ARGS.items():
        if key in query:
            try:
                value = cast(query[key][0])
            except ValueError:
                raise ValueError(f'{key} must be {cast.__name__}')
            setattr(args, key, value)
    iq_scan.validate_detection_args(args, sample_rate)
    return args


def command_for(scan_id, args, root):
    scan_path=(Path(root).expanduser().resolve()/scan_id).resolve()
    module=Path(iq_scan.__file__).resolve()
    parts = [sys.executable, str(module), '--redetect', str(scan_path)]
    for key in DETECT_ARGS:
        parts.extend((f"--{key.replace('_','-')}", f"{getattr(args, key):g}"))
    return shlex.join(parts)


def handler_for(state, defaults, overrides=None):
    overrides = dict(overrides or {})
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass                                                     # the terminal stays readable

        def send(self, code, body, content_type):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload, code=200):
            self.send(code, json.dumps(payload).encode(), 'application/json')

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == '/':
                    return self.send(200, PAGE.encode(), 'text/html; charset=utf-8')
                if url.path == '/api/scans':
                    return self.send_json(dict(scans=state.available(), defaults=defaults, overrides=overrides))
                scan_id = query.get('scan', [''])[0]
                if url.path == '/api/image':
                    return self.send(200, state.image(scan_id), 'image/png')
                if url.path == '/api/detect':
                    meta, f, norm, _reference, dt, shaping = state.get(scan_id)
                    base_defaults = dict(defaults)
                    base_defaults.update(state.detection_defaults(scan_id))
                    base_defaults.update(overrides)
                    args = detect_params(query, base_defaults, meta['sample_rate'])
                    events = iq_scan.detect(dict(meta), args, f, norm, dt)
                    return self.send_json(dict(events=events, shaping=shaping,
                        command=command_for(scan_id, args, state.root),
                        extent=dict(low_offset_hz=float(f[0]), high_offset_hz=float(f[-1]),
                            duration_s=meta['duration_s'], center_frequency_hz=meta['center_frequency_hz'],
                            sample_rate=meta['sample_rate'])))
                return self.send_json(dict(error='Not found'), 404)
            except ValueError as exc:
                return self.send_json(dict(error=str(exc)), 400)
            except Exception as exc:                                 # a bad request must not kill the server
                return self.send_json(dict(error=f'{type(exc).__name__}: {exc}'), 500)
    return Handler


def serve(root, port=8731, open_browser=True, defaults=None, overrides=None):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f'No scan directory: {root}')
    state = Spectra(root)
    ready = state.available()
    if not ready:
        raise ValueError(f'No spectrum.npz under {root}. Run a scan with --save-spectrum first.')
    parser_defaults = iq_scan.parser().parse_args([])
    selected_defaults = {key: getattr(parser_defaults, key) for key in DETECT_ARGS}
    if defaults is not None:
        unknown = set(defaults) - set(DETECT_ARGS)
        if unknown:
            raise ValueError('Unknown detection defaults: '+', '.join(sorted(unknown)))
        selected_defaults.update(defaults)
    if overrides is not None:
        unknown = set(overrides) - set(DETECT_ARGS)
        if unknown:
            raise ValueError('Unknown detection overrides: '+', '.join(sorted(unknown)))
    server = ThreadingHTTPServer(('127.0.0.1', port), handler_for(state, selected_defaults, overrides))
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'Explorer on {url} — {len(ready)} cached scan(s) under {root}')
    print('Exploration only; use the printed --redetect command to produce a real report. Ctrl-C to stop.')
    if open_browser:
        threading.Timer(.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        server.server_close()
    return 0


PAGE = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>IQ scan explorer</title><style>
:root{--bg:#10151e;--fg:#e5edf8;--dim:#8fa3bd;--line:#354050;--accent:#79dfff;--panel:#161d29}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:15px system-ui;margin:0;padding:20px}
h1{font-size:19px;margin:0 0 4px}p.sub{color:var(--dim);margin:0 0 18px;font-size:13px}
.wrap{display:grid;grid-template-columns:270px minmax(0,1fr);gap:20px;align-items:start}
@media(max-width:820px){.wrap{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
label{display:block;font-size:12px;color:var(--dim);margin:12px 0 4px;text-transform:uppercase;letter-spacing:.05em}
input[type=range]{width:100%;accent-color:var(--accent)}select,input[type=number]{width:100%;background:#0c111a;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px}
.val{float:right;color:var(--accent);font-variant-numeric:tabular-nums}
.stage{position:relative;line-height:0;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.stage img{width:100%;display:block}
.stage svg{position:absolute;inset:0;width:100%;height:100%}
rect.ev{fill:rgba(121,223,255,.16);stroke:var(--accent);stroke-width:.35;vector-effect:non-scaling-stroke;cursor:pointer}
rect.ev:focus{stroke:#fff;stroke-width:.8;outline:none}
rect.ev.sel{fill:rgba(255,215,100,.28);stroke:#ffd764}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:14px}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:600}tr.sel td{background:rgba(255,215,100,.1)}
tbody tr{cursor:pointer}tbody tr:focus td{outline:1px solid var(--accent);outline-offset:-1px}code{display:block;background:#0c111a;border:1px solid var(--line);border-radius:6px;padding:9px;margin-top:12px;font-size:12px;overflow-wrap:anywhere;color:var(--accent)}
.axis{display:flex;justify-content:space-between;color:var(--dim);font-size:11px;margin-top:4px}
.note{color:var(--dim);font-size:12px;margin-top:14px;line-height:1.5}
.err{color:#ff9d9d}.busy{opacity:.55}
</style>
<h1>IQ scan explorer</h1>
<p class="sub">Live re-detection over a cached spectrum. Frequency references and contrast are hints, not identification. Raw-IQ modulation and protocol analysis is unavailable in this cached view; use the printed command with <code>--analyze-signals</code> while the recording is present.</p>
<div class="wrap">
<div class="panel">
  <label>Scan</label><select id="scan"></select>
<label>Threshold <span class="val" id="vthreshold"></span></label><input type="range" id="threshold" min="1" max="30" step="0.5">
  <label>Min duration (s) <span class="val" id="vmin_duration"></span></label><input type="range" id="min_duration" min="0" max="2" step="0.01">
  <label>DC exclude (Hz) <span class="val" id="vdc_exclude"></span></label><input type="range" id="dc_exclude" min="0" max="20000" step="250">
<label>Max events <span class="val" id="vtop"></span></label><input type="range" id="top" min="1" max="80" step="1">
  <p class="note" id="shaping"></p>
</div>
<div>
  <div class="stage"><img id="wf" alt="Cached spectrogram"><svg id="ov" viewBox="0 0 100 100" preserveAspectRatio="none"></svg></div>
  <div class="axis"><span id="flo"></span><span>time runs downward</span><span id="fhi"></span></div>
  <code id="cmd"></code>
  <div id="msg" class="note"></div>
  <table><thead><tr><th>ID</th><th>Type</th><th>Start</th><th>End</th><th>Offset kHz</th><th>Width Hz</th><th>Contrast</th></tr></thead><tbody id="rows"></tbody></table>
  <p class="note">Threshold is applied before regions are formed, so raising it reshapes and splits events rather than just hiding them &mdash; that is why these boxes change size. Run the command above to produce a real report with images and clips. The explorer does not infer modulation, symbol rate or protocol from the cached matrix.</p>
</div></div>
<script>
const $=id=>document.getElementById(id), KEYS=['threshold','min_duration','dc_exclude','top'];
let extent=null, sel=null, timer=null, seq=0;
const hms=s=>{let ms=Math.round(Number(s)*1000);const h=Math.floor(ms/3600000);ms-=h*3600000;const m=Math.floor(ms/60000);ms-=m*60000;const sec=Math.floor(ms/1000);ms-=sec*1000;return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}.${String(ms).padStart(3,'0')}`};
function show(){KEYS.forEach(k=>$('v'+k).textContent=$(k).value)}
function schedule(){show();clearTimeout(timer);timer=setTimeout(run,120)}
async function boot(){
  const r=await fetch('/api/scans'); const d=await r.json();
  window.scans=Object.fromEntries(d.scans.map(s=>[s.id,s]));window.serverOverrides=d.overrides||{};
  d.scans.forEach(s=>{const o=document.createElement('option');o.value=s.id;o.textContent=`${s.name} — ${hms(s.duration_s)}`;$('scan').append(o)});
  KEYS.forEach(k=>{$(k).value=d.defaults[k];$('v'+k).textContent=$(k).value;$(k).addEventListener('input',schedule)});
  $('scan').addEventListener('change',load); load();
}
function clearResults(message=''){
  extent=null;sel=null;$('ov').replaceChildren();$('rows').replaceChildren();$('cmd').textContent='';$('shaping').textContent='';
  $('flo').textContent='';$('fhi').textContent='';$('msg').className='note';$('msg').textContent=message;
}
function load(){
  ++seq;clearResults('Loading scan…');$('wf').removeAttribute('src');
  const scan=window.scans?.[$('scan').value];
  if(scan){const max=Math.max(0,Math.floor((scan.sample_rate*.45-.001)/250)*250);$('dc_exclude').max=max;
    const stored=scan.detection_args||{};KEYS.forEach(k=>{if(stored[k]!==undefined)$(k).value=stored[k];if(window.serverOverrides[k]!==undefined)$(k).value=window.serverOverrides[k]});
    if(Number($('dc_exclude').value)>max)$('dc_exclude').value=max;show()}
  $('wf').src='/api/image?scan='+encodeURIComponent($('scan').value);run()
}
async function run(){
  const mine=++seq, q=new URLSearchParams({scan:$('scan').value});
  KEYS.forEach(k=>q.set(k,$(k).value));
  document.body.classList.add('busy');
  let d;
  try{ d=await (await fetch('/api/detect?'+q)).json(); }
  catch(e){ if(mine!==seq)return; clearResults('Request failed: '+String(e)); $('msg').className='note err'; document.body.classList.remove('busy'); return; }
  if(mine!==seq) return;
  document.body.classList.remove('busy');
  if(d.error){ clearResults(String(d.error)); $('msg').className='note err'; return }
  $('msg').className='note';
  extent=d.extent; $('cmd').textContent=d.command; $('msg').textContent=d.events.length+' candidate(s).';
  $('shaping').textContent='Cached FFT shaping: '+Object.entries(d.shaping).map(([k,v])=>k+'='+v).join(', ')+'. Rescan to change these.';
  const c=extent.center_frequency_hz, hasCenter=c!==null&&c!==undefined, u=hasCenter?1e6:1e3;
  $('flo').textContent=((hasCenter?c:0)+extent.low_offset_hz)/u+(hasCenter?' MHz':' kHz');
  $('fhi').textContent=((hasCenter?c:0)+extent.high_offset_hz)/u+(hasCenter?' MHz':' kHz');
  draw(d.events); table(d.events);
}
function box(e){
  const span=extent.high_offset_hz-extent.low_offset_hz;
  return {x:100*(e.low_offset_hz-extent.low_offset_hz)/span, w:Math.max(.4,100*(e.high_offset_hz-e.low_offset_hz)/span),
          y:100*e.start_s/extent.duration_s, h:Math.max(.4,100*(e.end_s-e.start_s)/extent.duration_s)};
}
function draw(events){
  $('ov').replaceChildren(...events.map(e=>{const b=box(e),r=document.createElementNS('http://www.w3.org/2000/svg','rect'),t=document.createElementNS('http://www.w3.org/2000/svg','title');
    r.setAttribute('class','ev'+(sel===e.id?' sel':''));r.dataset.id=e.id;r.setAttribute('x',b.x);r.setAttribute('y',b.y);r.setAttribute('width',b.w);r.setAttribute('height',b.h);r.setAttribute('tabindex','0');r.setAttribute('role','button');r.setAttribute('aria-label',`Select event ${e.id}, ${e.kind}, ${e.contrast_db} dB`);t.textContent=`#${e.id} ${e.kind} ${e.contrast_db} dB`;r.append(t);
    r.onclick=()=>select(e.id,events);r.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();select(e.id,events)}};return r}));
}
function table(events){
  $('rows').replaceChildren(...events.map(e=>{const r=document.createElement('tr');r.dataset.id=e.id;r.className=sel===e.id?'sel':'';r.tabIndex=0;r.setAttribute('aria-label',`Select event ${e.id}, ${e.kind}`);r.setAttribute('role','button');
    [e.id,e.kind,hms(e.start_s),hms(e.end_s),(e.center_offset_hz/1000).toFixed(3),e.bandwidth_hz.toFixed(0),e.contrast_db.toFixed(1)+' dB'].forEach(value=>{const td=document.createElement('td');td.textContent=value;r.append(td)});
    r.onclick=()=>select(e.id,events);r.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();select(e.id,events)}};return r}));
}
function select(id,events){sel=+id;draw(events);table(events);mark()}
function mark(){
  $('rows').querySelectorAll('tr').forEach(r=>r.classList.toggle('sel',+r.dataset.id===sel));
  $('ov').querySelectorAll('rect').forEach(r=>r.classList.toggle('sel',+r.dataset.id===sel));
}
boot();
</script>
'''
