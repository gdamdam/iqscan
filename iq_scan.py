#!/usr/bin/env python3
"""Find candidate activity in signed complex IQ recordings with optional protocol evidence."""
__version__ = '1.4.1'
import argparse, csv, html, json, math, os, re, shlex, sys, tempfile, zipfile
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


def nonnegative(value):
    x=float(value)
    if not math.isfinite(x) or x<0: raise argparse.ArgumentTypeError('Must be nonnegative and finite')
    return x


def parser():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.epilog='Decode Meteor images with: iqscan meteor RECORDING --satellite M2-4 --video'
    p.add_argument('file',type=Path,nargs='?')
    p.add_argument('--sample-rate',type=positive,help='Complex samples/sec; otherwise read NNNsps from filename')
    p.add_argument('--center-frequency',type=float,help='Hz; otherwise read NNNHz from filename; optional')
    p.add_argument('--format',choices=['cs8','cs16','cu8','cf32','cf32_le','cf32_be'],help='Otherwise infer raw, WAV, or SigMF format')
    p.add_argument('--iq-order', choices=['IQ','QI'], default='IQ', help='Interleaved component order')
    p.add_argument('--start', type=nonnegative, default=0, help='Start a fresh scan this many seconds into the source')
    p.add_argument('--duration', type=positive, help='Scan at most this many seconds; FFTs are recomputed for this interval')
    p.add_argument('--min-offset', type=float, help='Lowest detection offset from center, Hz')
    p.add_argument('--max-offset', type=float, help='Highest detection offset from center, Hz')
    p.add_argument('--source', type=Path, help='Relocated raw recording for --redetect; fingerprint must match')
    p.add_argument('--portable', action='store_true', help='Omit generated absolute local paths from the report (not a reusable cache)')
    p.add_argument('--channel-clips', type=int, default=0, help='Also export this many filtered, centered cf32 channel clips (bounded to 262144 output samples)')
    p.add_argument('--analysis-seconds', type=positive, default=2.0, help='Maximum signal-analysis source duration per event, up to 30 seconds')
    p.add_argument('--open',action='store_true',help='Open the completed HTML report in your default browser')
    p.add_argument('--output',type=Path,help='New output directory; existing directories are never overwritten')
    p.add_argument('--fft-size',type=int,default=4096,help='Power of two, 256..16384')
    p.add_argument('--time-bin',type=positive,default=.1,help='Requested time resolution in seconds')
    p.add_argument('--max-rows',type=int,default=2000,help='Bound analysis memory; long files get coarser time bins')
    p.add_argument('--threshold',type=positive,default=7,help='Transient excess above each frequency baseline, dB')
    p.add_argument('--min-duration',type=nonnegative,default=.15,help='Minimum transient duration in seconds')
    p.add_argument('--dc-exclude',type=float,default=2000,help='Ignore this many Hz either side of center in detection')
    p.add_argument('--top',type=int,default=20,help='Maximum events in report')
    p.add_argument('--version',action='version',version=f'iqscan {__version__}')
    p.add_argument('--serve',nargs='?',const=8731,type=int,metavar='PORT',help='Browse cached spectra and retune detection live in a local browser app')
    p.add_argument('--scan-root',type=Path,help='Directory of scan folders for --serve; default ./scans')
    p.add_argument('--save-spectrum',action='store_true',help='Also write spectrum.npz so --redetect can rerun detection without recomputing FFTs')
    p.add_argument('--redetect',type=Path,help='Reuse spectrum.npz from a previous scan directory instead of reading the recording again')
    p.add_argument('--clips',type=int,default=10,help='Export this many event clips; 0 disables')
    p.add_argument('--padding',type=float,default=1,help='Clip padding on each side, seconds')
    p.add_argument('--max-clip-seconds',type=positive,default=10,help='Cap each clip; long events are clipped around strongest time')
    p.add_argument('--analyze-signals',action='store_true',help='Inspect bounded raw IQ around each event for modulation and symbol-rate candidates')
    p.add_argument('--color',choices=['auto','always','never'],default='auto')
    from spectrum_refs import add_arguments
    add_arguments(p)
    return p


