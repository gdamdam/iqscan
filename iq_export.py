"""Bounded channel exports and portable report metadata."""
import copy
import json
import math
import shlex
from argparse import Namespace
from pathlib import Path


def export_channels(meta, args, events, out):
    """Export derived IQ with explicit processing provenance; original clips stay exact."""
    if not args.channel_clips:
        return
    import numpy as np
    from signal_analysis import _stream_channelize
    from iq_input import write_sigmf
    folder = out / 'channels'
    folder.mkdir()
    fs = meta['sample_rate']
    for event in events[:args.channel_clips]:
        start = max(0, event['start_s'] - args.padding)
        end = min(meta['duration_s'], event['end_s'] + args.padding)
        if end - start > args.max_clip_seconds:
            start = max(start, event['peak_time_s'] - args.max_clip_seconds / 2)
            end = min(meta['duration_s'], start + args.max_clip_seconds)
        first = int(start * fs)
        count = min(meta['samples'] - first, math.ceil(end * fs) - first)
        samples, rate, warnings = _stream_channelize(
            meta, first, count, event['center_offset_hz'],
            max(12000, event['bandwidth_hz']), max_output_samples=262144)
        name = f"event-{event['id']:02d}_{rate:g}SPS.cf32"
        path = folder / name
        pairs = np.column_stack((samples.real, samples.imag)).astype('<f4')
        pairs.tofile(path)
        center = meta['center_frequency_hz']
        center = None if center is None else center + event['center_offset_hz']
        provenance = dict(format='cf32_le', iq_order='IQ', sample_rate=rate,
                          center_frequency_hz=center, samples=len(samples),
                          duration_s=len(samples)/rate,
                          source_start_s=meta.get('scan_start_s',0)+first/fs,
                          requested_source_duration_s=count/fs,
                          frequency_shift_hz=-event['center_offset_hz'],
                          processing='Frequency translation, FIR low-pass filtering and decimation; not bit-exact',
                          warnings=warnings)
        provenance['processed_source_duration_s'] = min(count / fs, len(samples) / rate)
        provenance['source_end_s'] = provenance['source_start_s'] + provenance['processed_source_duration_s']
        annotation = {'core:sample_start':0, 'core:sample_count':len(samples),
                      'core:label':f"Event {event['id']}: derived channel",
                      'core:comment':provenance['processing']}
        sidecar = write_sigmf(path, rate, center, 'cf32_le', annotations=[annotation])
        event['channel_sigmf'] = 'channels/' + sidecar.name
        path.with_suffix('.json').write_text(json.dumps(provenance, indent=2)+'\n')
        event['channel_clip'] = f'channels/{name}'
        event['channel_metadata'] = provenance


def portable_report(meta, args, events):
    """Remove generated local paths, retaining relative links and human filenames."""
    meta, events = copy.deepcopy(meta), copy.deepcopy(events)
    settings = vars(args).copy()
    paths = {str(value):value.name for value in settings.values() if isinstance(value,Path) and value.is_absolute()}
    for key in ('input','redetected_from','sigmf_meta'):
        if meta.get(key):
            paths[str(meta[key])] = Path(meta[key]).name
    def redact(value):
        if isinstance(value,dict):
            return {key:redact(item) for key,item in value.items()}
        if isinstance(value,list):
            return [redact(item) for item in value]
        if isinstance(value,str):
            for path,name in sorted(paths.items(),key=lambda item:len(item[0]),reverse=True):
                value=value.replace(path,name)
        return value
    meta,events = redact(meta),redact(events)
    for key, value in settings.items():
        if isinstance(value, Path):
            settings[key] = Path(value.name)
    for key in ('input', 'redetected_from', 'metadata_file', 'sigmf_metadata', 'sigmf_meta'):
        if meta.get(key):
            meta[key] = Path(meta[key]).name
    # Fingerprints intentionally retain identity but contain no source paths.
    for event in events:
        if event.get('clip'):
            event['open_command'] = 'inspectrum ' + shlex.quote(event.get('clip_metadata', event['clip']))
    meta['portable_report'] = True
    return meta, Namespace(**settings), events
