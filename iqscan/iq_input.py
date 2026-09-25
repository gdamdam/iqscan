"""Input metadata, bounded IQ reads, and exact source checks for iqscan."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import numpy as np

from .iq_wav import read_header


_FORMATS = {
    "cs8": (np.dtype("i1"), 2),
    "cs16": (np.dtype("<i2"), 4),
    "cu8": (np.dtype("u1"), 2),
    "cf32_le": (np.dtype("<f4"), 8),
    "cf32_be": (np.dtype(">f4"), 8),
}
_SIGMF_TYPES = {
    "ci8": "cs8",
    "ci16_le": "cs16",
    "cu8": "cu8",
    "cf32_le": "cf32_le",
    "cf32_be": "cf32_be",
}
_FORMAT_TO_SIGMF = {value: key for key, value in _SIGMF_TYPES.items()}
_HASH_CHUNK = 1024 * 1024
_SAMPLE_BLOCKS = 16


def _positive_finite(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be positive and finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be positive and finite") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return number


def _finite_number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


_MULTIPLIERS = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}


def _filename_number(name, suffix):
    """Read e.g. 137900000Hz, 137900kHz, 2.4MSPS; SI prefixes are case-insensitive."""
    match = re.search(r"(\d+(?:\.\d+)?)([kmg]?)" + suffix, name, re.I)
    return float(match[1]) * _MULTIPLIERS[match[2].lower()] if match else None


def format_rate(rate):
    """Plain decimal sample rate for filenames and text; never exponent notation."""
    rate = float(rate)
    return str(int(rate)) if rate.is_integer() else f"{rate:.3f}".rstrip("0").rstrip(".")


def shell_quote(text):
    """Quote one argument for this platform's shell (cmd.exe on Windows)."""
    return subprocess.list2cmdline([str(text)]) if os.name == "nt" else shlex.quote(str(text))


def open_command(path):
    """Command that opens a file with the default application on this platform."""
    if sys.platform == "darwin":
        return "open " + shell_quote(path)
    if os.name == "nt":
        return 'start "" ' + shell_quote(path)
    return "xdg-open " + shell_quote(path)


def _sigmf_paths(path):
    name = path.name.lower()
    if name.endswith(".sigmf-meta"):
        return path, path.with_name(path.name[:-len(".sigmf-meta")] + ".sigmf-data")
    if name.endswith(".sigmf-data"):
        return path.with_name(path.name[:-len(".sigmf-data")] + ".sigmf-meta"), path
    sidecar = path.with_name(path.name + ".sigmf-meta")
    if sidecar.is_file():
        return sidecar, path
    return None, None


def _sigmf_header(meta_path):
    try:
        document = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid SigMF metadata: {meta_path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("global"), dict):
        raise ValueError("SigMF metadata requires a global object")
    global_meta = document["global"]
    datatype = global_meta.get("core:datatype")
    if datatype not in _SIGMF_TYPES:
        raise ValueError(f"Unsupported SigMF core:datatype: {datatype!r}")
    if "core:offset" in global_meta and (type(global_meta["core:offset"]) is not int or global_meta["core:offset"] != 0):
        raise ValueError("SigMF core:offset is unsupported")
    if type(global_meta.get("core:num_channels", 1)) is not int or global_meta.get("core:num_channels", 1) != 1:
        raise ValueError("SigMF multiple interleaved channels are unsupported")
    if type(global_meta.get("core:trailing_bytes", 0)) is not int or global_meta.get("core:trailing_bytes", 0) != 0:
        raise ValueError("SigMF trailing bytes are unsupported")
    captures = document.get("captures", [])
    if not isinstance(captures, list) or len(captures) > 1:
        raise ValueError("SigMF multiple captures are unsupported")
    capture = captures[0] if captures else {}
    if (not isinstance(capture, dict) or (captures and type(capture.get("core:sample_start")) is not int)
            or capture.get("core:sample_start", 0) != 0):
        raise ValueError("SigMF capture sample offsets are unsupported")
    if type(capture.get("core:header_bytes", 0)) is not int or capture.get("core:header_bytes", 0) != 0:
        raise ValueError("SigMF capture header bytes are unsupported")
    dataset = global_meta.get("core:dataset")
    if dataset is not None:
        if (not isinstance(dataset, str) or not dataset or dataset in (".", "..")
                or Path(dataset).name != dataset or "\\" in dataset):
            raise ValueError("SigMF core:dataset must be a filename in the metadata directory")
    return _SIGMF_TYPES[datatype], global_meta, capture


