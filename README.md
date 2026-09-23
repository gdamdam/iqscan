<div align="center">

```text
█████    ███     ████    ████    ███    █   █
  █     █   █   █       █       █   █   ██  █
  █     █   █   █       █       █   █   ██  █
  █     █   █    ███    █       █████   █ █ █
  █     █ █ █       █   █       █   █   █  ██
  █     █  █        █   █       █   █   █  ██
█████    ██ █   ████     ████   █   █   █   █
```

### Find the signals hiding in an IQ recording.

Point it at a raw capture. Get back an interactive offline report — waterfall,
ranked events, zoomed spectrograms, and bit-exact IQ clips ready for inspectrum.

<p>
<img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-1f6feb?style=for-the-badge&logo=python&logoColor=white">
<img alt="NumPy SciPy Matplotlib" src="https://img.shields.io/badge/numpy·scipy·matplotlib-013243?style=for-the-badge">
<img alt="version 1.3.0" src="https://img.shields.io/badge/version-1.3.0-0f766e?style=for-the-badge">
</p>
<p>
<img alt="Formats" src="https://img.shields.io/badge/formats-raw%20IQ%20·%20SigMF%20·%20IQ%20WAV-6e40c9?style=flat-square">
<img alt="Report" src="https://img.shields.io/badge/report-offline%20HTML-0f766e?style=flat-square">
<img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20·%20Linux-334155?style=flat-square">
<a href="LICENSE"><img alt="GPL-3.0-only" src="https://img.shields.io/badge/license-GPL--3.0--only-blue?style=flat-square"></a>
<a href="https://github.com/gdamdam/iqscan/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/gdamdam/iqscan/actions/workflows/tests.yml/badge.svg"></a>
</p>

</div>

---

## ⚡ Quick start

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

---

## 🖼 What you get

| Output | Purpose |
|---|---|
| `report.html` | Browse the waterfall, select events, and zoom into signals |
| `events.json` / `events.csv` | Inspect or process the detection results |
| `clips/` | Exact excerpts of the original recording |
| `OPEN-CLIPS.txt` | Commands to open clips in inspectrum |

Keep the report folder together; the HTML uses images and files beside it.

---

## 🎛 Common tasks

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

---

## 🔎 Before interpreting or sharing results

- Detections are candidates. Interference and receiver artifacts can appear as signals.
- Frequency matches are hints; they do not identify a transmitter. Contrast is not calibrated SNR.
- Reports normally contain local paths and recording metadata. Even with `--portable`, review filenames and metadata before sharing.
- Band plans download automatically when the center frequency is known. Use `--offline-references` to prevent catalog downloads; IQ data stays local.

---

## 📚 Learn more

- [Formats and input metadata](docs/guide.md#what-goes-in)
- [Analysis, exports, and cached scans](docs/guide.md#everyday-commands)
- [Reference catalogs](docs/guide.md#band-plans-and-known-signal-hints-downloaded-cached-matched-locally)
- [Pip installation](docs/guide.md#quickstart) · [Tests](docs/guide.md#validation) · [Changelog](CHANGELOG.md)

Use [nextpass](https://github.com/gdamdam/nextpass) to plan a satellite recording.
