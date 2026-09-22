"""Downloadable frequency context. Matches describe references, never signal identity."""
import hashlib
import html
import json
import math
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parent
PLANS={'us':'US','international':'International','fr':'French'}
BAND_REPO='https://github.com/Arrin-KN1E/SDR-Band-Plans'
SATNOGS='https://db.satnogs.org'
DISCLAIMER='Frequency overlap only: not a decoded identification or evidence the transmitter was active at the recording location. Community band plans may be old, incomplete or locally inaccurate; they are not the authoritative legal allocation table.'


def default_reference_cache():
    """Keep source checkouts self-contained; installed packages use user cache."""
    local=ROOT/'.cache/spectrum'
    if (ROOT/'.git').exists(): return local
    base=Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache')).expanduser()
    return base/'iqscan'/'spectrum'


def add_arguments(p):
    p.add_argument('--bandplan',choices=['us','international','fr','none'],default='us',help='Community band-plan region (not automatically inferred from private location)')
    p.add_argument('--known-signals',choices=['satnogs','none'],default='none',help='Optional transmitter catalog; default is general band/service references only')
    p.add_argument('--sat',dest='known_signals',action='store_const',const='satnogs',help='Enable satellite transmitter hints (alias for --known-signals satnogs)')
    p.add_argument('--bandplan-file',type=Path,help='Replace selected plan with local SDR# XML or custom JSON')
    p.add_argument('--signals-file',type=Path,help='Additional known-signal JSON, no network upload')
    p.add_argument('--signal-status',choices=['active','all'],default='active',help='SatNOGS catalog status filter; active does not prove current reception')
    p.add_argument('--match-tolerance',type=float,default=5000,help='Known-signal frequency matching tolerance in Hz, e.g. for Doppler/oscillator error')
    p.add_argument('--max-reference-matches',type=int,default=5,help='Maximum known-signal hints per event; narrowest/nearest first')
    p.add_argument('--refresh-references',action='store_true',help='Redownload selected reference catalogs')
    p.add_argument('--offline-references',action='store_true',help='No reference network requests; use cache/local files only')
    p.add_argument('--reference-max-age-days',type=float,default=7,help='Cache download age before refresh; not age of upstream information')
    p.add_argument('--reference-cache',type=Path,default=default_reference_cache(),help='Local downloaded catalogs (XDG cache when installed)')
    p.add_argument('--update-references',action='store_true',help='Download/refresh selected catalogs and exit; no IQ file required')


def read_json(data):
    result=json.loads(data)
    if not isinstance(result,list) or any(not isinstance(row,dict) for row in result):raise ValueError('Catalog must be a JSON list of objects')
    return result


def custom(data):
    rows=read_json(data);result=[]
    for row in rows:
        lo=float(row.get('low_hz',row.get('frequency_hz',-1)))
        hi=float(row.get('high_hz',row.get('frequency_hz',-1)))
        if not math.isfinite(lo) or not math.isfinite(hi) or lo<0 or hi<lo or not row.get('name'):
            raise ValueError('Each custom entry requires name and finite 0 <= low_hz <= high_hz, or frequency_hz')
        result.append(dict(row,low_hz=lo,high_hz=hi,name=str(row['name']),mode=str(row.get('mode','')),status=str(row.get('status','unverified'))))
    return result


