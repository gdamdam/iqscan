"""Render a synchronized Meteor waterfall and predicted sky-track video."""

from __future__ import annotations

import math
import os
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from skyfield.api import EarthSatellite, load, wgs84


FFT = 8192
DT = 0.5
FPS = 20
WIDTH, HEIGHT = 1600, 900
SPECTRUM = (96, 155, 1000, 105)
WATERFALL = (96, 325, 1000, 465)
RADIUS = 168
SKY_CX, SKY_CY = 1335, 350

NORAD = {'M2-3': 57166, 'M2-4': 59051}


def load_context(meta, args):
    """Use nextpass's private QTH configuration and cached CelesTrak elements."""
    if args.recording_start:
        start = datetime.fromisoformat(args.recording_start.replace('Z', '+00:00'))
        if start.tzinfo is None:
            raise ValueError('--recording-start must include a UTC timezone')
        start = start.astimezone(timezone.utc)
    else:
        match = re.search(r'(\d{4}-\d\d-\d\d)_(\d\d-\d\d-\d\d)', Path(meta['input']).name)
        if not match:
            raise ValueError('Video needs a SatDump-style UTC filename or --recording-start ISO-8601')
        start = datetime.strptime('_'.join(match.groups()), '%Y-%m-%d_%H-%M-%S').replace(tzinfo=timezone.utc)
    location_path = (args.location_config or
                     Path(os.environ.get('RADIO_LOCATION_CONFIG', str(Path.home() / '.config/radio/location.json')))).expanduser()
    try:
        site = json.loads(location_path.read_text())
        lat, lon = float(site['lat']), float(site['lon'])
        altitude = float(site['altitude'])
        zone = ZoneInfo(site['timezone'])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'Cannot read nextpass location {location_path}: {exc}') from exc
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Invalid observer coordinates in nextpass location')
    cat_id = NORAD[args.satellite]
    cache_root = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'nextpass'
    elements_path = (args.elements_file or cache_root / f'{cat_id}.json').expanduser()
    try:
        rows = json.loads(elements_path.read_text())
        if isinstance(rows, dict):
            rows = [rows]
        element = next(row for row in rows if int(row['NORAD_CAT_ID']) == cat_id)
        epoch = datetime.fromisoformat(element['EPOCH'].replace('Z', '+00:00'))
        if epoch.tzinfo is None:
            epoch = epoch.replace(tzinfo=timezone.utc)
    except (OSError, KeyError, TypeError, ValueError, StopIteration) as exc:
        raise ValueError(f'Cannot load nextpass orbital elements for {args.satellite} from {elements_path}: {exc}') from exc
    if abs((start - epoch).total_seconds()) > 14 * 86400:
        raise ValueError('Orbital elements are more than 14 days from the recording; refresh nextpass or pass --elements-file')
    ts = load.timescale(builtin=True)
    satellite = EarthSatellite.from_omm(ts, element)
    return dict(start=start, zone=zone, latitude=lat, longitude=lon,
                observer=wgs84.latlon(lat, lon, elevation_m=altitude),
                satellite=satellite, element_file=str(elements_path), location_file=str(location_path))

# Arial on macOS, common TrueType families elsewhere, then Pillow's bundled font,
# so --video never depends on one platform's font directory.
FONT_NAMES = ('/System/Library/Fonts/Supplemental/Arial.ttf', 'Arial.ttf', 'DejaVuSans.ttf',
              'LiberationSans-Regular.ttf')
BOLD_NAMES = ('/System/Library/Fonts/Supplemental/Arial Bold.ttf', 'Arial Bold.ttf', 'DejaVuSans-Bold.ttf',
              'LiberationSans-Bold.ttf') + FONT_NAMES


def load_font(names, size):
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                                # Pillow < 10.1 has no sized default
        return ImageFont.load_default()


FONTS = {s: load_font(FONT_NAMES, s) for s in (15, 18, 21, 23, 27, 38)}
BOLDS = {s: load_font(BOLD_NAMES, s) for s in (18, 21, 23, 27, 38)}


