# iqscan guide

[Back to the README](../README.md)

Commands below run from the repository root.

- [Quickstart](#quickstart)
- [What goes in](#what-goes-in)
- [What comes out](#what-comes-out)
- [Everyday commands](#everyday-commands)
- [How detection actually works — and where it lies to you](#how-detection-actually-works--and-where-it-lies-to-you)
- [Band plans and known-signal hints (downloaded, cached, matched locally)](#band-plans-and-known-signal-hints-downloaded-cached-matched-locally)
- [Driving the interactive HTML report](#driving-the-interactive-html-report)
- [Time formatting and privacy](#time-formatting-and-privacy)
- [Validation](#validation)
- [Related](#related)

## Quickstart

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

## What goes in

| Format | Notes |
|---|---|
| `.cs8` | signed interleaved I/Q, 8-bit |
| `.cs16` | signed interleaved I/Q, 16-bit little-endian |
| `.cu8` | unsigned interleaved I/Q, 8-bit (centered around 127.5) |
| `.cf32`, `.cf32_le`, `.cf32_be` | interleaved I/Q, 32-bit float; `.cf32` means little-endian |
| `.sigmf-meta` / `.sigmf-data` | SigMF single-capture complex IQ with supported datatype and sample-rate metadata |
| IQ `.wav` | SDRconnect two-channel PCM16 (RIFF/RF64) — I is ch 1, Q is ch 2 |

Raw inputs default to I then Q component order; use `--iq-order QI` for a reversed
interleave. `--format` can supply or override a raw extension. Sample rate and center
frequency are parsed from `NNNSPS` and `NNNHz` in SatDump-style filenames when the
format has no header. SigMF and IQ WAV headers supply their own sample rate; conflicting
command-line metadata is rejected. This release supports one SigMF capture starting at
sample zero; multiple captures and unsupported datatypes are rejected. Non-finite float
samples and incomplete complex samples are rejected. **The input is opened read-only and
never modified.** For IQ WAV, oversized data-length fields warn and only complete samples
present are scanned. Integer inputs count I/Q components at their digital rails;
when at least 0.1% hit a rail, the terminal and HTML report warn that overload may
distort detections and modulation evidence.

---

## What comes out

Every run creates a fresh `scans/<name>_<timestamp>/`. Existing directories are refused,
never overwritten. `scans/` is Git-ignored.

```
scans/capture_500000SPS_137900000Hz_TIMESTAMP/
├── report.html            ← start here: interactive, offline, no server
├── waterfall.png          ← full-band overview + broadband level trace
├── spectrum-context.png   ← detected spectrum vs. known band allocations
├── events.json            ← timing, frequency, contrast, clip metadata
├── events.csv             ← event rows in chronological order
├── OPEN-CLIPS.txt         ← ready-to-paste inspectrum commands
├── spectrum.npz           ← optional with --save-spectrum
├── images/
│   ├── event-01.png       ← zoomed spectrogram per event
│   └── …
├── clips/
│   ├── event-01_500000SPS.cs8         ← bit-exact excerpt of the original IQ
│   └── event-01_500000SPS.cs8.sigmf-meta ← event annotation (IQ order)
└── channels/              ← optional with --channel-clips
    ├── event-01_48000SPS.cf32          ← centered, filtered derived IQ
    ├── event-01_48000SPS.cf32.sigmf-meta ← derived-channel annotation
    └── event-01_48000SPS.json          ← processing and source provenance
```

Original clips are **exact source bytes** — no resampling, no filtering, no frequency
shift. Clip time starts at zero; `clip_start_original_s` in `events.json` records its
position in the source, including any `--start` offset. By default each clip carries
one second of padding and caps at ten seconds around the event's strongest moment
(`--max-clip-seconds` changes the cap). Event tables and images still describe the full
detected interval. For IQ-order clips, a SigMF sidecar marks the detected candidate
within the clip. QI-order bytes are preserved exactly and carry a viewer-order warning
instead of a SigMF sidecar.

---

## Everyday commands

```sh
./scan.sh capture.cs8 --sample-rate 500000 --top 30 --clips 10
./scan.sh capture.cs8 --sample-rate 500000 --threshold 5 --min-duration .1
./scan.sh capture.cs8 --sample-rate 500000 --clips 0 --output scans/my-new-scan
./scan.sh capture.cs8 --sample-rate 500000 --analyze-signals --clips 0
./scan.sh capture.cs8 --sample-rate 500000 --start 30 --duration 15 --min-offset 10000 --max-offset 60000
./scan.sh capture.cs8 --sample-rate 500000 --channel-clips 3 --clips 3
./scan.sh capture.cs8 --sample-rate 500000 --analyze-signals --analysis-seconds 5
./scan.sh capture.cs8 --sample-rate 500000 --portable --clips 0
# terrestrial ISM or amateur capture: waveform evidence only
./scan.sh field_433MHz_1000000SPS_433920000Hz.cs8 --analyze-signals --clips 0
# satellite capture: optional SatNOGS frequency context stays separate
./scan.sh downlink_137900000Hz.cs8 --sample-rate 2400000 --analyze-signals --sat --clips 0
./scan.sh capture.cs8 --sat --open        # satellite hints, then open the report
./scan.sh --help
```

### Optional waveform and symbol-rate evidence

`--analyze-signals` enables a bounded inspection of the original raw IQ around each
detected event. It reports low or medium confidence candidates such as AM, FM, FSK,
PSK, carrier/tone and OOK, along with supporting features and a
symbol rate only when decoding confirms it (currently 1200 baud); otherwise the
rate stays unknown. The currently supported
confirmed protocol check is FM Bell 202 AFSK1200 carrying AX.25 UI frames with a
valid HDLC frame and FCS; other protocols remain unconfirmed. This is general-purpose
waveform evidence for terrestrial, satellite, ISM, amateur, broadcast and other
recordings; it is not a satellite-only classifier and it does not decode arbitrary
protocols. A candidate is not a transmitter identification.

The option is opt-in because raw samples are required. Analysis reads at most two
seconds of source IQ per event by default (`--analysis-seconds` accepts up to 30), with
an additional 12-million-source-sample work cap. It mixes, FIR-filters and decimates
in bounded chunks before inspecting at most 262,144 output samples per window. Long
events may use several windows near their start, detected peak and end within the same
source-work budget. `events.json` records each window's start, end, output rate and
truncation reasons. This keeps a 2.4 MS/s packet long enough for AX.25 decoding while
bounding memory and work; it does not guarantee a decode for every event.

Reports keep the result in `events.json` and `events.csv`, show a summary in the
terminal and safely escaped HTML, and leave the protocol field unconfirmed unless a
supported decoder validates a frame. A `--redetect` run can still use a cached spectrum
without the source recording; in that case signal analysis is unknown. The live explorer
only redraws cached detection and never invents modulation or protocol evidence; run
the printed redetect command with `--analyze-signals` while the verified raw capture is
available.

Reference catalogs remain separate context. Band plans and optional SatNOGS entries
are frequency overlap hints and cannot promote a frequency match into an observed
signal or protocol identification.

### Scan an interval or a frequency range

`--start` and `--duration` select a source interval for a **fresh scan**. FFTs process
that selected interval, and report/event times start at zero within it. Metadata keeps
`scan_start_s` so exact clips and analysis windows can point back to the source time.
These options cannot be combined with `--redetect`.

`--min-offset` and `--max-offset` bound **detection** in Hz relative to the recording's
center. The FFT still covers the full selected interval and sampled band, so the
waterfall retains context. The bounds can be changed on redetection without rebuilding
the spectrum.

### Export a centered channel or a portable report

`--channel-clips N` writes up to N derived `.cf32` clips under `channels/`. Each clip
translates its event to zero Hz, applies a low-pass FIR filter, and decimates before
export. Output is limited to 262,144 complex samples per clip. Its JSON sidecar records
source time, output rate, frequency shift, processing and warnings; a SigMF sidecar
marks the derived clip. These clips are useful for closer inspection; `clips/` retains
the exact source bytes.

`--portable` removes generated absolute local paths from the HTML, JSON, CSV and clip
instructions, leaving relative links and source filenames suitable for sharing. Review
recording filenames and metadata before sharing. Portable output is a report, not a
reusable spectrum cache, so it cannot be combined with `--save-spectrum`.

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
Source verification for clips and decoding adds I/O proportional to recording length.

Each redetect writes its own scan directory and records `redetected_from` in
`events.json`. Detection parameters — `--threshold`, `--min-duration`, `--top`,
`--dc-exclude`, `--min-offset` and `--max-offset` — are all fair game. The FFT-shaping
options (`--fft-size`, `--time-bin`, `--max-rows`) are baked into the cache, so they
come from the original scan and a conflicting command line is reported and ignored.

New caches record a source fingerprint and cache schema version 2. To use raw samples
for exact clips, channel clips or waveform analysis on redetection, the source must
match that fingerprint. Use `--source /new/path/to/recording.cs8` with `--redetect`
after moving the file; changed bytes or interpretation are refused for raw operations.
Old caches without a fingerprint still support cached detection but skip raw operations
with a warning. Fingerprint verification reads the selected source payload, so a run
that needs clips or decoding may take longer than a matrix-only redetect.

### Retune it live in a browser

```sh
./scan.sh --serve            # http://127.0.0.1:8731, opens with --open
./scan.sh --serve 9000 --scan-root ~/captures/scans
```

Pick any scan that has a cached spectrum, then drag threshold, minimum duration, DC
exclusion and event count and watch detection redraw over the spectrogram. Detection runs against the in-memory matrix as you adjust the controls.
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

## How detection actually works — and where it lies to you

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

## Band plans and known-signal hints (downloaded, cached, matched locally)

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

## Driving the interactive HTML report

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

## Time formatting and privacy

Times display as `hh:mm:ss.mmm` everywhere — terminal, HTML, image axes, clip
instructions — measured **elapsed from the selected scan interval**, not wall-clock.
For `--start` scans, `scan_start_s` records the interval’s source offset and
`clip_start_original_s` records the clip’s source position. CSV and JSON
keep numeric `*_s` fields and add readable `*_hms` ones. Command-line duration options
still take seconds. Existing reports keep their original formatting; rerun to regenerate.

Reports embed local file paths and recording metadata, so review before sharing. They
contain no observing coordinates unless you supplied them indirectly through an input
filename or path. **The scanner never reads the private location config** — see
[nextpass](https://github.com/gdamdam/nextpass) for the tool that does.

---

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

For browser regressions, install the optional test dependency and Chromium, then run:

```sh
python -m pip install -e '.[test]'
python -m playwright install chromium
IQSCAN_REQUIRE_BROWSER=1 python -m unittest discover -s tests -p 'test_browser.py'
```

The regression suite covers supported raw formats and SigMF, exact clip extraction,
IQ WAV headers, bounded waveform analysis, high-rate AX.25 evidence, source fingerprint
checks, portable reports, frequency and time bounds, reference handling, spectrum
caching and the explorer HTTP API. Network access is patched off for unit tests; the
explorer tests use a real loopback server. The GitHub Actions workflow is configured
for Python 3.9 and 3.12 unit tests, plus Chromium browser tests on Python 3.12.

---

## Related

[nextpass](https://github.com/gdamdam/nextpass) predicts METEOR M2-3/M2-4 passes so you know when to record.

### Related design references

The workflows and metadata choices here were informed by [inspectrum](https://github.com/miek/inspectrum)
for time selection and filtered exports, [IQEngine](https://github.com/IQEngine/IQEngine)
for SigMF-centered metadata and annotations, and [Universal Radio Hacker](https://github.com/jopohl/urh)
for keeping demodulation evidence distinct from protocol interpretation. These are
design references; no code was copied from them.