def bandplan(data):
    try:root=ET.fromstring(data,parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    except ET.ParseError as exc:raise ValueError('Invalid band-plan XML: '+str(exc)) from exc
    result=[]
    for el in root:
        if el.tag is ET.Comment:
            comment=' '.join((el.text or '').split())
            if result and len(comment)<250 and '<RangeEntry' not in comment:
                result[-1]['source_note']=comment
                if re.fullmatch(r'(?:Ch\.?\s*)?\d+',result[-1]['name']): result[-1]['name']+=' — '+comment
            continue
        if el.tag!='RangeEntry':continue
        lo=float(el.attrib['minFrequency']);hi=float(el.attrib['maxFrequency'])
        if not math.isfinite(lo) or not math.isfinite(hi) or lo<0 or hi<lo:raise ValueError('Invalid band-plan frequency range')
        result.append(dict(low_hz=lo,high_hz=hi,name=' '.join(''.join(el.itertext()).split()),mode=el.attrib.get('mode',''),status='unverified community reference'))
    if not result:raise ValueError('No RangeEntry records found')
    return result


def download(key,url,parse,args):
    cache=args.reference_cache.expanduser();path=cache/(key+'.json')
    saved=None
    try:
        saved=json.loads(path.read_text());parse(saved['raw'].encode())
        datetime.fromisoformat(saved['downloaded_utc'])
        if saved['source_url']!=url: saved=None
    except (OSError,ValueError,KeyError,TypeError,ET.ParseError):saved=None
    need=args.refresh_references or args.update_references or saved is None
    if saved:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['downloaded_utc'])).total_seconds()/86400
        need=need or age>args.reference_max_age_days
    note=''
    if not args.offline_references and need:
        try:
            req=Request(url,headers={'User-Agent':'RadioIQScanner/1.0'})
            with urlopen(req,timeout=25) as response:
                data=response.read(20_000_001)
                if len(data)>20_000_000:raise ValueError('Reference response exceeds 20 MB')
                modified=response.headers.get('Last-Modified')
            records=parse(data)
            if not records:raise ValueError('Empty reference catalog')
            saved=dict(source_url=url,downloaded_utc=datetime.now(timezone.utc).isoformat(),http_last_modified=modified,sha256=hashlib.sha256(data).hexdigest(),raw=data.decode('utf-8-sig'))
            cache.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile('w',dir=cache,delete=False) as f:
                json.dump(saved,f);tmp=Path(f.name)
            tmp.replace(path)
        except (OSError,ValueError,ET.ParseError) as exc:
            if saved is None:raise ValueError(f'{key} unavailable: {exc}') from exc
            note=f'Download failed; using cached catalog: {exc}'
    elif args.offline_references and saved is None:raise ValueError(f'{key}: no cached data; run --update-references online first')
    records=parse(saved['raw'].encode())
    info={k:v for k,v in saved.items() if k!='raw'}
    info['catalog']=key;info['records']=len(records);info['warning']=note
    if args.offline_references and need:info['warning']='Offline: cached download may be stale'
    if key.startswith('bandplan'):
        header=saved['raw'][:1500]
        match=re.search(r'\b\d{1,2}\s+[A-Za-z]+\s+20\d\d\b',header)
        info['upstream_header_date']=match[0] if match else 'not stated'
        info['frequency_span_hz']=[min(r['low_hz'] for r in records),max(r['high_hz'] for r in records)]
    return records,info


