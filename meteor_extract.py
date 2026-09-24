"""Decode Meteor LRPT images and instrument data from a recorded CS16 IQ pass."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from iq_input import read_metadata


SATDUMP_APP = Path('/Applications/_RADIO/SatDump.app/Contents/MacOS/satdump')
SATELLITES = ('M2-3', 'M2-4')


def parser():
    p = argparse.ArgumentParser(
        prog='iqscan meteor', description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('recording', type=Path, help='Stopped, interleaved IQ-order CS16 recording')
    p.add_argument('--satellite', required=True, choices=SATELLITES)
    p.add_argument('--frequency', type=float, default=137_900_000,
                   help='LRPT carrier in Hz; choose the frequency actually seen in the waterfall')
    p.add_argument('--sample-rate', type=float, help='Required if absent from filename/metadata')
    p.add_argument('--center-frequency', type=float, help='Required if absent from filename/metadata')
    p.add_argument('--output', type=Path, help='New result directory; default: ./scans/<recording>_meteor_<time>')
    p.add_argument('--satdump', type=Path, help='SatDump CLI executable; otherwise find PATH or macOS app')
    p.add_argument('--satdump-cli', choices=('auto', 'stable', 'v2'), default='auto',
                   help='Select stable 1.x or 2.x CLI syntax; auto probes --help')
    p.add_argument('--video', action='store_true', help='Also render synchronized waterfall, spectrum, and predicted sky track')
    p.add_argument('--video-seconds', type=float, default=60, help='Length of compressed video in seconds')
    p.add_argument('--location-config', type=Path,
                   help='nextpass location.json; otherwise RADIO_LOCATION_CONFIG or ~/.config/radio/location.json')
    p.add_argument('--elements-file', type=Path,
                   help='CelesTrak OMM JSON instead of the nextpass orbital-element cache')
    p.add_argument('--recording-start',
                   help='UTC ISO-8601 recording start if absent from SatDump-style filename')
    p.add_argument('--dry-run', action='store_true', help='Validate and print decoder command; create no files')
    return p


def satdump_binary(value):
    if value:
        path = value.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f'SatDump executable not found: {path}')
        return path
    found = shutil.which('satdump')
    if found:
        return Path(found).resolve()
    if SATDUMP_APP.is_file():
        return SATDUMP_APP
    raise ValueError('SatDump CLI not found; install it or pass --satdump PATH')


def cli_style(binary, selected):
    if selected != 'auto':
        return selected
    try:
        probe = subprocess.run([str(binary), '--help'], capture_output=True,
                               text=True, errors='replace', timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'Cannot probe SatDump version; pass --satdump-cli stable or v2: {exc}') from exc
    output = probe.stdout + probe.stderr
    if '2.0.0' in output or 'SUBCOMMANDS:' in output:
        return 'v2'
    if '1.2.' in output or 'Usage : satdump [' in output:
        return 'stable'
    raise ValueError('Unknown SatDump CLI syntax; pass --satdump-cli stable or v2')


def prepare(args):
    if not math.isfinite(args.frequency) or args.frequency <= 0:
        raise ValueError('--frequency must be positive and finite')
    if not math.isfinite(args.video_seconds) or not 1 <= args.video_seconds <= 300:
        raise ValueError('--video-seconds must be 1..300')
    meta = read_metadata(args.recording, sample_rate=args.sample_rate,
                         center_frequency=args.center_frequency)
    if meta['format'] != 'cs16' or meta['container'] not in ('raw', 'SigMF'):
        raise ValueError('Meteor extraction requires raw interleaved little-endian CS16 IQ')
    rate, center = meta['sample_rate'], meta['center_frequency_hz']
    if center is None:
        raise ValueError('Center frequency missing; pass --center-frequency HZ')
    if abs(args.frequency - center) >= rate / 2:
        raise ValueError('Requested LRPT carrier is outside the recorded IQ bandwidth')
    if rate != int(rate):
        raise ValueError('SatDump requires an integer sample rate')
    binary = satdump_binary(args.satdump)
    style = cli_style(binary, args.satdump_cli)
    out = (args.output or Path.cwd() / 'scans' /
           f"{Path(meta['input']).stem}_meteor_{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}").expanduser().resolve()
    work = out / 'satdump'
    command = [str(binary)] + (['pipeline'] if style == 'v2' else []) + ['meteor_m2-x_lrpt', 'baseband',
               meta['input'], str(work), f'--samplerate={int(rate)}',
               '--baseband_format=cs16', f'--freq_shift={round(center - args.frequency)}',
               '--dc_block=true', f'--satellite_number={args.satellite}']
    return meta, out, work, command


def products(work, image_dir):
    """Copy raw channel PNGs for convenience; keep all SatDump products intact."""
    paths = sorted(work.rglob('MSU-MR-*.png'))
    images = []
    for source in paths:
        image_dir.mkdir(exist_ok=True)
        destination = image_dir / source.name
        if destination.exists():
            raise ValueError(f'Duplicate image channel: {destination.name}')
        shutil.copy2(source, destination)
        images.append(str(destination.relative_to(image_dir.parent)))
    data = []
    for source in sorted(work.rglob('*')):
        if source.is_file() and source.suffix.lower() in ('.json', '.cbor', '.cadu'):
            data.append({'path': str(source.relative_to(image_dir.parent)), 'bytes': source.stat().st_size})
    return images, data


def data_summary(work):
    summary = {}
    frames = work / 'meteor_m2-x_lrpt.cadu'
    if frames.is_file():
        size = frames.stat().st_size
        summary['recovered_cadu_frames'] = size // 1024 if size % 1024 == 0 else None
    telemetry = work / 'telemetry.json'
    if telemetry.is_file():
        try:
            records = json.loads(telemetry.read_text())
            if isinstance(records, list):
                summary['telemetry_records'] = len(records)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    return summary


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        meta, out, work, command = prepare(args)
        if out.exists():
            raise ValueError(f'Output already exists; choose a new directory: {out}')
        print('Recording:', meta['input'])
        print(f"Duration: {meta['duration_s']:.1f} s; target: {args.frequency / 1e6:.3f} MHz; output: {out}")
        print('Decoder command:', ' '.join(command))
        if args.dry_run:
            return 0
        out.mkdir(parents=True)
        work.mkdir()
        before = Path(meta['input']).stat()
        log_path = out / 'satdump.log'
        with log_path.open('w', encoding='utf-8', errors='replace') as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        after = Path(meta['input']).stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Source recording changed during decoding; results may be invalid')
        images, data = products(work, out / 'images')
        manifest = {
            'recording': meta['input'], 'recording_format': meta['format'],
            'sample_rate': meta['sample_rate'], 'center_frequency_hz': meta['center_frequency_hz'],
            'duration_s': meta['duration_s'], 'satellite': args.satellite,
            'signal_frequency_hz': args.frequency, 'satdump_exit_code': completed.returncode,
            'decoder_command': command, 'decoder_log': 'satdump.log',
            'images': images, 'data_products': data, 'data_summary': data_summary(work),
        }
        video_ok = True
        if args.video:
            try:
                try:
                    from meteor_video import load_context, render
                except ImportError as exc:
                    raise ValueError('Video dependencies missing; install Pillow and Skyfield (pip install iqscan[video])') from exc
                video_context = load_context(meta, args)
                video_path, poster_path = render(meta, args, video_context, out)
                manifest['video'] = str(video_path.relative_to(out))
                manifest['video_poster'] = str(poster_path.relative_to(out))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                video_ok = False
                manifest['video_error'] = str(exc)
                print(f'Video rendering failed: {exc}', file=sys.stderr)
        (out / 'extraction.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'Saved {len(images)} channel image(s), {len(data)} data product(s) in {out}')
        if completed.returncode:
            print(f'SatDump exited with code {completed.returncode}; check {log_path}', file=sys.stderr)
        if not images:
            print('No image channels decoded; inspect SatDump log and recovered data.', file=sys.stderr)
        return 0 if images and video_ok else 1
    except (OSError, ValueError) as exc:
        print(f'iqscan meteor: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
