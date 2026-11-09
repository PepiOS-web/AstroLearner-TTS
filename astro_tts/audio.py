"""Lossless float WAV storage and optional FFmpeg two-pass loudness mastering.

NumPy and SoundFile are deliberately imported inside audio operations, so the
desktop UI and diagnostics can start before the synthesis environment is ready.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence


class AudioError(RuntimeError):
    pass


def mono_waveform(value: Any):
    import numpy as np

    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    wave = np.asarray(value, dtype=np.float32)
    if wave.ndim == 2 and 1 in wave.shape:
        wave = wave.reshape(-1)
    elif wave.ndim == 2 and wave.shape[-1] <= 2:
        wave = wave.mean(axis=-1, dtype=np.float32)
    if wave.ndim != 1 or wave.size == 0:
        raise AudioError("El modelo no devolvió una onda de audio mono válida.")
    if not np.isfinite(wave).all():
        raise AudioError("El modelo devolvió valores de audio no finitos.")
    return wave


def write_float_wav(path: Path, waveform: Any, sample_rate: int) -> dict[str, Any]:
    import numpy as np
    import soundfile as sf

    if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
        raise AudioError("Frecuencia de muestreo no válida.")
    wave = mono_waveform(waveform)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # FLOAT retains the model output, including any peaks above 0 dBFS. There is
    # no integer conversion, clipping, resampling, speed change or normalization.
    sf.write(str(path), wave, sample_rate, subtype="FLOAT", format="WAV")
    peak = float(np.max(np.abs(wave)))
    return {
        "audio_path": str(path.resolve()),
        "sample_rate": sample_rate,
        "frames": int(wave.size),
        "duration_seconds": float(wave.size / sample_rate),
        "peak_amplitude": peak,
        "peak_dbfs": 20 * math.log10(peak) if peak > 0 else None,
        "subtype": "FLOAT",
    }


def wav_info(path: Path) -> dict[str, Any]:
    import soundfile as sf

    info = sf.info(str(path))
    return {
        "audio_path": str(Path(path).resolve()),
        "sample_rate": int(info.samplerate),
        "duration_seconds": float(info.frames / info.samplerate),
        "frames": int(info.frames),
        "subtype": info.subtype,
    }


def assemble_wav(
    segments: Sequence[dict[str, Any]],
    destination: Path,
    sample_rate: int,
) -> dict[str, Any]:
    """Stream original chunks plus explicit gaps; times mark chunk boundaries."""
    import numpy as np
    import soundfile as sf

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames_written = 0
    with sf.SoundFile(str(destination), mode="w", samplerate=sample_rate, channels=1, subtype="FLOAT") as output:
        for segment in segments:
            wave, rate = sf.read(segment["audio_path"], dtype="float32", always_2d=False)
            if int(rate) != sample_rate:
                raise AudioError("El modelo cambió de frecuencia entre fragmentos; no se ha remuestreado el original.")
            wave = mono_waveform(wave)
            segment["start_seconds"] = frames_written / sample_rate
            output.write(wave)
            frames_written += len(wave)
            segment["end_seconds"] = frames_written / sample_rate
            gap_frames = round(float(segment.get("gap_after_seconds", 0)) * sample_rate)
            if gap_frames:
                output.write(np.zeros(gap_frames, dtype=np.float32))
                frames_written += gap_frames
    return wav_info(destination)


def _run_ffmpeg(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


def master_wav(source: Path, destination: Path, ffmpeg: Path) -> dict[str, Any]:
    """Produce a separate 48 kHz / PCM24 master at -16 LUFS and -1.5 dBTP.

    The original is never replaced. No tempo or duration fitting is applied.
    """
    ffmpeg = Path(ffmpeg)
    if not ffmpeg.is_file():
        raise AudioError("FFmpeg no está disponible; se conserva el WAV original.")
    original = wav_info(source)
    timeout = max(60.0, original["duration_seconds"] * 3 + 30)
    common = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-i", str(source)]
    first = _run_ffmpeg(
        common + ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", os.devnull],
        timeout,
    )
    measurements = None
    if first.returncode == 0:
        for block in reversed(re.findall(r"\{[^{}]*\}", first.stderr, flags=re.DOTALL)):
            try:
                value = json.loads(block)
                if all(key in value for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
                    measurements = value
                    break
            except ValueError:
                continue
    if measurements is None:
        raise AudioError("FFmpeg no pudo medir la sonoridad; se conserva el WAV original.")
    try:
        numeric = {key: float(measurements[key]) for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (TypeError, ValueError) as exc:
        raise AudioError("FFmpeg devolvió una medición de sonoridad no válida.") from exc
    if not all(math.isfinite(value) for value in numeric.values()):
        raise AudioError("La medición de sonoridad no es válida (audio silencioso o insuficiente).")
    loudnorm = (
        "loudnorm=I=-16:TP=-1.5:LRA=11:"
        f"measured_I={numeric['input_i']}:measured_TP={numeric['input_tp']}:"
        f"measured_LRA={numeric['input_lra']}:measured_thresh={numeric['input_thresh']}:"
        f"offset={numeric['target_offset']}:linear=true:print_format=summary"
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp.wav")
    try:
        second = _run_ffmpeg(
            common + ["-af", loudnorm, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s24le", str(temporary)],
            timeout,
        )
        if second.returncode or not temporary.is_file():
            raise AudioError("FFmpeg no pudo crear el máster; se conserva el WAV original.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        **wav_info(destination),
        "mastering": "two_pass_loudnorm",
        "target_lufs": -16.0,
        "target_true_peak_dbtp": -1.5,
        "input_measurements": numeric,
    }
