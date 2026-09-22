<div align="center">

```
 ██╗ ██████╗ ███████╗ ██████╗ █████╗ ███╗   ██╗
 ██║██╔═══██╗██╔════╝██╔════╝██╔══██╗████╗  ██║
 ██║██║   ██║███████╗██║     ███████║██╔██╗ ██║
 ██║██║▄▄ ██║╚════██║██║     ██╔══██║██║╚██╗██║
 ██║╚██████╔╝███████║╚██████╗██║  ██║██║ ╚████║
 ╚═╝ ╚══▀▀═╝ ╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═══╝
```

### Find the signals hiding in an IQ recording.

Point it at a raw capture. Get back an interactive offline report — waterfall,
ranked events, zoomed spectrograms, and bit-exact IQ clips ready for inspectrum.

<p>
<img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-1f6feb?style=for-the-badge&logo=python&logoColor=white">
<img alt="NumPy SciPy Matplotlib" src="https://img.shields.io/badge/numpy·scipy·matplotlib-013243?style=for-the-badge">
<img alt="25 tests passing" src="https://img.shields.io/badge/tests-25%20passing-2ea043?style=for-the-badge">
<img alt="version 1.1.0" src="https://img.shields.io/badge/version-1.1.0-0f766e?style=for-the-badge">
</p>
<p>
<img alt="Formats" src="https://img.shields.io/badge/formats-cs8%20·%20cs16%20·%20IQ%20WAV-6e40c9?style=flat-square">
<img alt="Report" src="https://img.shields.io/badge/report-offline%20HTML-0f766e?style=flat-square">
<img alt="Explorer" src="https://img.shields.io/badge/explorer-live%20retune-be123c?style=flat-square">
<img alt="Dependencies" src="https://img.shields.io/badge/extra%20deps-none-475569?style=flat-square">
<img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20·%20Linux-334155?style=flat-square">
<img alt="Location" src="https://img.shields.io/badge/observing%20location-not%20required-64748b?style=flat-square">
</p>

</div>

---

## ⚡ Quickstart

```sh
git clone https://github.com/gdamdam/iqscan.git
cd iqscan
./scan.sh /path/to/recording_500000SPS_137900000Hz.cs8
```

That's the whole setup. The launcher creates `.venv` and installs NumPy, SciPy and
Matplotlib on first use, then prints the report path and an `open …/report.html` command.

Filename doesn't carry its metadata? Say so explicitly:

```sh
./scan.sh capture.cs16 --sample-rate 2000000 --center-frequency 137900000
```

> [!IMPORTANT]
> Use the **saved complex sample rate after decimation**, not necessarily the receiver
> input rate. Without a center frequency the results show offsets from center instead of
> absolute frequencies, and automatic reference matching is skipped.

To install the command for use outside this checkout:

```sh
python3 -m pip install .
iqscan /path/to/recording_500000SPS_137900000Hz.cs8
```

The source checkout also provides `./scan.sh`, which creates its local virtual environment
on first use. Installed commands use the Python environment that installed `iqscan`.

---

## 📡 What goes in

| Format | Notes |
|---|---|
| `.cs8` | signed interleaved I/Q, 8-bit |
| `.cs16` | signed interleaved I/Q, 16-bit little-endian |
| IQ `.wav` | SDRconnect two-channel PCM16 (RIFF/RF64) — I is ch 1, Q is ch 2 |

Sample rate and center frequency are parsed from `NNNSPS` and `NNNHz` in SatDump-style
filenames. Other WAV encodings are rejected rather than guessed at. **The input is opened
read-only and never modified.** For IQ WAV the rate comes from the header, and a
conflicting `--sample-rate` is rejected. Oversized data-length fields warn, and only the
complete samples present are scanned.

---

## 🖼 What comes out

Every run creates a fresh `scans/<name>_<timestamp>/`. Existing directories are refused,
never overwritten. `scans/` is Git-ignored.

```
scans/capture_500000SPS_137900000Hz_TIMESTAMP/
├── report.html            ← start here: interactive, offline, no server
├── waterfall.png          ← full-band overview + broadband level trace
├── spectrum-context.png   ← detected spectrum vs. known band allocations
├── events.json            ← timing, frequency, contrast, clip metadata
├── events.csv             ← same, chronological
├── OPEN-CLIPS.txt         ← ready-to-paste inspectrum commands
├── images/
│   ├── event-01.png       ← zoomed spectrogram per event
│   └── …
└── clips/
    ├── event-01_500000SPS.cs8   ← bit-exact excerpt of the original IQ
    └── …
```

Clips are **exact original samples** — no resampling, no filtering, no frequency shift.
Clip time starts at zero; the original offset lives in `events.json` and `OPEN-CLIPS.txt`.
By default each clip carries one second of padding and caps at ten seconds, centered on
the event's strongest moment (`--max-clip-seconds` changes the cap). Event tables and
images still describe the full detected interval.