def load(args):
    if args.offline_references and (args.refresh_references or args.update_references):raise ValueError('Offline and refresh/update reference options cannot be combined')
    if not math.isfinite(args.match_tolerance) or args.match_tolerance<0:raise ValueError('Match tolerance must be finite and nonnegative')
    if not math.isfinite(args.reference_max_age_days) or args.reference_max_age_days<=0 or args.max_reference_matches<1:raise ValueError('Reference cache age and match count must be positive')
    bands=[];signals=[];sources=[];warnings=[]
    def fetched(key,url,parse):
        try:
            rows,info=download(key,url,parse,args);sources.append(info)
            if info.get('warning'):warnings.append(info['warning'])
            for r in rows:r.setdefault('source_url',url)
            return rows
        except (ValueError,OSError) as exc:
            warnings.append(str(exc));return []
    if args.bandplan_file:
        path=args.bandplan_file.expanduser();data=path.read_bytes();bands=bandplan(data) if path.suffix.lower()=='.xml' else custom(data)
        sources.append(dict(catalog='custom-bandplan',source_url='local file',records=len(bands),sha256=hashlib.sha256(data).hexdigest()))
    elif args.bandplan!='none':
        url=f'https://raw.githubusercontent.com/Arrin-KN1E/SDR-Band-Plans/master/{PLANS[args.bandplan]}/SDR%23/BandPlan.xml'
        bands=fetched('bandplan-'+args.bandplan,url,bandplan)
    if args.known_signals=='satnogs':
        tx=fetched('satnogs-transmitters',SATNOGS+'/api/transmitters/?format=json',read_json)
        sats=fetched('satnogs-satellites',SATNOGS+'/api/satellites/?format=json',read_json)
        names={r.get('sat_id'):r.get('name') for r in sats}
        for r in tx:
            if args.signal_status=='active' and (r.get('status')!='active' or r.get('alive') is False):continue
            lo=r.get('downlink_low');hi=r.get('downlink_high') or lo
            if lo is None or hi is None:continue
            lo=float(lo);hi=float(hi)
            if not math.isfinite(lo) or not math.isfinite(hi) or lo<0 or hi<lo:continue
            name=names.get(r.get('sat_id')) or f"NORAD {r.get('norad_cat_id','?')}"
            signals.append(dict(low_hz=lo,high_hz=hi,name=name+' — '+str(r.get('description','')),mode=r.get('mode'),baud=r.get('baud'),status=r.get('status'),updated=r.get('updated'),unconfirmed=r.get('unconfirmed'),norad=r.get('norad_cat_id'),source_url=SATNOGS+'/transmitter/'+str(r.get('uuid'))+'/'))
    if args.signals_file:
        path=args.signals_file.expanduser();data=path.read_bytes();rows=custom(data);signals.extend(rows)
        sources.append(dict(catalog='custom-signals',source_url='local file',records=len(rows),sha256=hashlib.sha256(data).hexdigest()))
    for message in warnings:print('Reference warning: '+message,file=sys.stderr)
    return dict(bands=bands,signals=signals,sources=sources,warnings=warnings,region=args.bandplan,disclaimer=DISCLAIMER)


def overlaps(a,b,lo,hi,tolerance=0):
    # Half-open bands, with point frequency entries supported.
    if a==b:return lo-tolerance<=a<hi+tolerance
    return a<hi+tolerance and b>lo-tolerance


def describe(meta,events,catalog,args):
    center=meta.get('center_frequency_hz')
    context={k:v for k,v in catalog.items() if k not in ('bands','signals')}
    context['match_tolerance_hz']=args.match_tolerance
    if center is None:
        context.update(bands=[],known_signals=[],warning='No absolute center frequency: frequency references cannot be matched.')
        meta['spectrum_context']=context;return
    lo=center-meta['sample_rate']/2;hi=center+meta['sample_rate']/2
    bands=[b for b in catalog['bands'] if overlaps(b['low_hz'],b['high_hz'],lo,hi)]
    signals=[b for b in catalog['signals'] if overlaps(b['low_hz'],b['high_hz'],lo,hi,args.match_tolerance)]
    signals.sort(key=lambda r:(abs((r['low_hz']+r['high_hz'])/2-center),r['name']))
    context.update(low_hz=lo,high_hz=hi,bands=bands,known_signals=signals)
    for e in events:
        a=center+e['low_offset_hz'];b=center+e['high_offset_hz']
        e['band_references']=[r for r in bands if overlaps(r['low_hz'],r['high_hz'],a,b)]
        matches=[]
        for r in signals:
            if overlaps(r['low_hz'],r['high_hz'],a,b,args.match_tolerance):
                gap=max(r['low_hz']-b,a-r['high_hz'],0)
                matches.append(dict(r,frequency_gap_hz=round(gap,1),match_basis='Frequency overlap/proximity only; not identified'))
        matches.sort(key=lambda r:(r['frequency_gap_hz'],r['high_hz']-r['low_hz'],r['name']))
        e['known_signal_match_count']=len(matches);e['known_signal_hints']=matches[:args.max_reference_matches]
    meta['spectrum_context']=context


