"""Bounded, local-only decoding of uploaded voice references; no speech models."""

from __future__ import annotations

import hashlib
import math
import shutil
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

from . import audio

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_UPLOAD_BYTES + 128 * 1024
MIN_SECONDS = 5.0
MAX_SECONDS = 30.0
FORMATS = {".mp3": "mp3", ".wav": "wav", ".flac": "flac", ".m4a": "mov", ".ogg": "ogg"}


def validate_settings(text: str, start: float, duration: float) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("La transcripción exacta debe contener entre 1 y 500 caracteres.")
    if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
        raise ValueError("El inicio debe ser un número de segundos finito mayor o igual a cero.")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not MIN_SECONDS <= duration <= MAX_SECONDS):
        raise ValueError("La referencia debe durar entre 5 y 30 segundos.")


def upload_filename(value: str | None) -> tuple[str, str]:
    name = Path(PureWindowsPath(value or "").name).name
    suffix = Path(name).suffix.lower()
    if not name or suffix not in FORMATS:
        raise ValueError("Elige un archivo MP3, WAV, FLAC, M4A u OGG.")
    return name[:200], suffix


def valid_header(header: bytes, suffix: str) -> bool:
    if suffix == ".wav":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    if suffix == ".mp3":
        return header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)
    if suffix == ".flac":
        return header.startswith(b"fLaC")
    if suffix == ".ogg":
        return header.startswith(b"OggS")
    if suffix == ".m4a":
        return len(header) >= 12 and header[4:8] == b"ftyp"
    return False


def staging_path(runtime: Path, path: Path, *, exists: bool = True) -> Path:
    runtime = Path(runtime).resolve()
    incoming = runtime / "incoming"
    if incoming.resolve() != incoming:
        raise ValueError("La carpeta temporal de referencias no es válida.")
    path = Path(path).resolve(strict=exists)
    if path.parent.parent != incoming or path.name != "source" + path.suffix.lower() or path.suffix.lower() not in FORMATS:
        raise ValueError("La referencia temporal no pertenece a este estudio.")
    try:
        if uuid.UUID(path.parent.name).hex != path.parent.name:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("El identificador temporal de la referencia no es válido.") from exc
    if exists and not path.is_file():
        raise ValueError("No se encuentra el archivo temporal de referencia.")
    return path


def cleanup_upload(runtime: Path, path: str | Path | None) -> None:
    if path is None:
        return
    try:
        validated = staging_path(runtime, Path(path), exists=False)
        validated.unlink(missing_ok=True)
        try:
            validated.parent.rmdir()
        except OSError:
            pass  # Never recursively remove an unexpected file or directory.
    except (OSError, ValueError, TypeError):
        pass


def prepare_reference(runtime: Path, source: Path, job_dir: Path, text: str,
                      start: float, duration: float, original_filename: str) -> dict[str, Any]:
    """Preserve the input, decode just the selected segment and measure its WAV."""
    import numpy as np
    import soundfile as sf

    validate_settings(text, start, duration)
    source = staging_path(runtime, source)
    size = source.stat().st_size
    if not 0 < size <= MAX_UPLOAD_BYTES:
        raise ValueError("El archivo de referencia debe ocupar entre 1 byte y 25 MiB.")
    with source.open("rb") as handle:
        if not valid_header(handle.read(64), source.suffix.lower()):
            raise ValueError("El contenido del archivo no coincide con su formato de audio.")
    ffmpeg = Path(runtime) / "bin" / "ffmpeg.exe"
    if not ffmpeg.is_file():
        raise audio.AudioError("FFmpeg es necesario para importar una referencia de voz.")
    preserved = job_dir / ("source" + source.suffix.lower())
    shutil.copy2(source, preserved)
    digest = hashlib.sha256(preserved.read_bytes()).hexdigest()
    destination = job_dir / "original.wav"
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-y",
               "-protocol_whitelist", "file,pipe", "-ss", str(start), "-f", FORMATS[source.suffix.lower()],
               "-i", str(preserved), "-t", str(duration), "-map", "0:a:0", "-vn", "-sn", "-dn",
               "-ac", "1", "-ar", "24000", "-c:a", "pcm_f32le", str(destination)]
    decoded = audio._run_ffmpeg(command, timeout=60.0)
    if decoded.returncode != 0 or not destination.is_file():
        raise audio.AudioError("No se pudo decodificar el fragmento. Comprueba el archivo y el punto de inicio.")
    wave, rate = sf.read(destination, dtype="float32", always_2d=False)
    wave = audio.mono_waveform(wave)
    actual_duration = len(wave) / rate
    if int(rate) != 24000 or actual_duration < MIN_SECONDS - 0.02 or actual_duration > MAX_SECONDS + 0.02:
        raise audio.AudioError("El fragmento disponible debe tener entre 5 y 30 segundos de audio.")
    if actual_duration < duration - 0.1:
        raise audio.AudioError("El archivo termina antes del fragmento elegido. Reduce la duración o adelanta el inicio.")
    peak = float(np.max(np.abs(wave)))
    rms = float(np.sqrt(np.mean(np.square(wave.astype(np.float64)))))
    if peak <= 1e-5 or rms <= 1e-5:
        raise audio.AudioError("El fragmento está vacío o prácticamente silencioso. Elige una parte con voz clara.")
    clipped = float(np.mean(np.abs(wave) >= 0.999))
    warnings = []
    if clipped > 0.001:
        warnings.append("La referencia contiene picos al límite digital; conviene una grabación sin saturación.")
    if rms < 0.003:
        warnings.append("La referencia tiene un nivel bajo. Comprueba que la voz sea clara y tenga poco ruido.")
    return {
        "raw": audio.wav_info(destination), "warnings": warnings,
        "metadata": {"source": "local_authorized_reference_import", "source_filename": original_filename,
                     "source_sha256": digest, "source_bytes": size, "source_audio_path": str(preserved.resolve()),
                     "start_seconds": start, "requested_duration_seconds": duration,
                     "duration_seconds": actual_duration, "sample_rate": 24000,
                     "peak_dbfs": 20 * math.log10(peak), "rms_dbfs": 20 * math.log10(rms),
                     "clipped_sample_fraction": clipped, "processing": "segment_mono_resample_24000_no_loudness_change"},
    }