---

## 🎛 Everyday commands

```sh
./scan.sh capture.cs8 --sample-rate 500000 --top 30 --clips 10
./scan.sh capture.cs8 --sample-rate 500000 --threshold 5 --min-duration .1
./scan.sh capture.cs8 --sample-rate 500000 --clips 0 --output scans/my-new-scan
./scan.sh capture.cs8 --sat --open        # satellite hints, then open the report
./scan.sh --help
```

### Retune detection without rescanning

Changing a threshold normally means rereading the whole recording and recomputing every
FFT. `--save-spectrum` writes the normalized matrix that detection consumes, and
`--redetect` picks it up:

```sh
./scan.sh capture.cs8 --sample-rate 500000 --save-spectrum   # scan once, keep the matrix
./scan.sh --redetect scans/<that-run> --threshold 9          # seconds, not minutes
./scan.sh --redetect scans/<that-run> --threshold 4 --top 40 --dc-exclude 0
```

Redetection reuses the cached detection matrix, capped by `--max-rows`.

Each redetect writes its own scan directory and records `redetected_from` in
`events.json`. Detection parameters — `--threshold`, `--min-duration`, `--top`,
`--dc-exclude` — are all fair game. The FFT-shaping options (`--fft-size`,
`--time-bin`, `--max-rows`) are baked into the cache, so they come from the original
scan and a conflicting command line is reported and ignored. Clips need the original
recording; if it has moved away, the run warns and produces everything except `clips/`.

### Retune it live in a browser

```sh
./scan.sh --serve            # http://127.0.0.1:8731, opens with --open
./scan.sh --serve 9000 --scan-root ~/captures/scans
```

Pick any scan that has a cached spectrum, then drag threshold, minimum duration, DC
exclusion and event count and watch detection redraw over the spectrogram. Each
re-detect is a **~16 ms** call against the in-memory matrix, so it tracks the slider.
Click a box or a table row to pair them up.

The app never writes anything. It prints the exact `--redetect` command for whatever
parameters you land on, and you run that to produce the real report with images and
clips. Built on `http.server` — **no Flask, no FastAPI, no new dependency** — bound to
loopback with no authentication, which is the whole security model. Scan IDs are
validated as direct children of the scan root, so the URL cannot walk the filesystem.

> [!NOTE]
> `--threshold` is applied *before* regions are formed, not as a filter afterwards.
> Raising it does not merely drop weak events — it shrinks and splits the surviving
> ones, changing their duration, bandwidth and rank. That is why a real redetect exists
> instead of a slider over a fixed event list.

---

<details>
<summary><b>How detection actually works — and where it lies to you</b></summary>

<br>

The scanner runs Hann-windowed FFTs across the entire recording and averages adjacent
frames. Per-time normalization suppresses broadband gain changes. **Transients** are
connected regions above each frequency's median level; **persistent narrow peaks** are
compared against a local frequency baseline. Ranking puts transient regions first by
detected time-frequency area, then persistent peaks by contrast.

Defaults: 4096 FFT bins, ~0.1 s time bins, at most 2000 rows. Long recordings
automatically coarsen their time bins to bound memory — raise `--max-rows` for finer
timing at higher memory cost, or ask for an interval with `--time-bin`. Every sample is
processed, but short events can be diluted by averaging. The final incomplete FFT is
zero-padded.

Detection skips the central ±2 kHz DC region and the outer 5% of each band edge. Use
`--dc-exclude 0` if activity at center matters, accepting DC artifacts. The displayed
waterfall still shows those regions.

**This is an activity detector, not transmitter identification.** Concretely:

- Gain transitions, receiver spurs and local interference show up as candidates.
- Weak broad signals and continuous wideband signals may never be selected — including
  weak satellite downlinks.
- No detections does **not** mean an empty recording.
- Contrast dB is not calibrated SNR. Detected width is a threshold-dependent estimate.
- Absolute frequency is only as good as your tuning metadata and receiver calibration.

Ranking is a heuristic for human inspection — not scientific importance, not decode
quality.

</details>

<details>
<summary><b>Band plans and known-signal hints (downloaded, cached, matched locally)</b></summary>

<br>

When a scan has a known absolute center frequency, the selected community band plan is
downloaded and cached automatically. Only full public catalogs are requested — **no IQ
data, file paths, observing coordinates or private config are ever sent.** Matching then
happens locally against the cached catalog.

```sh
# Refresh references without scanning anything:
./scan.sh --update-references --bandplan us

# Append to any scan command:
#   --bandplan us                          U.S. community plan (default)
#   --bandplan international               Basic international community plan
#   --bandplan fr                          French community plan
#   --known-signals satnogs                Satellite downlinks; same as --sat
#   --signal-status all                    Include inactive/unknown entries
#   --match-tolerance 3000                 Match within 3 kHz
#   --max-reference-matches 8              Up to eight hints per event
#   --refresh-references                   Force new downloads
#   --offline-references                   Never fetch; use existing cache
#   --bandplan none --known-signals none   Disable references entirely
```

