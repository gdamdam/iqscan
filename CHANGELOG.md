# Changelog

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
