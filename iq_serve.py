#!/usr/bin/env python3
"""Local explorer for cached spectra: re-run detection live over spectrum.npz.

Exploration only. The CLI still produces the shareable report, and the app hands
you the exact --redetect command for any parameter set you settle on. Binds to
loopback; there is no authentication and none is intended.
"""
import io
import json
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
        import numpy as np
        found = []
        for directory in sorted((p for p in self.root.iterdir() if p.is_dir()), reverse=True):
            path = directory/'spectrum.npz'
            if not path.exists():
                continue
            try:
                with np.load(path, allow_pickle=False) as data:      # lazy: reads one entry
                    meta = json.loads(str(data['meta']))
                    shaping = json.loads(str(data['spectrum_args']))
            except (OSError, ValueError, KeyError):
                continue                                             # a half-written cache is skipped, not fatal
            found.append(dict(id=directory.name, name=Path(meta['input']).name, duration_s=meta['duration_s'],
                sample_rate=meta['sample_rate'], center_frequency_hz=meta['center_frequency_hz'],
                rows=meta.get('rows'), frequency_bin_hz=meta['frequency_bin_hz'], shaping=shaping))
        return found

    def resolve(self, scan_id):
        # Only a direct child of the scan root, by name: no traversal, no absolute paths.
        if not scan_id or '/' in scan_id or '\\' in scan_id or scan_id in ('.', '..'):
            raise ValueError('Invalid scan id')
        directory = self.root/scan_id
        if not (directory.is_dir() and (directory/'spectrum.npz').exists()):
            raise ValueError(f'No cached spectrum for {scan_id}')
        return directory

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
                self.images[scan_id] = waterfall_png(self.get(scan_id)[2])
                if len(self.images) > self.keep:
                    self.images.pop(next(iter(self.images)))
            return self.images[scan_id]


def waterfall_png(norm):
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
    axes.imshow(norm, aspect='auto', cmap='magma', vmin=-3, vmax=max(12, float(np.percentile(norm, 99.5))))
    buffer = io.BytesIO()
    figure.savefig(buffer, format='png', dpi=100)
    plt.close(figure)
    return buffer.getvalue()


def detect_params(query, defaults):
    args = Namespace(**defaults)
    for key, cast in DETECT_ARGS.items():
        if key in query:
            try:
                value = cast(query[key][0])
            except ValueError:
                raise ValueError(f'{key} must be {cast.__name__}')
            setattr(args, key, value)
    if args.threshold <= 0 or args.min_duration < 0 or args.dc_exclude < 0 or args.top < 1:
        raise ValueError('Require threshold > 0, min-duration >= 0, dc-exclude >= 0 and top >= 1')
    return args


def command_for(scan_id, args):
    parts = [f'./scan.sh --redetect scans/{scan_id}']
    for key in DETECT_ARGS:
        parts.append(f"--{key.replace('_','-')} {getattr(args, key):g}")
    return ' '.join(parts)


def handler_for(state, defaults):
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
                    return self.send_json(dict(scans=state.available(), defaults=defaults))
                scan_id = query.get('scan', [''])[0]
                if url.path == '/api/image':
                    return self.send(200, state.image(scan_id), 'image/png')
                if url.path == '/api/detect':
                    meta, f, norm, _reference, dt, shaping = state.get(scan_id)
                    args = detect_params(query, defaults)
                    events = iq_scan.detect(dict(meta), args, f, norm, dt)
                    return self.send_json(dict(events=events, shaping=shaping,
                        command=command_for(scan_id, args),
                        extent=dict(low_offset_hz=float(f[0]), high_offset_hz=float(f[-1]),
                            duration_s=meta['duration_s'], center_frequency_hz=meta['center_frequency_hz'],
                            sample_rate=meta['sample_rate'])))
                return self.send_json(dict(error='Not found'), 404)
            except ValueError as exc:
                return self.send_json(dict(error=str(exc)), 400)
            except Exception as exc:                                 # a bad request must not kill the server
                return self.send_json(dict(error=f'{type(exc).__name__}: {exc}'), 500)
    return Handler


