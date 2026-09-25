"""Conservative modulation and signal-feature inference for bounded IQ snippets.

This module deliberately reports families of plausible modulations.  It does not
consult frequency plans or catalogs, and it never turns a frequency into a
protocol name.  The routines are intended for triage of an event in a recording,
not for replacing a protocol decoder.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:  # scipy is a project dependency, but keep import failure graceful.
    from scipy.signal import fftconvolve, filtfilt, firwin
except Exception:  # pragma: no cover - exercised only in minimal installations
    fftconvolve = filtfilt = firwin = None


MAX_SAMPLES = 262_144
_EPS = 1e-12


def _finite(value: Any) -> bool:
    try:
        return bool(math.isfinite(float(value)))
    except (TypeError, ValueError, OverflowError):
        return False


def _f(value: Any) -> Optional[float]:
    """Convert a scalar to a JSON-safe finite float, or None."""
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def _clean_samples(samples: Any, sample_rate: Any) -> Tuple[Optional[np.ndarray], Optional[float], List[str]]:
    warnings: List[str] = []
    fs = _f(sample_rate)
    if fs is None or fs <= 0:
        warnings.append("invalid sample rate")
        return None, None, warnings
    try:
        a = np.asarray(samples)
    except Exception:
        warnings.append("samples could not be converted to an array")
        return None, fs, warnings
    if a.ndim != 1:
        warnings.append("samples must be a one-dimensional complex array")
        return None, fs, warnings
    if not np.iscomplexobj(a):
        # A real array is almost always a caller mistake; accepting it would
        # create a misleading single-sideband interpretation.
        warnings.append("samples must have a complex dtype")
        return None, fs, warnings
    a = np.asarray(a, dtype=np.complex128)
    if len(a) > MAX_SAMPLES:
        a = a[:MAX_SAMPLES]
        warnings.append(f"analysis bounded to {MAX_SAMPLES} complex samples")
    if len(a) and not np.isfinite(a).all():
        warnings.append("samples contain non-finite values")
        return None, fs, warnings
    return a, fs, warnings


def _empty(warnings: Iterable[str], features: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "status": "unknown",
        "candidates": [],
        "features": features or {},
        "symbol_rate_baud": None,
        "protocol": {"name": None, "status": "unconfirmed", "evidence": []},
        "warnings": [str(x) for x in warnings],
    }


def _safe_percentile(x: np.ndarray, q: float, default: float = 0.0) -> float:
    if not len(x):
        return default
    y = np.asarray(x, dtype=np.float64)
    y = y[np.isfinite(y)]
    return float(np.percentile(y, q)) if len(y) else default


def _run_lengths(mask: np.ndarray) -> List[int]:
    if not len(mask):
        return []
    starts = np.flatnonzero(np.r_[True, mask[1:] != mask[:-1]])
    ends = np.r_[starts[1:], len(mask)]
    return [int(b - a) for a, b in zip(starts, ends)]


def _spectrum(a: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray, float]:
    n = len(a)
    nfft = 1 << max(8, min(18, int(math.ceil(math.log2(max(256, n))))))
    nfft = max(256, min(nfft, 131072))
    window = np.hanning(n)
    # Retain DC: after event channelization, a real carrier is intentionally at
    # zero Hz. The caller can inspect dc_power to distinguish a receiver offset.
    spec = np.fft.fftshift(np.fft.fft(a * window, n=nfft))
    p = np.abs(spec) ** 2
    freq = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    return freq, p, float(fs / nfft)


def analyze_samples(samples: Any, sample_rate: float, analysis_bandwidth_hz: Optional[float] = None) -> Dict[str, Any]:
    """Analyze a bounded complex-IQ snippet and return a JSON-safe dictionary.

    The detector intentionally uses conservative thresholds.  ``confidence`` is
    qualitative evidence strength, not a probability.  A result with no robust
    evidence has ``status == 'unknown'``.
    """
    a, fs, warnings = _clean_samples(samples, sample_rate)
    if a is None or fs is None:
        return _empty(warnings)
    n = len(a)
    if n < 256:
        return _empty([*warnings, "snippet is too short for reliable modulation inference"],
                      {"n_samples": int(n), "sample_rate_hz": fs, "duration_s": n / fs})
    power = np.abs(a) ** 2
    rms = float(np.sqrt(np.mean(power)))
    peak = float(np.max(np.abs(a))) if n else 0.0
    env = np.abs(a)
    env_mean = float(np.mean(env))
    env_std = float(np.std(env))
    env_cv = env_std / max(env_mean, _EPS)
    dphase = np.angle(a[1:] * np.conj(a[:-1]))
    inst_hz = dphase * fs / (2.0 * np.pi)
    inst_med = float(np.median(inst_hz)) if len(inst_hz) else 0.0
    inst_dev = float(np.std(inst_hz)) if len(inst_hz) else 0.0
    freq, spec, df = _spectrum(a, fs)
    # When the caller has channelized a snippet, evaluate contrast and
    # flatness in the retained passband. Stopband zeros otherwise make filtered
    # white noise look like a perfect narrow carrier.
    if _finite(analysis_bandwidth_hz) and 0 < float(analysis_bandwidth_hz) < fs:
        passband = np.abs(freq) <= min(fs * .49, float(analysis_bandwidth_hz) * .8)
        if int(np.count_nonzero(passband)) >= 16:
            metric_mask = np.abs(freq) <= min(fs * .49, float(analysis_bandwidth_hz) * .45)
            if int(np.count_nonzero(metric_mask)) < 16:
                metric_mask = passband
        else:
            metric_mask = np.ones(len(freq), dtype=bool)
    else:
        metric_mask = np.ones(len(freq), dtype=bool)
    metric_spec = spec[metric_mask]
    metric_freq = freq[metric_mask]
    positive_power = np.maximum(metric_spec, _EPS)
    local_peak = int(np.argmax(metric_spec))
    peak_i = int(np.flatnonzero(metric_mask)[local_peak])
    peak_power = float(spec[peak_i])
    # The robust median excludes a small neighborhood around the maximum.
    neighborhood = max(2, int(round(200.0 / max(df, _EPS))))
    background = np.delete(metric_spec, np.arange(max(0, local_peak - neighborhood),
                                                  min(len(metric_spec), local_peak + neighborhood + 1)))
    noise_power = max(_safe_percentile(background, 50, _EPS), _EPS)
    snr_db = float(10.0 * np.log10(max(peak_power, _EPS) / noise_power))
    flatness = float(np.exp(np.mean(np.log(positive_power))) / max(np.mean(positive_power), _EPS))
    # Occupied width around the strongest lobe at -10 dB.  This is only a
    # feature; it does not identify a radio service.
    threshold = peak_power * 0.1
    occupied = np.flatnonzero(metric_spec >= threshold)
    bandwidth = float((metric_freq[occupied[-1]] - metric_freq[occupied[0]] + df) if len(occupied) else 0.0)
    # Phase stability and fourth-order phase coherence are useful family hints.
    unit = a / np.maximum(env, _EPS)
    # PSK often suppresses its carrier: the strongest ordinary FFT bin can
    # be a data sideband. Raising unit phasors to the modulation order removes
    # ideal data transitions, exposing residual carrier rotation instead.
    phase_index = np.arange(n, dtype=np.float64)
    def phase_coherence(order):
        raised = unit ** order
        rotation = np.angle(np.mean(raised[1:] * raised[:-1].conj()))
        return float(abs(np.mean(raised * np.exp(-1j * rotation * phase_index))))
    c2, c4 = phase_coherence(2), phase_coherence(4)
    residual_steps = np.abs(np.angle(np.exp(1j * (dphase - np.median(dphase)))))
    discrete_jumps = (np.median(residual_steps) < .1
                      and np.mean(residual_steps > .7) > .005)
    dphase_std = float(np.std(dphase)) if len(dphase) else math.pi
    envelope_ac = float(np.corrcoef((env[:-1] - env[:-1].mean()),
                                    (env[1:] - env[1:].mean()))[0, 1]) if n > 2 and env_std > _EPS else 0.0
    features: Dict[str, Any] = {
        "n_samples": int(n),
        "sample_rate_hz": fs,
        "duration_s": float(n / fs),
        "rms": rms,
        "peak": peak,
        "crest_factor": float(peak / max(rms, _EPS)),
        "mean_i": float(np.mean(a.real)),
        "mean_q": float(np.mean(a.imag)),
        "dc_power": float(abs(np.mean(a)) ** 2),
        "power": float(np.mean(power)),
        "spectral_peak_excess_db": snr_db,
        "dominant_frequency_hz": float(freq[peak_i]),
        "bandwidth_hz": max(0.0, bandwidth),
        "spectral_flatness": flatness,
        "envelope_cv": env_cv,
        "envelope_autocorrelation_1": envelope_ac,
        "instantaneous_frequency_hz": inst_med,
        "instantaneous_frequency_std_hz": inst_dev,
        "phase_increment_std_rad": dphase_std,
        "constant_modulus_c2": c2,
        "constant_modulus_c4": c4,
    }
    # A single FFT bin in white noise commonly sits 10--13 dB above the
    # median. Requiring a stronger excess prevents random noise envelopes from
    # being mislabeled as AM/OOK.
    if not rms or snr_db < 16.0 or flatness > 0.78:
        reason = "low SNR or noise-like spectrum"
        return _empty([*warnings, reason], features)

    candidates: List[Dict[str, Any]] = []
    def candidate(name: str, confidence: str, evidence: List[str]) -> None:
        candidates.append({"modulation": name, "confidence": confidence,
                           "evidence": [str(e) for e in evidence]})

    # A narrow, phase-stable lobe is a carrier/tone family.  Modulated tones
    # retain enough lobe energy to be listed alongside the likely family.
    tone_like = snr_db >= 8.0 and flatness < 0.30 and bandwidth < max(30.0, fs * 0.012)
    if tone_like and dphase_std < 0.32:
        candidate("carrier/tone", "medium" if snr_db >= 15 else "low",
                  [f"narrow spectral peak ({bandwidth:.1f} Hz occupied width)",
                   f"stable phase increments (std {dphase_std:.3f} rad)"])

    # OOK/AM: envelope variation well above noise, with a carrier or sideband
    # peak.  The run-length test catches on/off keying without calling it AM.
    env_p10, env_p90 = _safe_percentile(env, 10), _safe_percentile(env, 90)
    on_threshold = env_p10 + 0.55 * max(env_p90 - env_p10, _EPS)
    runs = _run_lengths(env > on_threshold)
    long_run_fraction = float(max(runs) / n) if runs else 0.0
    am_like = env_cv > 0.12 and snr_db >= 8.0 and (flatness < 0.72)
    if am_like:
        ev = [f"envelope coefficient of variation {env_cv:.2f}"]
        if long_run_fraction > 0.008:
            ev.append("envelope has sustained on/off runs")
            candidate("OOK", "medium" if env_cv > 0.32 else "low", ev)
        else:
            ev.append("envelope varies around a spectral carrier")
            candidate("AM", "medium" if env_cv > 0.22 else "low", ev)

    # FM/FSK have changing instantaneous frequency.  A broad but coherent
    # signal with nearly constant envelope supports the family inference.
    const_env = env_cv < 0.16
    freq_dynamic = inst_dev > max(8.0, fs / n * 2.0)
    if const_env and freq_dynamic and snr_db >= 14.0 and flatness < 0.72:
        # Histogram separation is only a weak FSK cue. Smooth FM and FSK are
        # intentionally both reported when the evidence cannot distinguish them.
        q25, q75 = np.percentile(inst_hz, [25, 75])
        if q75 - q25 > max(20.0, 0.004 * fs):
            ambiguity = [f"instantaneous frequency varies ({inst_dev:.1f} Hz std)",
                         "approximately constant envelope; FM versus FSK is ambiguous"]
            candidate("FM", "low", ambiguity)
            candidate("FSK", "low", ambiguity)
        else:
            candidate("FM", "medium" if inst_dev > fs * .02 else "low",
                      [f"instantaneous frequency varies ({inst_dev:.1f} Hz std)",
                       "approximately constant envelope"])

    # PSK family: constant envelope and phase-state coherence. A noisy signal
    # can satisfy one of C2/C4 by chance, so require a clear spectral excess and
    # non-trivial phase increments.
    # Smooth FM phase trajectories can have non-zero fourth-order moments; a
    # PSK hint requires visibly discrete phase increments as well.
    psk_like = const_env and snr_db >= 14.0 and flatness < 0.72 and discrete_jumps and (c2 > .30 or c4 > .30)
    if psk_like:
        candidates = [c for c in candidates if c["modulation"] not in {"FM", "FSK"}]
        if c4 > c2 * 1.25 and c4 > .30:
            candidate("QPSK", "low", [f"fourth-order phase coherence {c4:.2f}",
                                        "near-constant envelope"])
        elif c2 > .30:
            candidate("BPSK", "low", [f"second-order phase coherence {c2:.2f}",
                                         "near-constant envelope"])
        else:
            candidate("PSK", "low", ["phase-state coherence is present but order is ambiguous",
                                        "near-constant envelope"])

    # Remove duplicate family entries while preserving evidence order.
    dedup: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        if c["modulation"] not in dedup:
            dedup[c["modulation"]] = c
        else:
            old = dedup[c["modulation"]]
            old["evidence"] = list(dict.fromkeys(old["evidence"] + c["evidence"]))
    candidates = list(dedup.values())
    # A baud estimate is deliberately withheld from heuristic envelope periods:
    # a repeated preamble or modulation tone is not proof of symbol timing.
    symbol_rate: Optional[float] = None
    protocol: Dict[str, Any] = {"name": None, "status": "unconfirmed", "evidence": []}
    # A decoder is attempted independently of heuristic family labels. An
    # AX.25 burst has an on/off envelope and can otherwise be mislabeled OOK;
    # only valid HDLC framing, address fields and CRC may confirm the protocol.
    try:
        from .protocol_decode import decode_ax25_afsk  # type: ignore
        decoded = decode_ax25_afsk(a, fs)
        if isinstance(decoded, dict) and decoded.get("status") == "confirmed":
            protocol = {
                "name": str(decoded.get("name", "AX.25")),
                "status": "confirmed",
                "evidence": [str(x) for x in decoded.get("evidence", [])],
            }
            symbol_rate = 1200.0
            features["symbol_period_samples"] = float(fs / symbol_rate)
            # Protocol evidence supersedes ambiguous heuristic candidates.
            candidates = [{
                "modulation": "AFSK1200",
                "confidence": "medium",
                "evidence": [
                    "validated Bell 202 AFSK frame over FM",
                    "HDLC framing, AX.25 address fields and CRC-16/X-25 passed",
                ],
            }]
    except (ImportError, AttributeError, TypeError, ValueError, OverflowError):
        # Decoder availability is optional and malformed snippets are not
        # allowed to turn heuristic analysis into a hard failure.
        pass
    if not candidates:
        return _empty([*warnings, "signal evidence is ambiguous"], features)
    return {
        "status": "candidate",
        "candidates": candidates,
        "features": features,
        "symbol_rate_baud": symbol_rate,
        "protocol": protocol,
        "warnings": warnings,
    }


def _read_raw(path: Path, fmt: str, offset: int, count: int) -> np.ndarray:
    """Compatibility reader for tests and callers with an explicit byte offset."""
    from .iq_input import read_samples
    byte_size = {"cs8": 2, "cu8": 2, "cs16": 4,
                 "cf32_le": 8, "cf32_be": 8}.get(fmt)
    if byte_size is None:
        raise ValueError("unsupported IQ format")
    return read_samples({"input": str(path), "format": fmt, "data_offset": offset,
                         "bytes_per_complex": byte_size, "samples": count}, 0, count)


def _read_wav_iq(path: Path, data_offset: int, count: int) -> np.ndarray:
    return _read_raw(path, "cs16", data_offset, count)


def _channelize(samples: np.ndarray, fs: float, center_hz: float, bandwidth_hz: Optional[float]) -> Tuple[np.ndarray, float, List[str]]:
    """Retain the array-based helper used by sample-level tests."""
    warnings: List[str] = []
    if not len(samples):
        return samples, fs, warnings
    if not _finite(center_hz) or abs(float(center_hz)) >= fs / 2:
        warnings.append("event center offset is invalid for sample rate")
        return np.empty(0, dtype=np.complex128), fs, warnings
    t = np.arange(len(samples), dtype=np.float64) / fs
    mixed = samples * np.exp(-2j * np.pi * float(center_hz) * t)
    bw = float(bandwidth_hz) if _finite(bandwidth_hz) and float(bandwidth_hz) > 0 else fs * .08
    cutoff = min(fs * .46, max(fs * .015, bw * .65))
    if firwin is None or len(mixed) < 48:
        return mixed, fs, warnings
    taps = min(129, max(33, (len(mixed) // 64) * 2 + 1))
    if taps % 2 == 0:
        taps += 1
    try:
        h = firwin(taps, cutoff, fs=fs, window="hann")
        if len(mixed) > taps * 3:
            mixed = filtfilt(h, [1.0], mixed).astype(np.complex128)
        else:
            warnings.append("short event skipped FIR edge filtering")
    except Exception:
        warnings.append("channel filter unavailable for event")
    return mixed, fs, warnings


def _channel_plan(fs: float, bandwidth_hz: Any) -> Tuple[int, float, float]:
    """Choose an output rate with room for the wanted channel and decoder."""
    bw = float(bandwidth_hz) if _finite(bandwidth_hz) and float(bandwidth_hz) > 0 else min(12_000.0, fs * .08)
    target_rate = max(48_000.0, 4.0 * bw)
    decimation = max(1, int(fs // target_rate))
    output_fs = fs / decimation
    cutoff = min(output_fs * .42, max(output_fs * .015, bw * .65))
    return decimation, output_fs, cutoff


def _stream_channelize(meta: Dict[str, Any], first: int, count: int, center_hz: float,
                       bandwidth_hz: Optional[float], max_output_samples: int = MAX_SAMPLES
                       ) -> Tuple[np.ndarray, float, List[str]]:
    """Read, mix, FIR-filter and decimate a source interval in bounded chunks.

    ``first`` and ``count`` are source complex-sample coordinates. A causal FIR
    carries state across reads, so chunk boundaries do not alias or lose data.
    The returned array never exceeds ``max_output_samples``.
    """
    from .iq_input import read_samples
    fs = float(meta["sample_rate"])
    if not _finite(fs) or fs <= 0:
        raise ValueError("invalid sample rate")
    if not _finite(center_hz) or abs(float(center_hz)) >= fs / 2:
        raise ValueError("event center offset is invalid for sample rate")
    decimation, output_fs, cutoff = _channel_plan(fs, bandwidth_hz)
    warnings: List[str] = []
    if firwin is None or fftconvolve is None:
        if decimation > 1:
            warnings.append("anti-alias filter unavailable; decimation skipped")
        decimation, output_fs = 1, fs
    max_output_samples = max(1, min(int(max_output_samples), MAX_SAMPLES))
    count = max(0, int(count))
    allowed_raw = max_output_samples * decimation
    if count > allowed_raw:
        count = allowed_raw
        warnings.append("channel output bounded by sample limit")
    if count == 0:
        return np.empty(0, dtype=np.complex128), output_fs, warnings
    if firwin is not None and fftconvolve is not None:
        taps = min(4097, max(129, 12 * decimation + 1))
        if taps % 2 == 0:
            taps += 1
        h = firwin(taps, cutoff, fs=fs, window="hann")
        history = np.zeros(taps - 1, dtype=np.complex128)
    else:
        h = None
        history = np.empty(0, dtype=np.complex128)
    pieces: List[np.ndarray] = []
    processed = 0
    while processed < count:
        want = min(65_536, count - processed)
        chunk = read_samples(meta, first + processed, want)
        if len(chunk) == 0:
            warnings.append("recording ended before requested analysis window")
            break
        absolute = first + processed + np.arange(len(chunk), dtype=np.float64)
        phase = np.remainder(absolute * (float(center_hz) / fs), 1.0)
        mixed = chunk * np.exp(-2j * np.pi * phase)
        if h is not None:
            joined = np.concatenate((history, mixed))
            filtered = fftconvolve(joined, h, mode="full")[len(history):len(history) + len(mixed)]
            history = joined[-len(history):]
        else:
            filtered = mixed
        offset = (-processed) % decimation
        pieces.append(np.asarray(filtered[offset::decimation], dtype=np.complex128))
        processed += len(chunk)
        if len(chunk) < want:
            warnings.append("recording ended before requested analysis window")
            break
    output = np.concatenate(pieces) if pieces else np.empty(0, dtype=np.complex128)
    return output[:max_output_samples], output_fs, warnings


def _event_windows(start: int, end: int, peak: int, budget: int,
                   output_cap_raw: int) -> Tuple[List[Tuple[int, int]], List[str]]:
    """Place bounded windows at a short event or across a long one."""
    span = max(0, end - start)
    per_window_cap = max(1, min(budget, output_cap_raw))
    if span <= per_window_cap:
        return [(start, span)], []
    reasons = ["duration_limit"] if span > budget else ["sample_limit"]
    # Three views make long events inspectable without exceeding the same
    # per-event work budget. The central view follows the detector's peak.
    views = min(3, budget)
    each = max(1, min(per_window_cap, budget // views))
    placements = [start, max(start, min(end - each, peak - each // 2)), end - each][:views]
    windows: List[Tuple[int, int]] = []
    for first in placements:
        item = (max(start, first), each)
        if item not in windows:
            windows.append(item)
    return windows, reasons


def analyze_events(meta: Dict[str, Any], events: Sequence[Dict[str, Any]],
                   max_samples: int = MAX_SAMPLES, duration_seconds: float = 2.0
                   ) -> List[Dict[str, Any]]:
    """Attach bounded, time-aware signal analysis to each event.

    At most ``duration_seconds`` of source IQ is read per event, and each
    channelized window stays below ``max_samples`` complex samples. Long events
    are sampled at the start, detector peak, and end within that work budget.
    """
    try:
        output_limit = max(1, min(int(max_samples), MAX_SAMPLES))
    except (TypeError, ValueError, OverflowError):
        output_limit = MAX_SAMPLES
    try:
        duration_limit = float(duration_seconds)
        if not math.isfinite(duration_limit) or duration_limit <= 0:
            raise ValueError("analysis duration must be positive and finite")
        path = Path(str(meta.get("input", "")))
        fs = float(meta.get("sample_rate"))
        fmt = str(meta.get("format", ""))
        data_offset = int(meta.get("data_offset", 0))
        if data_offset < 0 or not _finite(fs) or fs <= 0:
            raise ValueError("recording metadata is invalid")
        reader_fmt = "cs16" if fmt == "wav" else fmt
        bpc = {"cs8": 2, "cu8": 2, "cs16": 4,
               "cf32_le": 8, "cf32_be": 8}.get(reader_fmt)
        if bpc is None:
            raise ValueError("unsupported recording format")
        declared = meta.get("samples")
        if declared is None and meta.get("bytes") is not None:
            declared = int(meta["bytes"]) // bpc
        actual = max(0, (path.stat().st_size - data_offset) // bpc) if path.is_file() else 0
        total = min(actual, int(declared)) if declared is not None else actual
        if not path.is_file():
            raise ValueError("recording is unavailable")
        if total <= 0:
            raise ValueError("recording has no complete IQ samples")
        reader_meta = dict(meta, format=reader_fmt, bytes_per_complex=bpc, samples=total)
        # Work is bounded in both time and source samples, including implausible
        # metadata rates or an overly large caller-selected duration.
        work_limited = duration_limit * fs > 12_000_000
        budget = max(1, min(int(duration_limit * fs), 12_000_000))
    except (AttributeError, TypeError, ValueError, OverflowError, OSError) as exc:
        setup_error = str(exc)
    else:
        setup_error = None
    out_events: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            if setup_error is not None:
                raise ValueError(setup_error)
            center = float(event.get("center_offset_hz", 0.0))
            peak_t = float(event.get("peak_time_s", event.get("start_s", 0.0)))
            start_t = float(event.get("start_s", peak_t))
            end_t = float(event.get("end_s", peak_t))
            if not all(math.isfinite(x) for x in (center, peak_t, start_t, end_t)) or end_t < start_t:
                raise ValueError("event timing is invalid")
            event_len = end_t - start_t
            context = max(0.010, min(0.25, event_len * .5 + .02))
            start = max(0, min(total, int(math.floor((start_t - context) * fs))))
            end = max(start, min(total, int(math.ceil((end_t + context) * fs))))
            peak = max(start, min(end, int(round(peak_t * fs))))
            decimation, output_fs, _ = _channel_plan(fs, event.get("bandwidth_hz"))
            windows, truncation = _event_windows(start, end, peak, budget,
                                                  output_limit * decimation)
            if work_limited and end - start > budget:
                truncation.append("work_limit")
            results: List[Dict[str, Any]] = []
            provenance: List[Dict[str, Any]] = []
            for first, count in windows:
                channel, analysis_fs, warnings = _stream_channelize(
                    reader_meta, first, count, center, event.get("bandwidth_hz"), output_limit)
                window_reasons = list(truncation)
                if warnings:
                    window_reasons.extend("eof" if "ended" in x else "sample_limit" for x in warnings)
                window = {"start_s": first / fs, "end_s": (first + count) / fs,
                          "sample_rate_hz": analysis_fs, "n_samples": len(channel),
                          "source_samples": count,
                          "truncation_reasons": list(dict.fromkeys(window_reasons))}
                if _finite(meta.get("scan_start_s")):
                    origin = float(meta["scan_start_s"])
                    window["absolute_start_s"] = origin + window["start_s"]
                    window["absolute_end_s"] = origin + window["end_s"]
                provenance.append(window)
                result = analyze_samples(channel, analysis_fs,
                                         analysis_bandwidth_hz=event.get("bandwidth_hz"))
                if warnings:
                    result["warnings"] = warnings + list(result.get("warnings", []))
                results.append(result)
            if not results:
                raise ValueError("event has too few samples")
            # A CRC-confirmed frame is authoritative only for the window that
            # decoded it. Heuristic candidates otherwise use the strongest
            # analyzed window; candidate sets are never merged into a false
            # composite protocol claim.
            confirmed = [r for r in results if r["protocol"]["status"] == "confirmed"]
            if confirmed:
                result = confirmed[0]
            else:
                result = max(results, key=lambda r: (r["status"] == "candidate",
                    r.get("features", {}).get("spectral_peak_excess_db", -1)))
            features = result.setdefault("features", {})
            features["analysis_windows"] = provenance
            features["analysis_duration_limit_s"] = duration_limit
            features["truncation_reasons"] = list(dict.fromkeys(
                reason for window in provenance for reason in window["truncation_reasons"]))
            if len(windows) > 1:
                result["warnings"] = ["long event analyzed in bounded windows"] + list(result["warnings"])
        except Exception as exc:
            result = _empty([str(exc)])
        event["signal_analysis"] = result
        out_events.append(event)
    return out_events


__all__ = ["MAX_SAMPLES", "analyze_samples", "analyze_events"]