def brief(event):
    bands=list(dict.fromkeys(r['name'] for r in event.get('band_references',[])))
    hints=[r['name']+f" [{r.get('mode') or '?'}; catalog {r.get('status','unknown')}]" for r in event.get('known_signal_hints',[])]
    return '; '.join(bands[:3]),'; '.join(hints)


def html_context(meta):
    c=meta.get('spectrum_context',{});esc=lambda x:html.escape(str(x))
    if not c:return ''
    text='<h2>Spectrum reference context</h2><p>'+esc(c.get('disclaimer',DISCLAIMER))+'</p>'
    for warning in c.get('warnings',[])+([c['warning']] if c.get('warning') else []):text+='<p>'+esc(warning)+'</p>'
    text+='<p>Selected band plan: '+esc(c.get('region'))+'. Match tolerance: '+esc(c.get('match_tolerance_hz'))+' Hz. Source download dates are not verification dates.</p>'
    for title,key in [('Band/service reference entries','bands'),('Known-signal references in/near this capture','known_signals')]:
        rows=c.get(key,[])
        text+='<h3>'+title+f' ({len(rows)})</h3><div class="scroll"><table><tr><th>Frequency range (MHz)</th><th>Reference</th><th>Mode / status</th></tr>'
        for r in rows[:80]:
            text+=f'<tr><td>{r["low_hz"]/1e6:.6f}–{r["high_hz"]/1e6:.6f}</td><td>{esc(r["name"])}</td><td>{esc(r.get("mode",""))} / {esc(r.get("status","unverified"))}</td></tr>'
        text+='</table></div>'
        if len(rows)>80:text+='<p>First 80 shown; full entries are in events.json.</p>'
    text+='<h3>Sources</h3><ul>'
    for s in c.get('sources',[]):
        url=s.get('source_url','');link=f'<a href="{esc(url)}">{esc(s["catalog"])}</a>' if url.startswith('https://') else esc(s['catalog'])
        text+=f'<li>{link}: {s["records"]} records; downloaded {esc(s.get("downloaded_utc","local"))}; upstream header date {esc(s.get("upstream_header_date","not stated"))}</li>'
    return text+'</ul>'


def plot_context(meta,out):
    import matplotlib.pyplot as plt
    import textwrap
    c=meta.get('spectrum_context',{})
    if 'low_hz' not in c:return
    rows=[dict(r,label='Band: '+r['name']) for r in c['bands']]+[dict(r,label='Catalog: '+r['name']) for r in c['known_signals']]
    rows=rows[:24]
    if not rows:return
    fig,ax=plt.subplots(figsize=(13,max(4,len(rows)*.36+1.5)),layout='constrained')
    lo,hi=c['low_hz']/1e6,c['high_hz']/1e6
    for i,r in enumerate(rows):
        a=max(lo,r['low_hz']/1e6);b=min(hi,r['high_hz']/1e6)
        color='#4277aa' if r['label'].startswith('Band:') else '#b26a25'
        if a<=b:
            if a==b:ax.plot(a,i,'|',color=color,markersize=12)
            else:ax.plot([a,b],[i,i],color=color,lw=5,solid_capstyle='butt')
    ax.set_yticks(range(len(rows)),[textwrap.shorten(r['label'],width=65,placeholder='…') for r in rows],fontsize=8)
    ax.invert_yaxis();ax.set_xlim(lo,hi);ax.set_xlabel('Frequency (MHz)');ax.ticklabel_format(useOffset=False,axis='x');ax.grid(axis='x',alpha=.2)
    ax.set_title('Spectrum references — NOT detected signal identities\nCommunity plan + catalog entries; not verified current operation')
    fig.savefig(out/'spectrum-context.png',dpi=140)
    from report_interactive import panel
    result=panel(fig,ax,'reference');plt.close(fig)
    return result