def spectrum_data(meta, target_hz) -> tuple[np.ndarray, np.ndarray, float]:
    if meta['samples'] < FFT:
        raise ValueError('Recording is shorter than one video FFT')
    raw = np.memmap(meta['input'], dtype='<i2', mode='r')
    rate = meta['sample_rate']
    duration = meta['duration_s']
    rows = max(1, int(math.ceil(duration / DT)))
    freq_all = meta['center_frequency_hz'] / 1e6 + np.fft.fftshift(np.fft.fftfreq(FFT, 1 / rate)) / 1e6
    lo, hi = (target_hz - 250_000) / 1e6, (target_hz + 250_000) / 1e6
    band = np.flatnonzero((freq_all >= lo) & (freq_all <= hi))
    if len(band) < 32:
        raise ValueError('The LRPT frequency is too close to the sampled-band edge for a video')
    freq = freq_all[band]
    window = np.hanning(FFT).astype(np.float32)
    db = np.empty((rows, len(band)), dtype=np.float32)
    for row in range(rows):
        first = min(int(row * DT * rate), raw.size // 2 - FFT)
        last = min(raw.size // 2 - FFT, max(first, int((row + 1) * DT * rate) - FFT))
        starts = np.linspace(first, last, 6, dtype=np.int64)
        power = np.zeros(len(band), dtype=np.float64)
        for start in starts:
            pair = raw[2 * start:2 * (start + FFT)].reshape(FFT, 2)
            iq = (pair[:, 0].astype(np.float32) + 1j * pair[:, 1].astype(np.float32)) * window
            power += np.abs(np.fft.fftshift(np.fft.fft(iq))[band]) ** 2
        db[row] = 10 * np.log10(power / len(starts) + 1e-20)
    return freq, db, duration


def altaz_seconds(context, seconds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    times = [context['start'] + timedelta(seconds=float(s)) for s in seconds]
    t = load.timescale(builtin=True).from_datetimes(times)
    alt, az, _ = (context['satellite'] - context['observer']).at(t).altaz()
    return np.asarray(alt.degrees), np.asarray(az.degrees)


def xy_sky(alt: float, az: float) -> tuple[float, float]:
    radius = (90 - alt) / 90 * RADIUS
    a = math.radians(az)
    return SKY_CX + radius * math.sin(a), SKY_CY - radius * math.cos(a)


def colorize(db: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    colors = np.array([
        [2, 4, 21], [5, 17, 83], [9, 50, 153], [17, 100, 217],
        [44, 170, 250], [145, 235, 255], [250, 252, 247],
        [255, 226, 44], [255, 130, 12], [235, 31, 12],
    ], dtype=np.float32)
    stops = np.array([0, .08, .19, .34, .48, .62, .75, .84, .93, 1.0])
    u = np.clip((db - vmin) / (vmax - vmin), 0, 1)
    return np.stack([np.interp(u, stops, colors[:, c]) for c in range(3)], -1).astype(np.uint8)


def label(draw: ImageDraw.ImageDraw, xy, value: str, size=18, color='#dce9f5', bold=False):
    draw.text(xy, value, font=(BOLDS if bold else FONTS)[size], fill=color)


def make_base(freq: np.ndarray, alt_track: np.ndarray, az_track: np.ndarray,
              duration: float, meta, args, context) -> Image.Image:
    im = Image.new('RGB', (WIDTH, HEIGHT), '#09121f')
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((18, 12, 1582, 888), radius=17, fill='#101d2c', outline='#33465a', width=2)
    label(d, (42, 31), f'METEOR {args.satellite}  /  LRPT PASS', 27, '#eff8ff', True)
    label(d, (42, 69), f"{context['start']:%d %b %Y} UTC   •   {args.frequency/1e6:.3f} MHz   •   {meta['sample_rate']/1e6:g} Msps CS16", 18, '#8dabc2')
    label(d, (96, 119), 'SPECTRUM', 21, '#e5f6ff', True)
    label(d, (96, 289), 'LIVE WATERFALL', 21, '#e5f6ff', True)
    label(d, (1132, 115), 'SATELLITE POSITION', 21, '#e5f6ff', True)
    label(d, (1132, 599), 'ELEVATION THROUGH PASS', 18, '#e5f6ff', True)
    sx, sy, sw, sh = SPECTRUM
    wx, wy, ww, wh = WATERFALL
    d.rectangle((sx, sy, sx + sw, sy + sh), fill='#07101c', outline='#3a5065')
    d.rectangle((wx, wy, wx + ww, wy + wh), fill='#03071a', outline='#3a5065')
    fmin, fmax = float(freq[0]), float(freq[-1])
    for mhz in np.linspace(fmin, fmax, 11):
        x = sx + int((mhz - fmin) / (fmax - fmin) * sw)
        d.line((x, sy, x, sy + sh), fill='#1b3446', width=1)
        d.line((x, wy, x, wy + wh), fill='#18364b', width=1)
        label(d, (x - 22, 264), f'{mhz:.2f}', 15, '#83b5d0')
    label(d, (445, 809), 'FREQUENCY (MHz)', 18, '#9ebed4')
    for ago in (0, 30, 60, 90, 120):
        y = wy + int(ago / 120 * wh)
        d.line((wx - 7, y, wx, y), fill='#76b2d8', width=2)
        label(d, (43, y - 8), f'-{ago}s', 15, '#83b5d0')
    # Sky dome: north up, east right; outer circle is the horizon.
    for elev in (0, 30, 60):
        r = int((90 - elev) / 90 * RADIUS)
        d.ellipse((SKY_CX - r, SKY_CY - r, SKY_CX + r, SKY_CY + r), outline='#365c74', width=2)
        label(d, (SKY_CX + 5, SKY_CY - r + 4), f'{elev}°', 15, '#709ab2')
    d.line((SKY_CX - RADIUS, SKY_CY, SKY_CX + RADIUS, SKY_CY), fill='#24465d')
    d.line((SKY_CX, SKY_CY - RADIUS, SKY_CX, SKY_CY + RADIUS), fill='#24465d')
    for text, x, y in [('N', SKY_CX-7, SKY_CY-RADIUS-32),
                       ('E', SKY_CX+RADIUS+12, SKY_CY-11),
                       ('S', SKY_CX-7, SKY_CY+RADIUS+9),
                       ('W', SKY_CX-RADIUS-29, SKY_CY-11)]:
        label(d, (x, y), text, 21, '#add4e5', True)
    visible = alt_track >= 0
    points = [xy_sky(a, z) for a, z in zip(alt_track[visible], az_track[visible])]
    if len(points) > 1:
        d.line(points, fill='#b1782b', width=4, joint='curve')
    # Static elevation axes and complete predicted track.
    gx, gy, gw, gh = 1140, 649, 390, 150
    d.rectangle((gx, gy, gx + gw, gy + gh), fill='#081321', outline='#3a5065')
    for e in (0, 30, 60, 90):
        y = gy + gh - int(e / 90 * gh)
        d.line((gx, y, gx + gw, y), fill='#223b4d')
        label(d, (gx - 30, y - 8), str(e), 15, '#87a6bb')
    trace = [(gx + k / (len(alt_track)-1) * gw,
              gy + gh - max(0, a) / 90 * gh) for k, a in enumerate(alt_track)]
    d.line(trace, fill='#687a36', width=3)
    label(d, (gx, 806), 'Recording start', 15, '#87a6bb')
    label(d, (gx + gw - 102, 806), 'end', 15, '#87a6bb')
    d.line((38, 850, 1562, 850), fill='#2d4d60', width=2)
    label(d, (42, 861), f'{duration/60:.2f} min recording shown in {args.video_seconds:g}s ({duration/args.video_seconds:.1f}× speed)',
          15, '#8dabc2')
    label(d, (1000, 861), f"Predicted track • QTH {context['latitude']:.3f}°, {context['longitude']:.3f}°", 15, '#8dabc2')
    return im


def render(meta, args, context, out):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('ffmpeg is required for --video')
    output = out / 'waterfall-track.mp4'
    poster = out / 'waterfall-track-poster.png'
    freq, db, duration = spectrum_data(meta, args.frequency)
    baseline = float(np.median(db))
    vmin, vmax = baseline - 2.5, baseline + 17.5
    rgb = colorize(db, vmin, vmax)
    whole = Image.fromarray(rgb, 'RGB')
    track_seconds = np.linspace(0, duration + 60, 500)
    track_alt, track_az = altaz_seconds(context, track_seconds)
    frame_count = max(1, round(FPS * args.video_seconds))
    frame_seconds = np.linspace(0, duration, frame_count, endpoint=True)
    frame_alt, frame_az = altaz_seconds(context, frame_seconds)
    base = make_base(freq, track_alt, track_az, duration, meta, args, context)
    sx, sy, sw, sh = SPECTRUM
    wx, wy, ww, wh = WATERFALL
    frequency_pos = np.linspace(0, len(freq)-1, sw).astype(int)
    video_cmd = [
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{WIDTH}x{HEIGHT}',
        '-framerate', str(FPS), '-i', '-', '-an', '-c:v', 'libx264',
        '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart', str(output),
    ]
    proc = subprocess.Popen(video_cmd, stdin=subprocess.PIPE)
    try:
        for frame_number, seconds in enumerate(frame_seconds):
            row = min(len(db)-1, int(seconds / DT))
            first = max(0, row - 239)
            frame = base.copy()
            d = ImageDraw.Draw(frame)
            # Newest sample appears at top, as in a live SDR waterfall.
            n = row - first + 1
            strip = whole.crop((0, first, len(freq), row + 1)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            scaled_height = max(1, int(n / 240 * wh))
            strip = strip.resize((ww, scaled_height), Image.Resampling.BILINEAR)
            frame.paste(strip, (wx, wy))
            # Frequency grid and highlighted nominal LRPT carrier.
            for mhz in np.linspace(float(freq[0]), float(freq[-1]), 11):
                x = wx + int((mhz - freq[0]) / (freq[-1]-freq[0]) * ww)
                d.line((x, wy, x, wy+wh), fill='#12344c', width=1)
            marker_x = wx + int((args.frequency/1e6-freq[0])/(freq[-1]-freq[0])*ww)
            d.line((marker_x, sy, marker_x, sy+sh), fill='#30d3ec', width=1)
            d.line((marker_x, wy, marker_x, wy+wh), fill='#24abc7', width=1)
            # Current spectrum, using the same absolute color scale as the waterfall.
            values = db[row, frequency_pos]
            points = [(sx + j, sy + sh - int(np.clip((v-vmin)/(vmax-vmin), 0, 1)*sh))
                      for j, v in enumerate(values)]
            d.line(points, fill='#56def4', width=2, joint='curve')
            # Current sky position and elapsed track.
            alt, az = float(frame_alt[frame_number]), float(frame_az[frame_number])
            if alt >= 0:
                x, y = xy_sky(alt, az)
                d.ellipse((x-11,y-11,x+11,y+11), fill='#fff251', outline='#ffffff', width=2)
                status = f'Az {az:05.1f}°   El {alt:04.1f}°'
            else:
                status = f'Below horizon   El {alt:.1f}°'
            label(d, (1152, 554), status, 23, '#fff086', True)
            gx, gy, gw, gh = 1140, 649, 390, 150
            xx = gx + seconds / duration * gw
            yy = gy + gh - max(0, alt)/90*gh
            d.line((xx, gy, xx, gy+gh), fill='#3dcdeb', width=2)
            d.ellipse((xx-6,yy-6,xx+6,yy+6), fill='#fff251')
            current = context['start'] + timedelta(seconds=float(seconds))
            local = current.astimezone(context['zone'])
            label(d, (1191, 31), local.strftime('%H:%M:%S %Z'), 38, '#f5fbff', True)
            label(d, (1191, 75), current.strftime('%H:%M:%S UTC'), 21, '#9bb9ca')
            # The long-term progress bar stays visible even when the signal fades.
            d.rounded_rectangle((42, 833, 1556, 840), radius=3, fill='#24465d')
            d.rounded_rectangle((42, 833, 42 + int(seconds/duration*1514), 840),
                                radius=3, fill='#49dafa')
            if frame_number == frame_count // 2:
                frame.save(poster)
            proc.stdin.write(frame.tobytes())
            if frame_number % 200 == 0:
                print(f'{frame_number}/{len(frame_seconds)} frames', flush=True)
    finally:
        # Always reap ffmpeg, including when a frame write raised on a broken pipe.
        if proc.stdin:
            proc.stdin.close()
        rc = proc.wait()
    if rc:
        raise RuntimeError(f'ffmpeg exited with {rc}')
    print(f'Created {output} ({args.video_seconds:g}s) and {poster}')
    return output, poster