def validate_detection_args(args, sample_rate=None):
    """Validate values shared by CLI, cached redetection, and the explorer."""
    if not math.isfinite(args.threshold) or args.threshold<=0:
        raise ValueError('--threshold must be positive and finite')
    if not math.isfinite(args.min_duration) or args.min_duration<0:
        raise ValueError('--min-duration must be nonnegative and finite')
    if not math.isfinite(args.dc_exclude) or args.dc_exclude<0:
        raise ValueError('--dc-exclude must be nonnegative and finite')
    for key in ('min_offset', 'max_offset'):
        value = getattr(args, key, None)
        if value is not None and not math.isfinite(value):
            raise ValueError('Frequency bounds must be finite')
    lo, hi = getattr(args, 'min_offset', None), getattr(args, 'max_offset', None)
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError('--min-offset must be below --max-offset')
    if sample_rate is not None and any(v is not None and abs(v) > sample_rate / 2 for v in (lo, hi)):
        raise ValueError('Frequency bounds must lie inside the sampled band')
    if args.top<1:
        raise ValueError('--top must be at least 1')
    if sample_rate is not None and args.dc_exclude>=sample_rate*.45:
        raise ValueError('--dc-exclude must be below 45% of sample rate')


def validate_args(args, sample_rate=None):
    validate_detection_args(args, sample_rate)
    if args.analysis_seconds > 30:
        raise ValueError('--analysis-seconds must be at most 30')
    if args.channel_clips < 0:
        raise ValueError('--channel-clips must be nonnegative')
    if args.source and not args.redetect:
        raise ValueError('--source requires --redetect')
    if args.redetect and (args.start or args.duration):
        raise ValueError('Time-region selection requires a fresh scan, not --redetect')
    if args.portable and args.save_spectrum:
        raise ValueError('--portable cannot be combined with --save-spectrum')
    if not math.isfinite(args.time_bin) or args.time_bin<=0:
        raise ValueError('--time-bin must be positive and finite')
    if not 10<=args.max_rows<=10000:
        raise ValueError('--max-rows must be 10..10000')
    if args.fft_size<256 or args.fft_size>16384 or args.fft_size&(args.fft_size-1):
        raise ValueError('--fft-size must be a power of two from 256 to 16384')
    if args.clips<0:
        raise ValueError('--clips must be nonnegative')
    if not math.isfinite(args.padding) or args.padding<0:
        raise ValueError('--padding must be nonnegative and finite')
    if not math.isfinite(args.max_clip_seconds) or args.max_clip_seconds<=0:
        raise ValueError('--max-clip-seconds must be positive and finite')


def metadata(args):
    from iq_input import read_metadata
    meta = read_metadata(args.file, format=args.format, sample_rate=args.sample_rate,
                         center_frequency=args.center_frequency, iq_order=args.iq_order)
    validate_args(args, meta['sample_rate'])
    first = int(args.start * meta['sample_rate'])
    if first >= meta['samples']:
        raise ValueError('--start is beyond the recording')
    count = meta['samples'] - first
    if args.duration is not None:
        count = min(count, int(args.duration * meta['sample_rate']))
    meta.update(source_duration_s=meta['duration_s'], scan_start_s=first / meta['sample_rate'])
    meta['data_offset'] += first * meta['bytes_per_complex']
    meta.update(samples=count, bytes=count * meta['bytes_per_complex'],
                duration_s=count / meta['sample_rate'])
    if count < args.fft_size:
        raise ValueError('Recording is shorter than one FFT; reduce --fft-size')
    return meta


def spectrum(meta,args):
    import numpy as np
    from iq_input import read_samples
    n=args.fft_size; fs=meta['sample_rate']; frames=math.ceil(meta['samples']/n)
    navg=max(1,round(args.time_bin*fs/n),math.ceil(frames/args.max_rows))
    rows=math.ceil(frames/navg); w=np.hanning(n)
    scale = {'cs8':128, 'cu8':128, 'cs16':32768}.get(meta['format'])
    ps=np.zeros((rows,n),dtype=np.float32); rail=0; total=0
    for row in range(rows):
        first=row*navg; count=min(navg,frames-first)
        for k in range(first,first+count,64):
            nf=min(64,first+count-k)
            z=read_samples(meta,k*n,min(nf*n,meta['samples']-k*n))
            if scale:
                for component in (z.real,z.imag):
                    rail += int(np.count_nonzero((component <= -1) | (component >= (scale-1)/scale)))
            total += z.size * 2
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