def serve(root, port=8731, open_browser=True):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f'No scan directory: {root}')
    state = Spectra(root)
    ready = state.available()
    if not ready:
        raise ValueError(f'No spectrum.npz under {root}. Run a scan with --save-spectrum first.')
    parser_defaults = iq_scan.parser().parse_args([])
    defaults = {key: getattr(parser_defaults, key) for key in DETECT_ARGS}
    server = ThreadingHTTPServer(('127.0.0.1', port), handler_for(state, defaults))
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
rect.ev.sel{fill:rgba(255,215,100,.28);stroke:#ffd764}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:14px}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:600}tr.sel td{background:rgba(255,215,100,.1)}
tbody tr{cursor:pointer}code{display:block;background:#0c111a;border:1px solid var(--line);border-radius:6px;padding:9px;margin-top:12px;font-size:12px;overflow-wrap:anywhere;color:var(--accent)}
.axis{display:flex;justify-content:space-between;color:var(--dim);font-size:11px;margin-top:4px}
.note{color:var(--dim);font-size:12px;margin-top:14px;line-height:1.5}
.err{color:#ff9d9d}.busy{opacity:.55}
</style>
<h1>IQ scan explorer</h1>
<p class="sub">Live re-detection over a cached spectrum. Frequency references and contrast are hints, not identification.</p>
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
  <p class="note">Threshold is applied before regions are formed, so raising it reshapes and splits events rather than just hiding them &mdash; that is why these boxes change size. Run the command above to produce a real report with images and clips.</p>
</div></div>
<script>
const $=id=>document.getElementById(id), KEYS=['threshold','min_duration','dc_exclude','top'];
let extent=null, sel=null, timer=null, seq=0;
const hms=s=>{const h=Math.floor(s/3600),m=Math.floor(s/60)%60,x=(s%60).toFixed(3).padStart(6,'0');return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${x}`};
function show(){KEYS.forEach(k=>$('v'+k).textContent=$(k).value)}
function schedule(){show();clearTimeout(timer);timer=setTimeout(run,120)}
async function boot(){
  const r=await fetch('/api/scans'); const d=await r.json();
  d.scans.forEach(s=>{const o=document.createElement('option');o.value=s.id;o.textContent=`${s.name} — ${hms(s.duration_s)}`;$('scan').append(o)});
  KEYS.forEach(k=>{$(k).value=d.defaults[k];$(k).addEventListener('input',schedule)});
  $('scan').addEventListener('change',load); load();
}
function load(){$('wf').src='/api/image?scan='+encodeURIComponent($('scan').value); sel=null; run()}
async function run(){
  const mine=++seq, q=new URLSearchParams({scan:$('scan').value});
  KEYS.forEach(k=>q.set(k,$(k).value));
  document.body.classList.add('busy');
  let d;
  try{ d=await (await fetch('/api/detect?'+q)).json(); }
  catch(e){ $('msg').innerHTML='<span class="err">Request failed: '+e+'</span>'; document.body.classList.remove('busy'); return; }
  if(mine!==seq) return;
  document.body.classList.remove('busy');
  if(d.error){ $('msg').innerHTML='<span class="err">'+d.error+'</span>'; return }
  extent=d.extent; $('cmd').textContent=d.command; $('msg').textContent=d.events.length+' candidate(s).';
  $('shaping').textContent='Cached FFT shaping: '+Object.entries(d.shaping).map(([k,v])=>k+'='+v).join(', ')+'. Rescan to change these.';
  const c=extent.center_frequency_hz, u=c?1e6:1e3;
  $('flo').textContent=((c||0)+extent.low_offset_hz)/u+(c?' MHz':' kHz');
  $('fhi').textContent=((c||0)+extent.high_offset_hz)/u+(c?' MHz':' kHz');
  draw(d.events); table(d.events);
}
function box(e){
  const span=extent.high_offset_hz-extent.low_offset_hz;
  return {x:100*(e.low_offset_hz-extent.low_offset_hz)/span, w:Math.max(.4,100*(e.high_offset_hz-e.low_offset_hz)/span),
          y:100*e.start_s/extent.duration_s, h:Math.max(.4,100*(e.end_s-e.start_s)/extent.duration_s)};
}
function draw(events){
  $('ov').innerHTML=events.map(e=>{const b=box(e);
    return `<rect class="ev${sel===e.id?' sel':''}" data-id="${e.id}" x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}"><title>#${e.id} ${e.kind} ${e.contrast_db} dB</title></rect>`}).join('');
  $('ov').querySelectorAll('rect').forEach(r=>r.onclick=()=>{sel=+r.dataset.id;draw(events);mark()});
}
function table(events){
  $('rows').innerHTML=events.map(e=>`<tr data-id="${e.id}" class="${sel===e.id?'sel':''}"><td>${e.id}</td><td>${e.kind}</td><td>${hms(e.start_s)}</td><td>${hms(e.end_s)}</td><td>${(e.center_offset_hz/1000).toFixed(3)}</td><td>${e.bandwidth_hz.toFixed(0)}</td><td>${e.contrast_db.toFixed(1)} dB</td></tr>`).join('');
  $('rows').querySelectorAll('tr').forEach(r=>r.onclick=()=>{sel=+r.dataset.id;draw(events);mark()});
}
function mark(){
  $('rows').querySelectorAll('tr').forEach(r=>r.classList.toggle('sel',+r.dataset.id===sel));
  $('ov').querySelectorAll('rect').forEach(r=>r.classList.toggle('sel',+r.dataset.id===sel));
}
boot();
</script>
'''