The report gains overlapping band/service entries, nearby satellite downlinks with mode
and catalog status, per-event reference hints, a `spectrum-context.png` comparison, and
full provenance in JSON (source URLs, download dates, source-header dates, content hashes).

> [!WARNING]
> These are **community** plans from [Arrin-KN1E/SDR-Band-Plans](https://github.com/Arrin-KN1E/SDR-Band-Plans),
> not official national allocations. The U.S. XML header was dated August 2022 and the
> basic international one August 2021 at implementation time. Some entries are historical —
> a NOAA/Meteor label is **not** evidence of current operation. The official NTIA site
> rejected automated access during development, so this tool makes no claim to an
> authoritative FCC/ITU legal allocation table.

With `--sat`, SatNOGS defaults to records marked active and not explicitly dead. That is
catalog metadata — not a live status check, not an orbital visibility calculation.
Historical recordings are matched against today's catalog, not a snapshot from then.
Frequency proximity cannot separate co-channel satellites, interference or receiver
artifacts; decoding or other evidence is required.

The default 5 kHz tolerance covers modest Doppler and tuning error and applies only to
known-signal matching, never to band boundaries. Per-event matches sort by distance to the
detected interval, then by reference width.

Caches live in `.cache/spectrum/` for a source checkout, or in
`$XDG_CACHE_HOME/iqscan/spectrum/` (usually `~/.cache/iqscan/spectrum/`) when installed,
and refresh after seven days (`--reference-max-age-days`). Download age is not the age of the information inside.
Failed downloads fall back to a validated cache with warnings; with no cache the scan
continues and clearly reports the missing source. `--update-references` exits nonzero on
reference warnings or failure. `--reference-cache` selects another directory.

**Custom references.** `--bandplan-file` replaces the plan with a local SD#-format XML or
JSON file; `--signals-file` adds custom named signals. JSON uses Hz:

```json
[
  {"name":"My service band", "low_hz":100000000, "high_hz":101000000, "mode":"FM", "status":"unverified"},
  {"name":"My reference channel", "frequency_hz":100500000, "mode":"FM", "status":"unverified"}
]
```

That's a format example, not a real allocation. Keep personal station lists outside Git
(for example in `~/.config/radio/`). Invalid custom files are reported, never silently
dropped.

Sources: [community plans](https://github.com/Arrin-KN1E/SDR-Band-Plans) ·
[SatNOGS API](https://docs.satnogs.org/projects/satnogs-db/en/latest/api.html) ·
[NTIA redbook](https://redbook.ntia.gov/view/4-17)

</details>

<details>
<summary><b>Driving the interactive HTML report</b></summary>

<br>

Hover inside any graph for a crosshair and a readout underneath: elapsed
`hh:mm:ss.mmm`, frequency/offset, sampled contrast, matching event IDs and band
references. Click to pin or unpin. **+ / −** magnify, scrolling inside the image pans,
and **Reset** restores the view.

Click an event ID in the table to highlight its region on the overview waterfall. Click a
bracketed waterfall marker to select the matching table row and reveal its details and
frequency references below. Selection survives table sorting.

Everything works offline — no server, no external JavaScript. Keep the HTML and its
`images/` together. Zoom magnifies the existing picture; it does not add FFT resolution.
Hover levels come from a reduced grid and are approximate, not calibrated power or SNR.
Reference names are hints, not identifications. Older reports must be regenerated to gain
these controls.

Append `--open` to any scan to launch the finished report in your browser.

</details>

<details>
<summary><b>Time formatting and privacy</b></summary>

<br>

Times display as `hh:mm:ss.mmm` everywhere — terminal, HTML, image axes, clip
instructions — measured **elapsed from the recording start**, not wall-clock. CSV and JSON
keep numeric `*_s` fields and add readable `*_hms` ones. Command-line duration options
still take seconds. Existing reports keep their original formatting; rerun to regenerate.

Reports embed local file paths and recording metadata, so review before sharing. They
contain no observing coordinates unless you supplied them indirectly through an input
filename or path. **The scanner never reads the private location config** — see
[nextpass](https://github.com/gdamdam/nextpass) for the tool that does.

</details>

---

## 🔬 Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Twenty-five tests over `.cs8`/`.cs16` detection, exact clip extraction, IQ WAV headers, report
injection, reference handling, spectrum caching and the explorer HTTP API. Network
access is patched off and the explorer is exercised against a real loopback server,
so the suite is hermetic.

---

## 🛰 Related

[nextpass](https://github.com/gdamdam/nextpass) predicts METEOR M2-3/M2-4 passes so you know when to record.