def validate_cache_metadata(meta, shaping, dt):
    """Reject malformed metadata before detection, rendering, or clip extraction."""
    if not isinstance(meta, dict) or not isinstance(shaping, dict):
        raise ValueError('Invalid spectrum cache metadata')
    def finite_positive(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    required = ('input', 'format', 'center_frequency_hz', 'rows', 'samples', 'bytes',
                'bytes_per_complex', 'sample_rate', 'duration_s', 'frequency_bin_hz')
    if any(key not in meta for key in required):
        raise ValueError('Incomplete spectrum cache metadata')
    if not isinstance(meta['input'], str) or not meta['input'] or meta['format'] not in ('cs8', 'cs16', 'cu8', 'cf32_le', 'cf32_be'):
        raise ValueError('Invalid spectrum cache input metadata')
    for key in ('sample_rate', 'duration_s', 'frequency_bin_hz'):
        if not finite_positive(meta[key]): raise ValueError(f'Invalid cached {key}')
    for key in ('rows', 'samples', 'bytes', 'bytes_per_complex'):
        if type(meta[key]) is not int or meta[key] <= 0: raise ValueError(f'Invalid cached {key}')
    center = meta['center_frequency_hz']
    if center is not None and (isinstance(center,bool) or not isinstance(center,(int,float)) or not math.isfinite(center)):
        raise ValueError('Invalid cached center frequency')
    offset = meta.get('data_offset', 0)
    if type(offset) is not int or offset < 0: raise ValueError('Invalid cached data offset')
    bpc = {'cs8':2, 'cu8':2, 'cs16':4, 'cf32_le':8, 'cf32_be':8}[meta['format']]
    if meta.get('iq_order', 'IQ') not in ('IQ','QI'):
        raise ValueError('Invalid cached I/Q order')
    if meta.get('cache_schema_version', 1) not in (1,2):
        raise ValueError('Unsupported spectrum cache schema')
    if not math.isfinite(meta.get('scan_start_s',0)) or meta.get('scan_start_s',0) < 0:
        raise ValueError('Invalid cached source start')
    if meta['bytes_per_complex'] != bpc or meta['bytes'] != meta['samples'] * bpc:
        raise ValueError('Inconsistent cached sample sizes')
    if not math.isclose(meta['duration_s'], meta['samples'] / meta['sample_rate']):
        raise ValueError('Inconsistent cached duration')
    if not finite_positive(dt) or not (meta['rows'] - 1) * dt < meta['duration_s'] <= meta['rows'] * dt:
        raise ValueError('Invalid cached time bins')
    n = shaping.get('fft_size')
    max_rows = shaping.get('max_rows')
    if type(n) is not int or not 256 <= n <= 16384 or n & (n - 1):
        raise ValueError('Invalid cached FFT size')
    if type(max_rows) is not int or not 10 <= max_rows <= 10000 or not finite_positive(shaping.get('time_bin')):
        raise ValueError('Invalid cached FFT shaping')
    if not math.isclose(meta['frequency_bin_hz'], meta['sample_rate'] / n):
        raise ValueError('Inconsistent cached frequency bins')


def cache_summary(directory):
    """Read only small metadata entries; matrices are validated when selected."""
    import numpy as np
    path = Path(directory).expanduser().resolve() / 'spectrum.npz'
    try:
        with np.load(path, allow_pickle=False) as data:
            if not {'meta', 'spectrum_args', 'f', 'norm', 'reference', 'dt'} <= set(data.files):
                raise ValueError('Incomplete spectrum cache')
            meta = json.loads(str(data['meta']))
            shaping = json.loads(str(data['spectrum_args']))
            dt = float(data['dt'])
        validate_cache_metadata(meta, shaping, dt)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError(f'Invalid spectrum cache: {path}: {exc}') from exc
    return meta, shaping


def load_spectrum(directory):
    import numpy as np
    path = Path(directory).expanduser().resolve() / 'spectrum.npz'
    if not path.exists():
        raise ValueError(f'No spectrum.npz in {directory}. Rerun that scan with --save-spectrum.')
    try:
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data['meta']))
            shaping = json.loads(str(data['spectrum_args']))
            dt = float(data['dt'])
            validate_cache_metadata(meta, shaping, dt)
            f, norm, reference = data['f'], data['norm'], data['reference']
        if (f.shape != (shaping['fft_size'],) or norm.shape != (meta['rows'], len(f))
                or reference.shape != (meta['rows'],)
                or any(a.dtype.kind not in 'fiu' or not np.isfinite(a).all() for a in (f, norm, reference))):
            raise ValueError('Invalid spectrum cache arrays')
        expected = np.fft.fftshift(np.fft.fftfreq(len(f), 1 / meta['sample_rate']))
        if not np.allclose(f, expected): raise ValueError('Invalid cached frequency axis')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError(f'Invalid spectrum cache: {path}: {exc}') from exc
    return meta, f, norm, reference, dt, shaping