def _check_float_payload(path, offset, size, dtype):
    """Inspect every float component with bounded memory, including the final chunk."""
    with path.open("rb") as handle:
        handle.seek(offset)
        remaining = size
        while remaining:
            chunk = handle.read(min(remaining, _HASH_CHUNK))
            if not chunk:
                raise ValueError("IQ payload is truncated")
            if not np.isfinite(np.frombuffer(chunk, dtype=dtype)).all():
                raise ValueError("IQ float payload contains non-finite values")
            remaining -= len(chunk)


def _sigmf_semantic_sha256(path):
    """Ignore the required dataset filename change when a recording is renamed."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    document = dict(document)
    document["global"] = dict(document["global"])
    document["global"].pop("core:dataset", None)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_metadata(path, format=None, sample_rate=None, center_frequency=None, iq_order="IQ"):
    """Return canonical metadata for raw, WAV, or single-capture SigMF IQ."""
    path = Path(path).expanduser().resolve()
    if iq_order not in ("IQ", "QI"):
        raise ValueError("iq_order must be IQ or QI")
    if format == "cf32":
        format = "cf32_le"
    sigmf_meta, data_path = _sigmf_paths(path)
    wav = None
    sigmf_global = sigmf_capture = None
    if sigmf_meta is not None:
        fmt, sigmf_global, sigmf_capture = _sigmf_header(sigmf_meta)
        dataset = sigmf_global.get("core:dataset")
        if dataset is not None:
            resolved_data = sigmf_meta.with_name(dataset)
            if path.suffix.lower() != ".sigmf-meta" and resolved_data != data_path:
                raise ValueError("SigMF core:dataset conflicts with the data path")
            data_path = resolved_data
        elif data_path.suffix.lower() != ".sigmf-data":
            raise ValueError("Nonstandard SigMF data requires core:dataset")
        if format is not None and format != fmt:
            raise ValueError("--format conflicts with SigMF core:datatype")
        path = data_path
        container = "SigMF"
        offset = 0
        warning = None
        header_rate = sigmf_global.get("core:sample_rate")
        if header_rate is None:
            raise ValueError("SigMF global core:sample_rate is required")
        header_center = sigmf_capture.get("core:frequency")
    else:
        with path.open("rb") as handle:
            signature = handle.read(12)
        if path.suffix.lower() == ".wav" or (signature[:4] in (b"RIFF", b"RF64") and signature[8:] == b"WAVE"):
            wav = read_header(path)
            fmt = wav["format"]
            if format is not None and format != fmt:
                raise ValueError("--format conflicts with WAV payload")
            container, offset, warning = wav["container"], wav["data_offset"], wav.get("warning")
            header_rate, header_center = wav["sample_rate"], wav.get("center_frequency")
        else:
            fmt = format or path.suffix.lower().lstrip(".")
            if fmt == "cf32":
                fmt = "cf32_le"
            if fmt not in _FORMATS:
                raise ValueError("Use .cs8, .cs16, .cu8, .cf32, .cf32_le, .cf32_be, SigMF, or IQ WAV")
            container, offset, warning = "raw", 0, None
            header_rate = header_center = None
    if fmt not in _FORMATS:
        raise ValueError(f"Unsupported IQ format: {fmt}")
    if header_rate is not None:
        header_rate = _positive_finite(header_rate, "Sample rate")
        if sample_rate is not None and _positive_finite(sample_rate, "Sample rate") != header_rate:
            raise ValueError("--sample-rate conflicts with recording metadata")
    rate = header_rate if header_rate is not None else sample_rate
    if rate is None:
        rate = _filename_number(path.name, "SPS")
    if rate is None:
        raise ValueError("Sample rate missing: provide --sample-rate 500000 (complex samples/sec)")
    rate = _positive_finite(rate, "Sample rate")
    if header_center is not None:
        header_center = _finite_number(header_center, "Center frequency")
        if center_frequency is not None and _finite_number(center_frequency, "Center frequency") != header_center:
            raise ValueError("--center-frequency conflicts with recording metadata")
    center = header_center if header_center is not None else center_frequency
    if center is None:
        center = _filename_number(path.name, "Hz")
    if center is not None:
        center = _finite_number(center, "Center frequency")
    dtype, bpc = _FORMATS[fmt]
    size = wav["bytes"] if wav else path.stat().st_size
    if size == 0 or size % bpc:
        raise ValueError("File is empty or ends with an incomplete IQ sample")
    if offset + size > path.stat().st_size:
        raise ValueError("IQ payload is truncated")
    if fmt.startswith("cf32"):
        _check_float_payload(path, offset, size, dtype)
    duration = (size // bpc) / rate
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Recording duration must be positive and finite")
    result = dict(input=str(path), data_offset=offset, container=container,
                  input_warning=warning, format=fmt, sample_rate=rate,
                  center_frequency_hz=center, bytes=size, bytes_per_complex=bpc,
                  samples=size // bpc, duration_s=duration, iq_order=iq_order)
    if sigmf_meta is not None:
        result["sigmf_meta"] = str(sigmf_meta)
    return result


def read_samples(meta, first, count):
    """Read a bounded interval as normalized complex128, respecting IQ/QI order."""
    if type(first) is not int or type(count) is not int or first < 0 or count < 0:
        raise ValueError("Sample window must use nonnegative integer indices")
    if first > meta["samples"]:
        raise ValueError("Sample window starts past the payload")
    count = min(count, meta["samples"] - first)
    if not count:
        return np.empty(0, dtype=np.complex128)
    fmt = meta["format"]
    if fmt not in _FORMATS or meta.get("iq_order", "IQ") not in ("IQ", "QI"):
        raise ValueError("Unsupported IQ format or component order")
    dtype, bpc = _FORMATS[fmt]
    if meta["bytes_per_complex"] != bpc:
        raise ValueError("Inconsistent IQ byte width")
    with Path(meta["input"]).open("rb") as handle:
        handle.seek(meta.get("data_offset", 0) + first * bpc)
        raw = handle.read(count * bpc)
    if len(raw) != count * bpc:
        raise ValueError("IQ payload is truncated")
    pairs = np.frombuffer(raw, dtype=dtype).reshape(-1, 2).astype(np.float64)
    if not np.isfinite(pairs).all():
        raise ValueError("IQ float payload contains non-finite values")
    if meta.get("iq_order", "IQ") == "QI":
        pairs = pairs[:, ::-1]
    if fmt == "cs8":
        pairs /= 128.0
    elif fmt == "cs16":
        pairs /= 32768.0
    elif fmt == "cu8":
        pairs = (pairs - 127.5) / 127.5
    return pairs[:, 0] + 1j * pairs[:, 1]


def fingerprint(meta, full=False):
    """Hash the selected IQ payload (sampled unless ``full``) and record its layout."""
    fmt = meta["format"]
    if fmt not in _FORMATS or meta.get("iq_order", "IQ") not in ("IQ", "QI"):
        raise ValueError("Unsupported IQ format or component order")
    bpc = _FORMATS[fmt][1]
    offset = meta.get("data_offset", 0)
    size = meta["bytes"]
    if (type(offset) is not int or offset < 0 or type(size) is not int or size <= 0
            or size % bpc or meta["samples"] != size // bpc or meta["bytes_per_complex"] != bpc):
        raise ValueError("Inconsistent IQ payload layout")
    path = Path(meta["input"])
    if offset + size > path.stat().st_size:
        raise ValueError("IQ payload is truncated")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block_offset, block_size in _sample_blocks(offset, size, full):
            handle.seek(block_offset)
            remaining = block_size
            while remaining:
                chunk = handle.read(min(remaining, _HASH_CHUNK))
                if not chunk:
                    raise ValueError("IQ payload is truncated")
                digest.update(chunk)
                remaining -= len(chunk)
    layout = {key: meta.get(key) for key in ("format", "sample_rate", "center_frequency_hz",
                                                "iq_order", "container", "data_offset",
                                                "bytes_per_complex", "bytes", "samples")}
    if meta.get("sigmf_meta"):
        layout["sigmf_sha256"] = _sigmf_semantic_sha256(meta["sigmf_meta"])
    if meta.get("container") in ("RIFF", "RF64"):
        header_digest = hashlib.sha256()
        with path.open("rb") as handle:
            remaining = read_header(path)["data_offset"]
            while remaining:
                chunk = handle.read(min(remaining, _HASH_CHUNK))
                if not chunk:
                    raise ValueError("WAV header is truncated")
                header_digest.update(chunk)
                remaining -= len(chunk)
        layout["header_sha256"] = header_digest.hexdigest()
    return {**layout, ("sha256" if full else "sampled_sha256"): digest.hexdigest()}


def _sample_blocks(offset, size, full):
    """Whole payload, or 16 evenly spaced 1 MiB blocks (first and last included).

    A full hash costs a second read of the recording before any FFT runs; the
    sampled hash still catches truncation, overwrite and relocation to a
    different file, which is what the cache check exists for.
    """
    if full or size <= _SAMPLE_BLOCKS * _HASH_CHUNK:
        return [(offset, size)]
    step = (size - _HASH_CHUNK) / (_SAMPLE_BLOCKS - 1)
    return [(offset + round(i * step), _HASH_CHUNK) for i in range(_SAMPLE_BLOCKS)]


def relocated_source(meta, path):
    """Return metadata pointed at a relocated data file or SigMF metadata file."""
    path = Path(path).expanduser().resolve()
    moved = dict(meta)
    sigmf_meta, data_path = _sigmf_paths(path)
    if sigmf_meta is not None:
        _, global_meta, _ = _sigmf_header(sigmf_meta)
        if global_meta.get("core:dataset") is not None:
            described_data = sigmf_meta.with_name(global_meta["core:dataset"])
            if path.suffix.lower() != ".sigmf-meta" and path != described_data:
                raise ValueError("SigMF core:dataset conflicts with the data path")
            data_path = described_data
        moved["input"] = str(data_path)
        moved["sigmf_meta"] = str(sigmf_meta)
    else:
        moved["input"] = str(path)
        moved.pop("sigmf_meta", None)
    return moved


def write_sigmf(path, sample_rate, center_frequency, datatype, iq_order="IQ", annotations=None):
    """Write a SigMF sidecar for an existing IQ dataset and return its path."""
    path = Path(path).expanduser().resolve()
    if iq_order != "IQ":
        raise ValueError("SigMF complex datasets require I then Q component order")
    if datatype == "cf32":
        datatype = "cf32_le"
    datatype = _FORMAT_TO_SIGMF.get(datatype, datatype)
    if datatype not in _SIGMF_TYPES:
        raise ValueError(f"Unsupported SigMF core:datatype: {datatype!r}")
    rate = _positive_finite(sample_rate, "Sample rate")
    center = None if center_frequency is None else _finite_number(center_frequency, "Center frequency")
    if annotations is None:
        annotations = []
    if not isinstance(annotations, list) or not all(isinstance(item, dict) for item in annotations):
        raise ValueError("SigMF annotations must be a list of objects")
    if not path.is_file():
        raise ValueError("SigMF dataset file is missing")
    fmt = _SIGMF_TYPES[datatype]
    size = path.stat().st_size
    if not size or size % _FORMATS[fmt][1]:
        raise ValueError("SigMF dataset is empty or ends with an incomplete IQ sample")
    if path.name.lower().endswith(".sigmf-data"):
        meta_path = path.with_name(path.name[:-len(".sigmf-data")] + ".sigmf-meta")
        global_meta = {"core:datatype": datatype, "core:sample_rate": rate, "core:version": "1.2.0"}
    else:
        meta_path = path.with_name(path.name + ".sigmf-meta")
        global_meta = {"core:datatype": datatype, "core:sample_rate": rate,
                       "core:version": "1.2.0", "core:dataset": path.name}
    capture = {"core:sample_start": 0}
    if center is not None:
        capture["core:frequency"] = center
    document = {"global": global_meta, "captures": [capture], "annotations": annotations}
    meta_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return meta_path


def verify_source(meta, path=None):
    """Check a current or relocated source against meta['source_fingerprint']."""
    expected = meta.get("source_fingerprint")
    if not isinstance(expected, dict):
        return False
    try:
        candidate = relocated_source(meta, path) if path is not None else dict(meta)
        source = candidate.get("sigmf_meta", candidate["input"])
        actual = read_metadata(source, format=candidate["format"], sample_rate=candidate["sample_rate"],
                               center_frequency=candidate["center_frequency_hz"],
                               iq_order=candidate.get("iq_order", "IQ"))
        for key in ("format", "sample_rate", "center_frequency_hz", "container", "bytes_per_complex"):
            if actual[key] != candidate[key]:
                return False
        if candidate["data_offset"] < actual["data_offset"] or candidate["data_offset"] + candidate["bytes"] > actual["data_offset"] + actual["bytes"]:
            return False
        # Schema-2 caches written before 1.5.0 hold a full-payload hash; honour it.
        return fingerprint(candidate, full="sha256" in expected) == expected
    except (OSError, ValueError, TypeError, KeyError):
        return False
