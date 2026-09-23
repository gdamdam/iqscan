# iqscan

**Find activity in an IQ recording and explore it in your browser.**

Get an offline waterfall report, ranked signal candidates, and exact IQ clips for
closer inspection. Works with raw IQ, SigMF, and SDRconnect IQ WAV files.

Python 3.9+ · macOS / Linux · [GPL-3.0-only](LICENSE)

## Quick start

```sh
git clone https://github.com/gdamdam/iqscan.git
cd iqscan
./scan.sh /path/to/capture.cs8 --sample-rate 500000 --center-frequency 137900000 --open
```

Replace the path, sample rate, and center frequency with your recording's values.
The launcher creates `.venv` and installs dependencies on first use. It saves a
report under `scans/` and opens it in your browser. Your recording is never modified.

**Use the saved sample rate after decimation.** Metadata can also come from a
SigMF/WAV header or a filename such as `capture_500000SPS_137900000Hz.cs8`.

## What you get

| Output | Purpose |
|---|---|
| `report.html` | Browse the waterfall, select events, and zoom into signals |
| `events.json` / `events.csv` | Inspect or process the detection results |
| `clips/` | Exact excerpts of the original recording |
| `OPEN-CLIPS.txt` | Commands to open clips in inspectrum |

Keep the report folder together; the HTML uses images and files beside it.

## Common tasks

Add these options to your scan command:

| Task | Options |
|---|---|
| Inspect possible modulation and supported protocols | `--analyze-signals` |
| Scan only part of a recording | `--start 30 --duration 15` |
| Export filtered, centered channels | `--channel-clips 3` |
| Add satellite frequency hints | `--sat` |
| Remove generated absolute paths for sharing | `--portable --clips 0` |
| Save a spectrum for later adjustments | `--save-spectrum` |

To adjust an existing cached scan:

```sh
./scan.sh --redetect scans/YOUR_SCAN --threshold 9
./scan.sh --serve --open
```

The browser explorer previews changes. Run its printed command to save a new report.
See all options with `./scan.sh --help`.

## Before interpreting or sharing results

- Detections are candidates. Interference and receiver artifacts can appear as signals.
- Frequency matches are hints; they do not identify a transmitter. Contrast is not calibrated SNR.
- Reports normally contain local paths and recording metadata. Even with `--portable`, review filenames and metadata before sharing.
- Band plans download automatically when the center frequency is known. Use `--offline-references` to prevent catalog downloads; IQ data stays local.

## Learn more

- [Formats and input metadata](docs/guide.md#what-goes-in)
- [Analysis, exports, and cached scans](docs/guide.md#everyday-commands)
- [Reference catalogs](docs/guide.md#band-plans-and-known-signal-hints-downloaded-cached-matched-locally)
- [Pip installation](docs/guide.md#quickstart) · [Tests](docs/guide.md#validation) · [Changelog](CHANGELOG.md)

Use [nextpass](https://github.com/gdamdam/nextpass) to plan a satellite recording.