def time_edges(duration, dt, rows):
    """Return analysis-bin edges, shortening the final edge to recording duration."""
    return [min(i*dt, duration) for i in range(rows+1)]


def detect(meta,args,f,norm,dt):
    import numpy as np
    from scipy.ndimage import label,find_objects,median_filter
    validate_detection_args(args, meta['sample_rate'])
    baseline=np.median(norm,axis=0); excess=norm-baseline[None,:]
    valid=(abs(f)>args.dc_exclude)&(abs(f)<meta['sample_rate']*.45)
    if getattr(args, 'min_offset', None) is not None:
        valid &= f >= args.min_offset
    if getattr(args, 'max_offset', None) is not None:
        valid &= f <= args.max_offset
    mask=(excess>=args.threshold)&valid[None,:]
    labs,_=label(mask);events=[];df=meta['frequency_bin_hz']
    edges=time_edges(meta['duration_s'],dt,len(norm))
    def event(kind,ti,fi,peak,power,pixels):
        start=edges[ti.start];end=edges[min(ti.stop,len(norm))]
        peak_start=edges[peak];peak_time=peak_start+(edges[peak+1]-peak_start)/2
        return dict(kind=kind,start_s=round(start,6),end_s=round(end,6),duration_s=round(end-start,6),
            low_offset_hz=round(float(f[fi.start]-df/2),3),high_offset_hz=round(float(f[fi.stop-1]+df/2),3),
            center_offset_hz=round(float((f[fi.start]+f[fi.stop-1])/2),3),
            bandwidth_hz=round((fi.stop-fi.start)*df,3),peak_time_s=round(peak_time,6),
            contrast_db=round(float(power),2),pixels=int(pixels))
    for i,sl in enumerate(find_objects(labs),1):
        if sl is None:continue
        ti,fi=sl;region=labs[sl]==i;pixels=int(region.sum())
        if pixels<4 or edges[min(ti.stop,len(norm))]-edges[ti.start]<args.min_duration:continue
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
            e.update(clip=f'clips/{name}',clip_start_original_s=meta.get('scan_start_s',0)+first/meta['sample_rate'],clip_duration_s=(last-first)/meta['sample_rate'],
                     event_start_in_clip_s=max(0,e['start_s']-first/meta['sample_rate']))
            from iq_input import write_sigmf
            if meta.get('iq_order', 'IQ') == 'IQ':
                datatype = {'cs8':'ci8','cs16':'ci16_le','cu8':'cu8','cf32_le':'cf32_le','cf32_be':'cf32_be'}[meta['format']]
                event_first = max(0, int(e['start_s'] * meta['sample_rate']) - first)
                event_last = min(last-first, math.ceil(e['end_s'] * meta['sample_rate']) - first)
                annotation = {'core:sample_start':event_first, 'core:sample_count':max(0,event_last-event_first),
                              'core:label':f"Event {e['id']}: candidate {e.get('kind','activity')}",
                              'core:comment':'Activity detector candidate; not a transmitter identification'}
                if meta['center_frequency_hz'] is not None and 'low_offset_hz' in e and 'high_offset_hz' in e:
                    annotation['core:freq_lower_edge'] = meta['center_frequency_hz'] + e['low_offset_hz']
                    annotation['core:freq_upper_edge'] = meta['center_frequency_hz'] + e['high_offset_hz']
                sidecar = write_sigmf(path,meta['sample_rate'],meta['center_frequency_hz'],datatype, annotations=[annotation])
                e['clip_metadata'] = 'clips/' + sidecar.name
            else:
                sidecar = path
                e['clip_warning'] = 'QI component order preserved; configure the external viewer accordingly'
            e['open_command']='inspectrum '+shlex.quote(str(sidecar.resolve()))


