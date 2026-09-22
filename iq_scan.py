#!/usr/bin/env python3
"""Find candidate activity in signed complex IQ recordings. No protocol identification."""
import argparse, csv, html, json, math, os, re, shlex, sys, tempfile
from datetime import datetime
from pathlib import Path


def format_time(seconds):
    """Elapsed HH:MM:SS.mmm, with rounding carry and no 24-hour wrap."""
    millis = round(abs(float(seconds))*1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    sign = '-' if seconds < 0 and (hours or minutes or secs or millis) else ''
    return f'{sign}{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}'


def positive(value):
    x=float(value)
    if not math.isfinite(x) or x<=0: raise argparse.ArgumentTypeError('Must be positive and finite')
    return x


def parser():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('file',type=Path,nargs='?')
    p.add_argument('--sample-rate',type=positive,help='Complex samples/sec; otherwise read NNNsps from filename')
    p.add_argument('--center-frequency',type=positive,help='Hz; otherwise read NNNHz from filename; optional')
    p.add_argument('--format',choices=['cs8','cs16'],help='Otherwise use file extension; cs16 is little-endian')
    p.add_argument('--open',action='store_true',help='Open the completed HTML report in your default browser')
    p.add_argument('--output',type=Path,help='New output directory; existing directories are never overwritten')
    p.add_argument('--fft-size',type=int,default=4096,help='Power of two, 256..16384')
    p.add_argument('--time-bin',type=positive,default=.1,help='Requested time resolution in seconds')
    p.add_argument('--max-rows',type=int,default=2000,help='Bound analysis memory; long files get coarser time bins')
    p.add_argument('--threshold',type=positive,default=7,help='Transient excess above each frequency baseline, dB')
    p.add_argument('--min-duration',type=positive,default=.15,help='Minimum transient duration in seconds')
    p.add_argument('--dc-exclude',type=float,default=2000,help='Ignore this many Hz either side of center in detection')
    p.add_argument('--top',type=int,default=20,help='Maximum events in report')
    p.add_argument('--save-spectrum',action='store_true',help='Also write spectrum.npz so --redetect can rerun detection without recomputing FFTs')
    p.add_argument('--redetect',type=Path,help='Reuse spectrum.npz from a previous scan directory instead of reading the recording again')
    p.add_argument('--clips',type=int,default=10,help='Export this many event clips; 0 disables')
    p.add_argument('--padding',type=float,default=1,help='Clip padding on each side, seconds')
    p.add_argument('--max-clip-seconds',type=positive,default=10,help='Cap each clip; long events are clipped around strongest time')
    p.add_argument('--color',choices=['auto','always','never'],default='auto')
    from spectrum_refs import add_arguments
    add_arguments(p)
    return p


def metadata(args):
    path=args.file.expanduser().resolve()
    fmt=args.format or path.suffix.lower().lstrip('.')
    wav=None
    with path.open('rb') as probe:
        signature=probe.read(12)
    if fmt=='wav' or (signature[:4] in (b'RIFF',b'RF64') and signature[8:12]==b'WAVE'):
        from iq_wav import read_header
        wav=read_header(path);fmt='cs16'
        if args.format and args.format!='cs16':raise ValueError('WAV payload is cs16; conflicting --format')
        if args.sample_rate and args.sample_rate!=wav['sample_rate']:raise ValueError('--sample-rate conflicts with WAV header')
    if fmt not in ('cs8','cs16'): raise ValueError('Use .cs8, .cs16, or a two-channel PCM16 IQ .wav file')
    match=re.search(r'(\d+(?:\.\d+)?)SPS',path.name,re.I)
    fs=wav['sample_rate'] if wav else args.sample_rate or (float(match[1]) if match else None)
    match=re.search(r'(\d+(?:\.\d+)?)Hz',path.name,re.I)
    center=args.center_frequency or (float(match[1]) if match else None)
    if fs is None or fs<=0: raise ValueError('Sample rate missing: provide --sample-rate 500000 (complex samples/sec)')
    size=wav['bytes'] if wav else path.stat().st_size; bpc=2 if fmt=='cs8' else 4
    if size==0 or size%bpc: raise ValueError('File is empty or ends with an incomplete IQ sample')
    if args.fft_size<256 or args.fft_size>16384 or args.fft_size&(args.fft_size-1): raise ValueError('--fft-size must be a power of two from 256 to 16384')
    if not 10<=args.max_rows<=10000 or args.top<1 or args.clips<0: raise ValueError('Require max-rows 10..10000, top >=1 and clips >=0')
    if not math.isfinite(args.dc_exclude) or not 0<=args.dc_exclude<fs*.45: raise ValueError('--dc-exclude must be nonnegative and below 45% of sample rate')
    if not math.isfinite(args.padding) or args.padding<0: raise ValueError('--padding must be nonnegative')
    if size//bpc<args.fft_size: raise ValueError('Recording is shorter than one FFT; reduce --fft-size')
    return dict(input=str(path),data_offset=wav['data_offset'] if wav else 0,container=wav['container'] if wav else 'raw',input_warning=wav.get('warning') if wav else None,format=fmt,sample_rate=fs,center_frequency_hz=center,bytes=size,bytes_per_complex=bpc,samples=size//bpc,duration_s=size/bpc/fs)


def spectrum(meta,args):
    import numpy as np
    a=np.memmap(meta['input'],dtype='i1' if meta['format']=='cs8' else '<i2',mode='r',offset=meta.get('data_offset',0),shape=(meta['samples']*2,)).reshape(-1,2)
    n=args.fft_size; fs=meta['sample_rate']; frames=math.ceil(len(a)/n)
    navg=max(1,round(args.time_bin*fs/n),math.ceil(frames/args.max_rows))
    rows=math.ceil(frames/navg); w=np.hanning(n); scale=128 if meta['format']=='cs8' else 32768
    ps=np.zeros((rows,n),dtype=np.float32); rail=0; total=0
    for row in range(rows):
        first=row*navg; count=min(navg,frames-first)
        for k in range(first,first+count,64):
            nf=min(64,first+count-k); b=a[k*n:min((k+nf)*n,len(a))].astype(np.float32)
            rail+=int(np.count_nonzero((b==-scale)|(b==scale-1))); total+=b.size
            z=(b[:,0]+1j*b[:,1])/scale
            if len(z)<nf*n: z=np.pad(z,(0,nf*n-len(z)))
            power=abs(np.fft.fft(z.reshape(nf,n)*w,axis=1))**2
            ps[row]+=np.sum(power,axis=0).astype(np.float32)
        ps[row]/=count*fs*np.sum(w*w)
        if row%max(1,rows//10)==0: print(f'\rScanning {100*(row+1)/rows:3.0f}%',end='',file=sys.stderr,flush=True)
    print('\rScanning 100%',file=sys.stderr)
    ps=np.fft.fftshift(ps,axes=1); f=np.fft.fftshift(np.fft.fftfreq(n,1/fs)); dt=navg*n/fs
    db=10*np.log10(np.maximum(ps,1e-30))
    reference=np.median(db[:,(abs(f)>.22*fs)&(abs(f)<.4*fs)],axis=1)
    norm=db-reference[:,None]
    meta.update(time_bin_s=dt,frequency_bin_hz=fs/n,rail_fraction=rail/total,rows=rows,
                analysis_note='All samples processed; final FFT zero padded if incomplete. PSD levels are relative digital units, not calibrated RF power.')
    return f,norm,reference,dt


# Only what shapes the cached matrix. dc_exclude and threshold live in detect(),
# so a redetect run is free to change them.
SPECTRUM_ARGS = ('fft_size','time_bin','max_rows')


def save_spectrum(meta,spectrum_args,f,norm,reference,dt,out):
    import numpy as np
    # The normalized matrix is what detect() consumes; caching it skips rereading the
    # recording and recomputing every FFT when only detection parameters change.
    np.savez(out/'spectrum.npz',f=f,norm=norm.astype(np.float32),reference=reference,
        dt=dt,meta=json.dumps(meta),spectrum_args=json.dumps(spectrum_args))


def load_spectrum(directory):
    import numpy as np
    path=Path(directory).expanduser().resolve()/'spectrum.npz'
    if not path.exists(): raise ValueError(f'No spectrum.npz in {directory}. Rerun that scan with --save-spectrum.')
    with np.load(path,allow_pickle=False) as data:
        return (json.loads(str(data['meta'])),data['f'],data['norm'],data['reference'],
                float(data['dt']),json.loads(str(data['spectrum_args'])))


def detect(meta,args,f,norm,dt):
    import numpy as np
    from scipy.ndimage import label,find_objects,median_filter
    baseline=np.median(norm,axis=0); excess=norm-baseline[None,:]
    valid=(abs(f)>args.dc_exclude)&(abs(f)<meta['sample_rate']*.45)
    mask=(excess>=args.threshold)&valid[None,:]
    labs,_=label(mask);events=[];df=meta['frequency_bin_hz']
    def event(kind,ti,fi,peak,power,pixels):
        start=ti.start*dt;end=min(ti.stop*dt,meta['duration_s'])
        return dict(kind=kind,start_s=round(start,6),end_s=round(end,6),duration_s=round(end-start,6),
            low_offset_hz=round(float(f[fi.start]-df/2),3),high_offset_hz=round(float(f[fi.stop-1]+df/2),3),
            center_offset_hz=round(float((f[fi.start]+f[fi.stop-1])/2),3),
            bandwidth_hz=round((fi.stop-fi.start)*df,3),peak_time_s=round(min((peak+.5)*dt,meta['duration_s']),6),
            contrast_db=round(float(power),2),pixels=int(pixels))
    for i,sl in enumerate(find_objects(labs),1):
        if sl is None:continue
        ti,fi=sl;region=labs[sl]==i;pixels=int(region.sum())
        if pixels<4 or min(ti.stop*dt,meta['duration_s'])-ti.start*dt<args.min_duration:continue
        local=np.where(region,excess[sl],-np.inf);loc=np.unravel_index(local.argmax(),local.shape)
        events.append(event('transient',ti,fi,ti.start+loc[0],local[loc],pixels))
    # Persistent narrow peaks relative to nearby frequencies; not satellite IDs.
    smooth=median_filter(baseline,size=101,mode='nearest'); contrast=baseline-smooth
    labs,_=label((contrast>=args.threshold)&valid)
    for sl in find_objects(labs):
        if sl is None:continue
        fi=sl[0];frequency_index=fi.start+int(np.argmax(contrast[fi]))
        peak=int(np.argmax(norm[:,frequency_index]))
        events.append(event('persistent',slice(0,len(norm)),fi,peak,float(contrast[fi].max()),fi.stop-fi.start))
    events.sort(key=lambda e:(e['kind']=='transient',e['pixels'] if e['kind']=='transient' else e['contrast_db']),reverse=True)
    events=events[:args.top]
    for i,e in enumerate(events,1):
        e['id']=i
        e['frequency_hz']=None if meta['center_frequency_hz'] is None else round(meta['center_frequency_hz']+e['center_offset_hz'],3)
    return events


def clips(meta,args,events,out):
    # Open the recording only when clips are actually wanted; a redetect run may no
    # longer have the original file.
    if not args.clips: return
    folder=out/'clips';folder.mkdir()
    with open(meta['input'],'rb') as src:
        for e in events[:args.clips]:
            a=max(0,e['start_s']-args.padding);b=min(meta['duration_s'],e['end_s']+args.padding)
            if b-a>args.max_clip_seconds:
                a=max(a,e['peak_time_s']-args.max_clip_seconds/2);b=min(meta['duration_s'],a+args.max_clip_seconds)
            first=int(a*meta['sample_rate']);last=min(meta['samples'],math.ceil(b*meta['sample_rate']))
            name=f"event-{e['id']:02d}_{meta['sample_rate']:g}SPS.{meta['format']}";path=folder/name
            src.seek(meta.get('data_offset',0)+first*meta['bytes_per_complex']);remaining=(last-first)*meta['bytes_per_complex']
            with path.open('wb') as dest:
                while remaining:
                    chunk=src.read(min(remaining,1024*1024))
                    if not chunk:raise ValueError('Unexpected end of input while exporting clip')
                    dest.write(chunk);remaining-=len(chunk)
            e.update(clip=f'clips/{name}',clip_start_original_s=first/meta['sample_rate'],clip_duration_s=(last-first)/meta['sample_rate'],
                     event_start_in_clip_s=max(0,e['start_s']-first/meta['sample_rate']))
            e['open_command']='inspectrum '+shlex.quote(str(path.resolve()))


def report(meta,args,events,f,norm,reference,dt,out):
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    from spectrum_refs import html_context, plot_context, brief
    from report_interactive import panel, inject
    plots={}
    context_panel=plot_context(meta,out)
    if context_panel:plots["spectrum-context.png"]=[context_panel]
    time_ticks = FuncFormatter(lambda value, position: format_time(value))
    center=meta['center_frequency_hz'];freq=(f+(center or 0))/(1e6 if center else 1e3)
    unit='Frequency (MHz)' if center else 'Offset (kHz)'
    fig,(ax,bx)=plt.subplots(2,1,figsize=(13,9),layout='constrained',gridspec_kw={'height_ratios':[4,1]})
    im=ax.imshow(norm,extent=(freq[0],freq[-1],meta['duration_s'],0),aspect='auto',cmap='magma',vmin=-3,vmax=max(12,float(np.percentile(norm,99.5))))
    ax.set(xlabel=unit,ylabel='Elapsed time (hh:mm:ss.mmm)',title='Candidate activity — broadband level changes removed')
    ax.yaxis.set_major_formatter(time_ticks)
    ax.ticklabel_format(useOffset=False,axis='x')
    for e in events:
        x=((center or 0)+e['center_offset_hz'])/(1e6 if center else 1e3)
        ax.annotate(str(e['id']),(x,e['peak_time_s']),xytext=(5,0),textcoords='offset points',color='cyan',fontsize=9,bbox=dict(facecolor='black',alpha=.6,edgecolor='none'))
    fig.colorbar(im,ax=ax,label='dB above per-time reference band')
    bx.plot(np.minimum((np.arange(len(reference))+.5)*dt,meta['duration_s']),reference,lw=.8)
    bx.xaxis.set_major_formatter(time_ticks)
    bx.set(xlabel='Elapsed time (hh:mm:ss.mmm)',ylabel='Reference PSD\n(digital dB/Hz)',title='Broadband level: gain changes and interference can affect this trace');fig.savefig(out/'waterfall.png',dpi=140)
    plots['waterfall.png']=[panel(fig,ax,'waterfall',norm),panel(fig,bx,'trace')]
    plt.close(fig)
    images=out/'images';images.mkdir()
    for e in events:
        lo=e['low_offset_hz'];hi=e['high_offset_hz'];margin=max(3000,(hi-lo)*2)
        fi=(f>=lo-margin)&(f<=hi+margin)
        start=max(0,e['start_s']-max(1,dt));end=min(meta['duration_s'],e['end_s']+max(1,dt))
        ia=max(0,int(start/dt));ib=min(len(norm),math.ceil(end/dt))
        if ib<=ia or not fi.any():continue
        fig,ax=plt.subplots(figsize=(9,5),layout='constrained')
        section=norm[ia:ib][:,fi]
        im=ax.imshow(section,extent=(freq[fi][0],freq[fi][-1],min(ib*dt,meta['duration_s']),ia*dt),aspect='auto',cmap='magma',vmin=-3,vmax=max(12,float(np.percentile(section,99.5))))
        ax.set(xlabel=unit,ylabel='Original elapsed time (hh:mm:ss.mmm)',title=f"Event {e['id']} | {e['kind']} | unidentified activity")
        ax.yaxis.set_major_formatter(time_ticks)
        ax.ticklabel_format(useOffset=False,axis='x')
        fig.colorbar(im,ax=ax,label='dB above per-time reference band')
        name=f"images/event-{e['id']:02d}.png";fig.savefig(out/name,dpi=150)
        plots[name]=[panel(fig,ax,'waterfall',section)]
        plt.close(fig);e['image']=name
    for e in events:
        for key in ('start_s','end_s','duration_s','peak_time_s','clip_start_original_s','clip_duration_s','event_start_in_clip_s'):
            if key in e: e[key[:-2]+'_hms'] = format_time(e[key])
    meta['duration_hms'] = format_time(meta['duration_s'])
    (out/'events.json').write_text(json.dumps(dict(metadata=meta,settings=vars_serial(args),events=events),indent=2)+'\n')
    fields=list(dict.fromkeys(k for e in events for k in e)) or ['id','kind','start_s','end_s','frequency_hz']
    with (out/'events.csv').open('w',newline='') as fp:
        writer=csv.DictWriter(fp,fieldnames=fields);writer.writeheader();writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in e.items()} for e in events)
    table=[]
    for e in events:
        frequency=f"{e['frequency_hz']/1e6:.6f} MHz" if center else f"{e['center_offset_hz']/1000:+.3f} kHz"
        link=f'<a href="{html.escape(e["clip"])}">IQ clip</a>' if 'clip' in e else ''
        bands,hints=brief(e)
        context_text=html.escape(bands or 'No band-plan match')+'<br><small>'+html.escape(hints or 'No cataloged signal match')+'</small>'
        table.append(f'<tr><td>{e["id"]}</td><td>{e["kind"]}</td><td>{format_time(e["start_s"])}–{format_time(e["end_s"])}</td><td>{frequency}</td><td>{e["bandwidth_hz"]:.0f}</td><td>{e["contrast_db"]:.1f}</td><td>{link}</td><td>{context_text}</td></tr>')
    note='Candidates only: no transmitter or protocol identification. Transient contrast is relative to that frequency’s usual level; persistent contrast is relative to nearby frequencies. Values are not calibrated SNR. Stationary spurs, gain changes, and interference can trigger detections. Weak, broad, or continuous signals may be missed.'
    gallery=''.join(f'<h2>Event {e["id"]}</h2><img loading="lazy" src="{e["image"]}" alt="Event {e["id"]} close-up">' for e in events if 'image' in e)
    context_html=html_context(meta)
    if (out/'spectrum-context.png').exists(): context_html+='<img src="spectrum-context.png" alt="Band and known-signal reference chart">'
    page=f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>IQ scan</title><style>body{{background:#10151e;color:#e5edf8;font:16px system-ui;max-width:1200px;margin:30px auto;padding:20px}}a{{color:#79dfff}}img{{width:100%}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #354050}}.scroll{{overflow-x:auto}}code{{overflow-wrap:anywhere}}</style><h1>IQ recording scan</h1><p>{html.escape(Path(meta['input']).name)}</p><p>{format_time(meta['duration_s'])} duration · {meta['sample_rate']:g} samples/s · {meta['format']} · {len(events)} reported candidates</p><p>Resolution: {format_time(dt)} × {meta['frequency_bin_hz']:.1f} Hz. Center: {center if center else 'unknown; offsets only'} Hz.</p><img src="waterfall.png" alt="Annotated waterfall and broadband level"><div class="scroll"><table><tr><th>ID</th><th>Type</th><th>Elapsed time (hh:mm:ss.mmm)</th><th>Frequency</th><th>Detected width (Hz)</th><th>Contrast (dB)</th><th>Clip</th><th>Frequency references (not identification)</th></tr>{''.join(table)}</table></div><p>{note}</p><p>Clips preserve the original format, sample rate and full bandwidth. Use sample rate {meta['sample_rate']:g} in inspectrum. Clip time starts at zero. See events.json for original start times and open commands.</p>{context_html}{gallery}'''
    if meta.get('input_warning'):page=page.replace('<h1>IQ recording scan</h1>','<h1>IQ recording scan</h1><p><strong>Input warning:</strong> '+html.escape(meta['input_warning'])+'</p>')
    page=inject(page,plots,events,meta.get('spectrum_context',{}).get('bands',[]),center)
    (out/'report.html').write_text(page)
    instructions=['Open clips with inspectrum; set sample rate to '+str(meta['sample_rate'])+'.','Frequency offsets are relative to '+str(center)+' Hz.','Clips are exact excerpts, not filtered or frequency-shifted.','']
    for e in events:
        if 'clip' in e:instructions.extend([f"Event {e['id']}: original start {format_time(e['clip_start_original_s'])}; event begins {format_time(e['event_start_in_clip_s'])} into clip; offset {e['center_offset_hz']/1000:+.3f} kHz.",e['open_command'],''])
    (out/'OPEN-CLIPS.txt').write_text('\n'.join(instructions))


def vars_serial(args):return {k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        from spectrum_refs import load as load_references, describe, brief
        if args.update_references:
            catalog=load_references(args)
            for source in catalog['sources']:
                print(f"{source['catalog']}: {source['records']} records | {source.get('downloaded_utc','local file')}")
            print('Reference cache:',args.reference_cache)
            return 2 if catalog['warnings'] else 0
        if args.file is None and args.redetect is None: raise ValueError('Provide an IQ recording, --redetect DIR, or --update-references')
        cached=load_spectrum(args.redetect) if args.redetect is not None else None
        meta=cached[0] if cached else metadata(args)
        out=(args.output or Path.cwd()/'scans'/(Path(meta['input']).stem+'_'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))).expanduser().resolve()
        if out.exists():raise ValueError(f'Output already exists; choose a new directory: {out}')
        catalog=load_references(args) if meta['center_frequency_hz'] is not None else dict(bands=[],signals=[],sources=[],warnings=[],region=args.bandplan)
        import numpy, scipy
        with tempfile.TemporaryDirectory(prefix='iq-scan-') as tmp:
            os.environ.setdefault('MPLCONFIGDIR',tmp)
            if cached:
                _cached_meta,f,norm,reference,dt,spectrum_args=cached
                meta['redetected_from']=str(Path(args.redetect).expanduser().resolve())
                print('Reusing cached spectrum; FFT shaping comes from that scan ('
                    +', '.join(f'{k}={v}' for k,v in spectrum_args.items())+'), not this command line.',file=sys.stderr)
            else:
                f,norm,reference,dt=spectrum(meta,args)
                spectrum_args={k:getattr(args,k) for k in SPECTRUM_ARGS}
            print(f'Resolution: {format_time(dt)}, {meta["frequency_bin_hz"]:.1f} Hz; narrow/short signals may be diluted.',file=sys.stderr)
            events=detect(meta,args,f,norm,dt)
            describe(meta,events,catalog,args)
            out.mkdir(parents=True)
            if args.clips and not Path(meta['input']).exists():
                print(f"WARNING: {meta['input']} is gone; skipping clip export. Detection and images are unaffected.",file=sys.stderr)
                args.clips=0
            clips(meta,args,events,out)
            report(meta,args,events,f,norm,reference,dt,out)
            if args.save_spectrum: save_spectrum(meta,spectrum_args,f,norm,reference,dt,out)
        colored=args.color=='always' or args.color=='auto' and sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM')!='dumb'
        paint=lambda s:f'\033[96m{s}\033[0m' if colored else s
        print(paint('\nIQ SCAN — candidate activity'))
        print(f'{format_time(meta["duration_s"])} | {meta["sample_rate"]:g} samples/s | {meta["format"]}')
        context=meta.get('spectrum_context',{})
        print('Band plan:',args.bandplan,'| frequency references only, not identification')
        for band in context.get('bands',[])[:8]:
            print(f"  {band['low_hz']/1e6:.6f}–{band['high_hz']/1e6:.6f} MHz: {band['name']}")
        if context.get('warning'): print(context['warning'])
        print(' ID  Type         Start–end (hh:mm:ss.mmm)      Offset kHz   Width Hz  Contrast')
        for e in events:
            print(f" {e['id']:2}  {e['kind']:<10} {format_time(e['start_s'])}–{format_time(e['end_s'])}  {e['center_offset_hz']/1000:+10.3f}  {e['bandwidth_hz']:9.0f}  {e['contrast_db']:6.1f} dB")
            bands,hints=brief(e)
            import textwrap
            if bands:print(textwrap.fill('    Band: '+bands,width=95,subsequent_indent='    '))
            if hints:print(textwrap.fill('    Possible frequency references: '+hints,width=95,subsequent_indent='    '))
        if not events:print('No candidates above these thresholds. This does not prove no signal was present.')
        print('Contrast is not calibrated SNR. Candidates may be interference or receiver artifacts.')
        print(paint(f'\nReport: {out / "report.html"}'))
        print('Open report: open '+shlex.quote(str(out/'report.html')))
        print(f'Clip commands: {out / "OPEN-CLIPS.txt"}')
        if args.open:
            import webbrowser
            try:
                if not webbrowser.open((out/'report.html').as_uri(),new=2):
                    print('Could not launch a browser; open the report path above.',file=sys.stderr)
            except (OSError,webbrowser.Error) as exc:
                print(f'Could not launch a browser: {exc}. Report saved successfully.',file=sys.stderr)
        return 0
    except (OSError,ValueError,ImportError) as exc:
        print('Error: '+str(exc),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
