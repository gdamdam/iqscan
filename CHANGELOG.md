# Changelog

## 1.6.1 — 2026-09-25

- Bundle WWV/WWVH frequency references for offline HF scans.
- Extend `--update-references` with a locally cached EiBi shortwave/utility
  catalog; ordinary scans reuse it automatically. Add `--known-signals eibi`
  for first-scan downloads, seasonal URLs, Latin-1 parsing, validated updates
  and previous-season fallback. Retain schedule metadata without claiming
  time-aware identification.

## 1.6.0 — 2026-09-25

- Windows support: `scan.cmd` launcher, cmd.exe-style quoting for printed commands, `start`/`xdg-open` report commands per platform, SatDump lookup in Program Files and per-user Programs, reference cache under `%LOCALAPPDATA%`, `tzdata` installed with the video extra, and ANSI colour only in terminals that render it.
- Tests run on Windows (CI matrix adds windows-latest); symlink checks skip where symlinks are unavailable.

## 1.5.0 — 2026-09-25

- Fixed clip and channel filenames for recordings at 1 MS/s and above: sample rates are written as plain decimals (`2400000SPS`) instead of exponent notation that the filename parser misread as 6 samples/s. Report and terminal text use the same form.
- Source fingerprints now hash 16 evenly spaced 1 MiB blocks instead of the whole payload, removing a full extra read of the recording before every scan and redetect. Caches written by earlier versions keep their full-payload check.
- Added `--reference-band LO HI` to move the per-time level reference away from signals of interest; the band is stored with the cached spectrum shaping.
- WAV input accepts 8-bit PCM, 16-bit PCM and 32-bit float stereo, including WAVE_FORMAT_EXTENSIBLE, and reads the center frequency from the SDR#/HDSDR `auxi` chunk. Filenames with `kHz`, `MHz`, `kSPS` or `MSPS` now parse.
- The explorer rejects requests whose Host header is not loopback.
- Meteor extraction looks for SatDump in `PATH`, `$SATDUMP`, and the standard application folders, and reads the CLI generation from the reported version number.
- Reports, event JSON/CSV and clip instructions are always written as UTF-8. The persistent-peak smoothing window scales with FFT size. ffmpeg is always reaped after a failed video write.
- Modules now live in the `iqscan` package; run `python -m iqscan` or the `iqscan` script instead of `iq_scan.py`. Added a ruff configuration.

## 1.4.1 — 2026-09-23

- Channel exports now apply the channelizer's sample cap before choosing the window and keep it centered on the event, so long padding can no longer push the detected signal out of the clip.
- The spectrum explorer keeps saved `--min-offset`/`--max-offset` limits in previews and in the generated `--redetect` command.
- Meteor video falls back to common TrueType fonts and then Pillow's bundled font instead of requiring macOS Arial.

## 1.4.0

- Added `iqscan meteor` for offline SatDump LRPT extraction from M2-3/M2-4 CS16 recordings, preserving image channels, telemetry, recovered frames, and decoder logs even when final SatDump processing crashes.
- Added optional synchronized waterfall/sky-track video using nextpass's configured location and cached orbital elements.
- Included an offline Meteor LRPT frequency reference by default and clarified that long-event analysis uses representative windows rather than flagging it as a receiver problem.

## 1.3.0 — 2026-09-23

- Added unsigned 8-bit and little- or big-endian complex float IQ, raw `IQ`/`QI` component order, and single-capture SigMF input with strict metadata and sample validation.
- Added fresh-scan `--start`/`--duration` source intervals and `--min-offset`/`--max-offset` detection bounds. Event times stay relative to the selected interval; source offsets remain in provenance. The FFT still spans the selected interval.
- Added derived, centered, FIR-filtered `.cf32` channel clips with bounded output and processing metadata. Original clips remain exact source bytes. Added portable reports with relative generated links.
- Added source fingerprints to schema-2 caches, verified source relocation with `--source`, and safe redetection when the original source is absent or changed. Legacy caches remain readable for detection and skip unverified raw operations.
- Reworked optional waveform analysis to stream and decimate before its sample cap, with a configurable source-time limit, a 12-million-sample work cap, long-event windows, and per-window truncation provenance. A valid AX.25 packet at 2.4 MS/s now survives analysis.
- Kept event CSV rows chronological, added SigMF candidate annotations for IQ-order exact clips and derived channels, warned when QI clips need viewer configuration, and stamped reports with tool and cache schema versions.
- Fixed cached-source checks so missing, changed or relocated recordings cannot silently produce clips or decoder evidence from the wrong IQ payload; matrix-only redetection remains available.
- Hardened the live explorer: reject symlink escapes from the scan root, display server errors as text, and validate detection controls against each recording's sample rate.
- Fixed explorer scan switching and failed requests so stale event boxes, commands and selections clear; added keyboard selection for event boxes and rows.
- Kept parsed reference records available when a cached download has a malformed timestamp, while prompting an online refresh or marking offline freshness unknown.
- Added warnings in the terminal and report when at least 0.1% of integer I/Q components hit digital rails, plus QI viewer-order warnings for exact clips. Kept CSV event rows chronological and added report and explorer browser regressions alongside the Python 3.9/3.12 CI matrix.