def signal_analysis_brief(event):
    """Compact, human-readable summary for terminal and HTML reports."""
    analysis=event.get('signal_analysis')
    if not isinstance(analysis,dict):
        return 'Not analyzed (use --analyze-signals)'
    status=str(analysis.get('status') or 'unknown')
    parts=[]
    for candidate in analysis.get('candidates') or []:
        if not isinstance(candidate,dict):
            continue
        modulation=str(candidate.get('modulation') or 'unknown')
        confidence=str(candidate.get('confidence') or 'low')
        evidence=[str(item) for item in candidate.get('evidence') or [] if item]
        label=f'{modulation} ({confidence})'
        if evidence:
            label+=': '+'; '.join(evidence[:2])
        parts.append(label)
    rate=analysis.get('symbol_rate_baud')
    if isinstance(rate,(int,float)) and math.isfinite(rate):
        parts.append(f'{rate:g} baud')
    protocol=analysis.get('protocol')
    if isinstance(protocol,dict) and protocol.get('name'):
        label=str(protocol['name'])
        if protocol.get('status'):
            label+=f' ({protocol["status"]})'
        evidence=[str(item) for item in protocol.get('evidence') or [] if item]
        if evidence:
            label+=': '+evidence[0]
        parts.append(label)
    warnings=[str(item) for item in analysis.get('warnings') or [] if item]
    bounded='long event analyzed in bounded windows'
    if bounded in warnings:
        warnings.remove(bounded)
        parts.append('note: long event sampled in short analysis windows')
    if warnings:
        parts.append('warning: '+warnings[0])
    if not parts:
        parts.append(status)
    return ', '.join(parts)


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
    center=meta['center_frequency_hz'];freq=(f+(center or 0))/(1e6 if center is not None else 1e3)
    unit='Frequency (MHz)' if center is not None else 'Offset (kHz)'
    fig,(ax,bx)=plt.subplots(2,1,figsize=(13,9),layout='constrained',gridspec_kw={'height_ratios':[4,1]})
    im=ax.imshow(norm,extent=(freq[0],freq[-1],len(norm)*dt,0),aspect='auto',cmap='magma',vmin=-3,vmax=max(12,float(np.percentile(norm,99.5))))
    ax.set_ylim(meta['duration_s'],0)
    ax.set(xlabel=unit,ylabel='Elapsed time (hh:mm:ss.mmm)',title='Candidate activity — broadband level changes removed')
    ax.yaxis.set_major_formatter(time_ticks)
    ax.ticklabel_format(useOffset=False,axis='x')
    for e in events:
        x=((center or 0)+e['center_offset_hz'])/(1e6 if center is not None else 1e3)
        ax.annotate(str(e['id']),(x,e['peak_time_s']),xytext=(5,0),textcoords='offset points',color='cyan',fontsize=9,bbox=dict(facecolor='black',alpha=.6,edgecolor='none'))
    fig.colorbar(im,ax=ax,label='dB above per-time reference band')
    edges=time_edges(meta['duration_s'],dt,len(reference))
    bx.plot((np.asarray(edges[:-1])+np.asarray(edges[1:]))/2,reference,lw=.8)
    bx.xaxis.set_major_formatter(time_ticks)
    bx.set(xlabel='Elapsed time (hh:mm:ss.mmm)',ylabel='Reference PSD\n(digital dB/Hz)',title='Broadband level: gain changes and interference can affect this trace');fig.savefig(out/'waterfall.png',dpi=140)
    overview=panel(fig,ax,'waterfall',norm)
    overview.update(time_origin_s=0,time_bin_s=dt)
    plots['waterfall.png']=[overview,panel(fig,bx,'trace')]
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
        im=ax.imshow(section,extent=(freq[fi][0],freq[fi][-1],ib*dt,ia*dt),aspect='auto',cmap='magma',vmin=-3,vmax=max(12,float(np.percentile(section,99.5))))
        ax.set_ylim(min(ib*dt,meta['duration_s']),ia*dt)
        ax.set(xlabel=unit,ylabel='Elapsed time in scanned interval (hh:mm:ss.mmm)',title=f"Event {e['id']} | {e['kind']} | unidentified activity")
        ax.yaxis.set_major_formatter(time_ticks)
        ax.ticklabel_format(useOffset=False,axis='x')
        fig.colorbar(im,ax=ax,label='dB above per-time reference band')
        name=f"images/event-{e['id']:02d}.png";fig.savefig(out/name,dpi=150)
        event_panel=panel(fig,ax,'waterfall',section)
        event_panel.update(time_origin_s=ia*dt,time_bin_s=dt)
        plots[name]=[event_panel]
        plt.close(fig);e['image']=name
    for e in events:
        for key in ('start_s','end_s','duration_s','peak_time_s','clip_start_original_s','clip_duration_s','event_start_in_clip_s'):
            if key in e: e[key[:-2]+'_hms'] = format_time(e[key])
    meta['duration_hms'] = format_time(meta['duration_s'])
    (out/'events.json').write_text(json.dumps(dict(metadata=meta,settings=vars_serial(args),events=events),indent=2)+'\n')
    fields=list(dict.fromkeys(k for e in events for k in e)) or ['id','kind','start_s','end_s','frequency_hz']
    with (out/'events.csv').open('w',newline='') as fp:
        writer=csv.DictWriter(fp,fieldnames=fields);writer.writeheader();writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in e.items()} for e in sorted(events,key=lambda e:(e['start_s'],e['id'])))
    table=[]
    for e in events:
        frequency=f"{e['frequency_hz']/1e6:.6f} MHz" if center is not None else f"{e['center_offset_hz']/1000:+.3f} kHz"
        link=f'<a href="{html.escape(e["clip"])}">IQ clip</a>' if 'clip' in e else ''
        bands,hints=brief(e)
        context_text=html.escape(bands or 'No band-plan match')+'<br><small>'+html.escape(hints or 'No frequency reference in selected catalogs')+'</small>'
        if e.get('channel_clip'):
            link += '<br><a href="'+html.escape(e['channel_clip'])+'">Filtered channel</a>'
        analysis_text=html.escape(signal_analysis_brief(e))
        table.append(f'<tr><td>{e["id"]}</td><td>{e["kind"]}</td><td>{format_time(e["start_s"])}–{format_time(e["end_s"])}</td><td>{frequency}</td><td>{e["bandwidth_hz"]:.0f}</td><td>{e["contrast_db"]:.1f}</td><td>{link}</td><td>{context_text}</td><td>{analysis_text}</td></tr>')
    note='Candidates only: no transmitter identification. Optional protocol results are limited to supported decoder checks and remain unconfirmed otherwise. Transient contrast is relative to that frequency’s usual level; persistent contrast is relative to nearby frequencies. Values are not calibrated SNR. Stationary spurs, gain changes, and interference can trigger detections. Weak, broad, or continuous signals may be missed.'
    gallery=''.join(f'<h2>Event {e["id"]}</h2><img loading="lazy" src="{e["image"]}" alt="Event {e["id"]} close-up">' for e in events if 'image' in e)
    context_html=html_context(meta)
    if (out/'spectrum-context.png').exists(): context_html+='<img src="spectrum-context.png" alt="Band and known-signal reference chart">'
    page=f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>IQ scan</title><style>body{{background:#10151e;color:#e5edf8;font:16px system-ui;max-width:1200px;margin:30px auto;padding:20px}}a{{color:#79dfff}}img{{width:100%}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #354050}}.scroll{{overflow-x:auto}}code{{overflow-wrap:anywhere}}</style><h1>IQ recording scan</h1><p>{html.escape(Path(meta['input']).name)}</p><p>{format_time(meta['duration_s'])} duration · {meta['sample_rate']:g} samples/s · {meta['format']} · {len(events)} reported candidates</p><p>Resolution: {format_time(dt)} × {meta['frequency_bin_hz']:.1f} Hz. Center: {center if center is not None else 'unknown; offsets only'} Hz.</p><img src="waterfall.png" alt="Annotated waterfall and broadband level"><div class="scroll"><table><tr><th>ID</th><th>Type</th><th>Elapsed time (hh:mm:ss.mmm)</th><th>Frequency</th><th>Detected width (Hz)</th><th>Contrast (dB)</th><th>Clip</th><th>Frequency references (not identification)</th><th>Signal analysis (candidate evidence)</th></tr>{''.join(table)}</table></div><p>{note}</p><p>Signal analysis is an optional bounded raw-IQ heuristic. Candidate modulation and symbol rates are evidence for review, not general protocol identification. Where present, confirmed protocol evidence comes from a supported decoder check. A missing raw recording leaves analysis unknown.</p><p>Clips preserve the original format, sample rate and full bandwidth. Use sample rate {meta['sample_rate']:g} in inspectrum. Clip time starts at zero. See events.json for original start times and open commands.</p>{context_html}{gallery}'''
    if meta.get('input_warning'):page=page.replace('<h1>IQ recording scan</h1>','<h1>IQ recording scan</h1><p><strong>Input warning:</strong> '+html.escape(meta['input_warning'])+'</p>')
    if meta.get('scan_start_s'):
        page += '<p>All event and plot times are relative to the selected interval. Source interval starts at '+format_time(meta['scan_start_s'])+'. Clip original-start fields include this offset.</p>'
    if meta.get('rail_fraction',0) >= .001:
        page += f"<p><strong>Clipping warning:</strong> {100*meta['rail_fraction']:.2f}% of integer I/Q components hit a digital rail. Overload may distort detections and modulation evidence.</p>"
    if meta.get('source_warning'):
        page += '<p><strong>Source verification:</strong> '+html.escape(meta['source_warning'])+'</p>'
    page=inject(page,plots,events,meta.get('spectrum_context',{}).get('bands',[]),center)
    (out/'report.html').write_text(page)
    instructions=['Open clips with inspectrum; set sample rate to '+str(meta['sample_rate'])+'.','Frequency offsets are relative to '+str(center)+' Hz.','Clips are exact excerpts, not filtered or frequency-shifted.','']
    for e in events:
        if 'clip' in e:instructions.extend([f"Event {e['id']}: original start {format_time(e['clip_start_original_s'])}; event begins {format_time(e['event_start_in_clip_s'])} into clip; offset {e['center_offset_hz']/1000:+.3f} kHz.",e['open_command'],''])
    (out/'OPEN-CLIPS.txt').write_text('\n'.join(instructions))


def vars_serial(args):return {k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}


def source_state(path):
    """Ignore access-time changes caused by reading the recording itself."""
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'meteor':
        from meteor_extract import main as meteor_main
        return meteor_main(argv[1:])
    args=parser().parse_args(argv)
    try:
        from spectrum_refs import load as load_references, describe, brief
        if args.update_references:
            catalog=load_references(args)
            for source in catalog['sources']:
                print(f"{source['catalog']}: {source['records']} records | {source.get('downloaded_utc','local file')}")
            print('Reference cache:',args.reference_cache)
            return 2 if catalog['warnings'] else 0
        if args.serve is not None:
            import iq_serve
            return iq_serve.serve(args.scan_root or Path.cwd()/'scans',args.serve,open_browser=args.open, overrides={k:getattr(args,k) for k in iq_serve.DETECT_ARGS if any(token == '--'+k.replace('_','-') or token.startswith('--'+k.replace('_','-')+'=') for token in (argv if argv is not None else sys.argv[1:]))})
        if args.file is None and args.redetect is None: raise ValueError('Provide an IQ recording, --redetect DIR, or --update-references')
        if args.file and args.redetect:
            raise ValueError('Use --source to relocate a recording with --redetect')
        cached=load_spectrum(args.redetect) if args.redetect is not None else None
        meta=cached[0] if cached else metadata(args)
        if cached:
            for key in SPECTRUM_ARGS:
                setattr(args,key,cached[5][key])
        validate_args(args, meta['sample_rate'])
        out=(args.output or Path.cwd()/'scans'/(Path(meta['input']).stem+'_'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))).expanduser().resolve()
        if out.exists():raise ValueError(f'Output already exists; choose a new directory: {out}')
        catalog=load_references(args) if meta['center_frequency_hz'] is not None else dict(bands=[],signals=[],sources=[],warnings=[],region=args.bandplan)
        import numpy, scipy
        from iq_input import fingerprint, verify_source, relocated_source
        from iq_export import export_channels, portable_report
        source_ok = True
        raw_required = bool(args.clips or args.channel_clips or args.analyze_signals or args.source)
        if cached and not raw_required:
            source_ok = False
        elif cached:
            try:
                source_ok = verify_source(meta, args.source)
            except (OSError, ValueError):
                source_ok = False
            if args.source and not source_ok:
                raise ValueError('--source does not match the cached recording fingerprint and metadata')
            if source_ok and args.source:
                meta.update(relocated_source(meta, args.source))
            if source_ok:
                meta.pop('source_warning', None)
            if not source_ok:
                meta['source_warning'] = 'Raw source is missing, changed, or has no verifiable fingerprint. Clips and waveform analysis were skipped; cached detection remains available.'
                print('WARNING: '+meta['source_warning'], file=sys.stderr)
        else:
            before = source_state(meta['input'])
            meta['source_fingerprint'] = fingerprint(meta)
            if source_state(meta['input']) != before:
                raise ValueError('Source changed during fingerprinting; stop the recording and retry')
        source_stat = source_state(meta['input']) if source_ok else None
        meta.update(tool_version=__version__,cache_schema_version=2,
                    detection_args={key:getattr(args,key) for key in ('threshold','min_duration','dc_exclude','top','min_offset','max_offset')})
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
            if source_ok and source_state(meta['input']) != source_stat:
                raise ValueError('Source changed during scanning; stop the recording and retry')
            if args.analyze_signals and source_ok:
                from signal_analysis import analyze_events
                # Analysis is deliberately opt-in and bounded. The analyzer records
                # unknown status when a cached redetect has no accessible raw input.
                analyze_events(meta,events,max_samples=262144,duration_seconds=args.analysis_seconds)
            elif args.analyze_signals:
                from signal_analysis import _empty
                for event in events:
                    event['signal_analysis'] = _empty([meta['source_warning']])
            describe(meta,events,catalog,args)
            out.mkdir(parents=True)
            if source_ok:
                clips(meta,args,events,out)
                export_channels(meta,args,events,out)
            if source_ok and Path(meta['input']).is_absolute() and source_state(meta['input']) != source_stat:
                raise ValueError('Source changed while exporting; discard this incomplete output and retry')
            if args.portable:
                meta,args,events = portable_report(meta,args,events)
            report(meta,args,events,f,norm,reference,dt,out)
            if args.save_spectrum: save_spectrum(meta,spectrum_args,f,norm,reference,dt,out)
        colored=args.color=='always' or args.color=='auto' and sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM')!='dumb'
        paint=lambda s:f'\033[96m{s}\033[0m' if colored else s
        print(paint('\nIQ SCAN — candidate activity'))
        if meta.get('scan_start_s'):
            print('Times are relative to the scan interval; original source start: '+format_time(meta['scan_start_s']))
        if meta.get('rail_fraction',0) >= .001:
            print(f"WARNING: {100*meta['rail_fraction']:.2f}% of integer I/Q components hit digital rails.")
        print(f'{format_time(meta["duration_s"])} | {meta["sample_rate"]:g} samples/s | {meta["format"]}')
        context=meta.get('spectrum_context',{})
        print('Band plan:',args.bandplan,'| frequency references only, not identification')
        for band in context.get('bands',[])[:8]:
            print(f"  {band['low_hz']/1e6:.6f}–{band['high_hz']/1e6:.6f} MHz: {band['name']}")
        if context.get('warning'): print(context['warning'])
        print(' ID  Type         Start–end (hh:mm:ss.mmm)      Offset kHz   Width Hz  Contrast  Signal analysis')
        for e in events:
            print(f" {e['id']:2}  {e['kind']:<10} {format_time(e['start_s'])}–{format_time(e['end_s'])}  {e['center_offset_hz']/1000:+10.3f}  {e['bandwidth_hz']:9.0f}  {e['contrast_db']:6.1f} dB")
            if args.analyze_signals:
                print('      Signal analysis: '+signal_analysis_brief(e))
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
