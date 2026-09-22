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
    from scipy.signal import filtfilt, firwin
except Exception:  # pragma: no cover - exercised only in minimal installations
    filtfilt = firwin = None


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
        from protocol_decode import decode_ax25_afsk  # type: ignore
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
    if fmt == "cs8":
        dtype, bytes_per_complex, scale = np.dtype("i1"), 2, 128.0
    elif fmt == "cs16":
        dtype, bytes_per_complex, scale = np.dtype("<i2"), 4, 32768.0
    else:
        raise ValueError("unsupported IQ format")
    if count <= 0:
        return np.empty(0, dtype=np.complex128)
    with path.open("rb") as handle:
        handle.seek(max(0, int(offset)))
        raw = handle.read(int(count) * bytes_per_complex)
    usable = (len(raw) // bytes_per_complex) * bytes_per_complex
    if usable <= 0:
        return np.empty(0, dtype=np.complex128)
    pairs = np.frombuffer(raw[:usable], dtype=dtype).reshape(-1, 2).astype(np.float64)
    return (pairs[:, 0] + 1j * pairs[:, 1]) / scale


def _read_wav_iq(path: Path, data_offset: int, count: int) -> np.ndarray:
    # Avoid wave.open's full-data read and honor the explicit bounded data offset.
    return _read_raw(path, "cs16", data_offset, count)


def _channelize(samples: np.ndarray, fs: float, center_hz: float, bandwidth_hz: Optional[float]) -> Tuple[np.ndarray, float, List[str]]:
    """Mix an event to zero and FIR-filter it with a guarded event bandwidth."""
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


def analyze_events(meta: Dict[str, Any], events: Sequence[Dict[str, Any]], max_samples: int = MAX_SAMPLES) -> List[Dict[str, Any]]:
    """Attach bounded signal analysis to each event and return the same list.

    ``meta`` follows ``iq_scan.metadata``: ``input``, ``format``, ``data_offset``
    and ``sample_rate`` are used.  Event samples are centered around
    ``peak_time_s`` and mixed by ``center_offset_hz`` before guarded FIR filtering.
    Missing files and malformed events become ``unknown`` results with warnings.
    """
    try:
        limit = max(1, min(int(max_samples), MAX_SAMPLES))
    except (TypeError, ValueError):
        limit = MAX_SAMPLES
    try:
        path = Path(str(meta.get("input", "")))
        fs = float(meta.get("sample_rate"))
        fmt = str(meta.get("format", ""))
        data_offset = int(meta.get("data_offset", 0))
        if data_offset < 0:
            raise ValueError("recording data offset is invalid")
    except (AttributeError, TypeError, ValueError):
        path, fs, fmt, data_offset = Path(""), float("nan"), "", 0
    out_events: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        warnings: List[str] = []
        try:
            center = float(event.get("center_offset_hz", 0.0))
            peak_t = float(event.get("peak_time_s", event.get("start_s", 0.0)))
            start_t = float(event.get("start_s", peak_t))
            end_t = float(event.get("end_s", peak_t))
            if not all(math.isfinite(x) for x in (center, peak_t, start_t, end_t)):
                raise ValueError("event timing is non-finite")
            if not path.is_file() or not _finite(fs) or fs <= 0:
                raise ValueError("recording is unavailable")
            # Include context around the event, but bound the total read. One
            # event cannot cause the full recording to be loaded.
            event_len = max(0.0, end_t - start_t)
            context = max(0.010, min(0.25, event_len * .5 + .02))
            count = min(limit, max(256, int(round((event_len + 2 * context) * fs))))
            half = count // 2
            peak_index = max(0, int(round(peak_t * fs)))
            first = max(0, peak_index - half)
            # Metadata from iq_scan includes payload bytes and sample count.
            # Clamp the window before opening the file so a WAV trailing chunk
            # can never be interpreted as IQ when an event reaches EOF.
            bpc = 2 if fmt == "cs8" else 4
            declared_samples = meta.get("samples")
            if declared_samples is None and meta.get("bytes") is not None:
                declared_samples = int(meta["bytes"]) // bpc
            if declared_samples is not None:
                declared_samples = int(declared_samples)
                if declared_samples <= 0:
                    raise ValueError("recording has no complete IQ samples")
                first = min(first, declared_samples)
                count = min(count, max(0, declared_samples - first))
            if fmt in ("cs8", "cs16"):
                raw = _read_raw(path, fmt, data_offset + first * (2 if fmt == "cs8" else 4), count)
            elif path.suffix.lower() == ".wav":
                raw = _read_wav_iq(path, data_offset + first * 4, count)
            else:
                raise ValueError("unsupported recording format")
            if len(raw) < 256:
                raise ValueError("event has too few samples")
            raw, analysis_fs, filter_warnings = _channelize(raw, fs, center, event.get("bandwidth_hz"))
            warnings.extend(filter_warnings)
            result = analyze_samples(raw, analysis_fs, analysis_bandwidth_hz=event.get("bandwidth_hz"))
            if warnings:
                result["warnings"] = warnings + list(result.get("warnings", []))
        except Exception as exc:
            result = _empty([str(exc)])
        event["signal_analysis"] = result
        out_events.append(event)
    return out_events


__all__ = ["MAX_SAMPLES", "analyze_samples", "analyze_events"]
