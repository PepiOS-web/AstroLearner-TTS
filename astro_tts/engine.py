"""Local, single-job Qwen3-TTS engine for AstroLearner on an NVIDIA GPU.

Importing this module does not import Torch, Qwen, NumPy or SoundFile. Models
are loaded only from installed local directories, one model type at a time.
"""

from __future__ import annotations

import gc
import importlib.metadata
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import audio, references
from .profiles import ProfileStore, get_profile_spec, utc_now, write_json


MAX_TEXT_CHARACTERS = 18_000
MAX_CHUNK_CHARACTERS = 500
TARGET_CHUNK_CHARACTERS = 250
MODEL_DIRECTORIES = {"design": "voice-design", "narrate": "base"}
MODEL_REPOSITORIES = {
    "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "narrate": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
}
_LOGGER = logging.getLogger(__name__)
_RUNTIME_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_ABBREVIATIONS = {
    "sr", "sra", "srta", "dr", "dra", "prof", "profa", "ud", "uds", "etc", "aprox",
    "mr", "mrs", "ms", "jr", "st", "vs", "fig", "vol", "no", "e.g", "i.e", "p.ej",
}


class EngineError(RuntimeError):
    pass


class EngineBusyError(EngineError):
    pass


class JobCancelled(EngineError):
    pass


@dataclass(frozen=True)
class TextChunk:
    text: str
    paragraph_index: int
    paragraph_end: bool


def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("El texto debe ser una cadena.")
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(char) == "Cc" and char not in "\n\t" for char in text):
        raise ValueError("El texto contiene caracteres de control no admitidos.")
    paragraphs = re.split(r"\n[ \t]*\n+", text)
    return "\n\n".join(re.sub(r"\s+", " ", paragraph).strip() for paragraph in paragraphs if paragraph.strip())


def apply_pronunciations(text: str, replacements: dict[str, str] | None) -> str:
    """Apply literal complete-word substitutions once, without interpreting markup."""
    if not replacements:
        return text
    if not isinstance(replacements, dict) or len(replacements) > 100:
        raise ValueError("El diccionario de pronunciación debe contener un máximo de 100 entradas.")
    normalized: dict[str, tuple[str, str]] = {}
    for original, spoken in replacements.items():
        if not isinstance(original, str) or not isinstance(spoken, str):
            raise ValueError("Las pronunciaciones deben ser parejas de texto.")
        original = normalize_text(original)
        spoken = normalize_text(spoken)
        if not original or not spoken or len(original) > 80 or len(spoken) > 160:
            raise ValueError("Cada pronunciación necesita una palabra (máximo 80 caracteres) y su lectura (máximo 160).")
        if any(char in original + spoken for char in "<>\n"):
            raise ValueError("Las pronunciaciones admiten texto literal, sin HTML ni SSML.")
        lowered = original.casefold()
        if lowered in normalized:
            raise ValueError("Hay términos duplicados en el diccionario de pronunciación.")
        normalized[lowered] = (original, spoken)
    ordered = sorted(normalized.values(), key=lambda item: len(item[0]), reverse=True)
    alternatives = "|".join(f"(?P<p{index}>{re.escape(term)})" for index, (term, _) in enumerate(ordered))
    pattern = re.compile(r"(?<!\w)(?:" + alternatives + r")(?!\w)", re.IGNORECASE)
    return pattern.sub(lambda match: ordered[int(match.lastgroup[1:])][1], text)


def _sentences(paragraph: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for match in re.finditer(r"[.!?…]+[\"'»”’)\]]*(?:\s+|$)", paragraph):
        ending = paragraph[start:match.end()].strip()
        punctuation = match.group(0).rstrip()
        if punctuation.startswith(".") and not punctuation.startswith(".."):
            word = re.search(r"([\w.]+)\.[\"'»”’)\]]*\s*$", ending)
            if word:
                token = word.group(1)
                if token.casefold() in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
                    continue
                if re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", token):
                    continue
        if ending:
            sentences.append(ending)
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def segment_text(
    text: str,
    target: int = TARGET_CHUNK_CHARACTERS,
    maximum: int = MAX_CHUNK_CHARACTERS,
) -> list[TextChunk]:
    """Keep whole sentences and paragraph boundaries; reject oversized sentences."""
    if not 1 <= target <= maximum:
        raise ValueError("Límites de fragmentación no válidos.")
    normalized = normalize_text(text)
    if not normalized:
        raise ValueError("Escribe el texto que quieras narrar.")
    if len(normalized) > MAX_TEXT_CHARACTERS:
        raise ValueError(f"El texto supera el límite de {MAX_TEXT_CHARACTERS:,} caracteres.")
    chunks: list[TextChunk] = []
    for paragraph_index, paragraph in enumerate(normalized.split("\n\n")):
        pieces: list[str] = []
        current = ""
        for sentence in _sentences(paragraph):
            if len(sentence) > maximum:
                raise ValueError(
                    f"Una frase tiene {len(sentence)} caracteres; el máximo por frase es {maximum}. "
                    "Añade un punto donde corresponda para conservar frases completas."
                )
            combined = f"{current} {sentence}" if current else sentence
            if current and len(combined) > target:
                pieces.append(current)
                current = sentence
            else:
                current = combined
        if current:
            pieces.append(current)
        chunks.extend(TextChunk(piece, paragraph_index, i == len(pieces) - 1) for i, piece in enumerate(pieces))
    return chunks


def _package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


class TTSEngine:
    def __init__(
        self,
        runtime_dir: Path,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        keep_model_loaded: bool = False,
    ):
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.on_event = on_event
        self._profiles = ProfileStore(self.runtime_dir)
        with _LOCKS_GUARD:
            self._job_lock = _RUNTIME_LOCKS.setdefault(os.path.normcase(str(self.runtime_dir)), threading.Lock())
        self._cancel_event = threading.Event()
        self._torch: Any = None
        self._model: Any = None
        self._model_kind: str | None = None
        self._keep_model_loaded = keep_model_loaded

    def _emit(self, event: str, **fields: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"event": event, **fields})
            except Exception:
                _LOGGER.warning("No se pudo notificar el estado del motor TTS.")

    def get_profiles(self) -> list[dict[str, Any]]:
        return self._profiles.list()

    def approve_profile(self, profile_id: str, candidate_id: str | None = None) -> dict[str, Any]:
        if not self._job_lock.acquire(blocking=False):
            raise EngineBusyError("Espera a que termine el trabajo antes de aprobar la voz.")
        try:
            result = self._profiles.approve(profile_id, candidate_id)
            self._emit("profile_approved", profile_id=profile_id, candidate_id=result["active_candidate_id"])
            return result
        finally:
            self._job_lock.release()

    def cancel(self) -> None:
        """Request cancellation at the next boundary; a GPU call finishes safely."""
        self._cancel_event.set()

    cancel_current_job = cancel

    def _check_cancel(self, payload: dict[str, Any]) -> None:
        external = payload.get("cancel_event")
        if self._cancel_event.is_set() or (external is not None and external.is_set()):
            raise JobCancelled("Trabajo cancelado después del fragmento en curso.")

    def _progress(self, progress: Callable[[str, float], Any] | None, message: str, fraction: float, payload: dict[str, Any]) -> None:
        self._check_cancel(payload)
        if progress is not None and progress(message, min(1.0, max(0.0, fraction))) is False:
            raise JobCancelled("Trabajo cancelado.")
        self._check_cancel(payload)
        self._emit("progress", message=message, fraction=fraction)

    def doctor(self) -> dict[str, Any]:
        """Lightweight installation diagnostics. Does not import Torch or load models."""
        packages = {name: _package_version(name) for name in ("torch", "qwen-tts", "numpy", "soundfile", "transformers")}
        models = {}
        for name in MODEL_DIRECTORIES.values():
            directory = self.runtime_dir / "models" / name
            required = ("config.json", "generation_config.json", "speech_tokenizer/config.json")
            missing = [item for item in required if not (directory / item).is_file()]
            weights_present = any(directory.glob("*.safetensors")) if directory.is_dir() else False
            models[name] = {"path": str(directory), "present": not missing and weights_present, "missing": missing, "weights_present": weights_present}
        ffmpeg = self.runtime_dir / "bin" / "ffmpeg.exe"
        gpu: dict[str, Any] = {"available": False}
        executable = shutil.which("nvidia-smi")
        if executable:
            try:
                result = subprocess.run(
                    [executable, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
                if result.returncode == 0 and result.stdout.strip():
                    name, memory, driver = [part.strip() for part in result.stdout.splitlines()[0].split(",", 2)]
                    gpu = {"available": True, "name": name, "memory_total_mb": int(float(memory)), "driver_version": driver}
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        warnings = []
        if not ffmpeg.is_file():
            warnings.append("FFmpeg no está instalado; los WAV originales siguen disponibles.")
        if not gpu["available"]:
            warnings.append("No se ha podido verificar una GPU NVIDIA mediante nvidia-smi.")
        return {
            "runtime_dir": str(self.runtime_dir),
            "ready": all(packages.values()) and all(item["present"] for item in models.values()) and gpu["available"],
            "packages": packages,
            "models": models,
            "gpu": gpu,
            "ffmpeg": {"path": str(ffmpeg), "available": ffmpeg.is_file()},
            "mode": "cuda-bfloat16-sdpa",
            "offline_inference": True,
            "warnings": warnings,
        }

    def _import_torch(self):
        if self._torch is None:
            try:
                import torch
            except ImportError as exc:
                raise EngineError("Falta PyTorch en el entorno local. Ejecuta el instalador de AstroLearner TTS.") from exc
            if not torch.cuda.is_available():
                raise EngineError("No hay CUDA disponible. Este motor necesita la GPU NVIDIA; comprueba el controlador y PyTorch CUDA.")
            if hasattr(torch.cuda, "is_bf16_supported") and not torch.cuda.is_bf16_supported():
                raise EngineError("La GPU o el entorno CUDA no admite bfloat16; no se ha cambiado a CPU.")
            self._torch = torch
        return self._torch

    def _load_model(self, kind: str) -> tuple[Any, float]:
        if self._model is not None and self._model_kind == kind:
            return self._model, 0.0
        self._unload_model()
        directory = self.runtime_dir / "models" / MODEL_DIRECTORIES[kind]
        if not directory.is_dir() or not (directory / "config.json").is_file():
            raise EngineError(f"Falta el modelo local {directory.name}. Ejecuta primero el instalador.")
        # Set before importing HF/Qwen: their offline constants are initialized
        # at import time. from_pretrained also receives local_files_only=True.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"
        torch = self._import_torch()
        try:
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:
            raise EngineError("Falta qwen-tts en el entorno local. Ejecuta el instalador de AstroLearner TTS.") from exc
        started = time.perf_counter()
        self._emit("model_loading", model=directory.name)
        self._model = Qwen3TTSModel.from_pretrained(
            str(directory), device_map="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa", local_files_only=True,
        )
        self._model_kind = kind
        elapsed = time.perf_counter() - started
        self._emit("model_loaded", model=directory.name, seconds=elapsed)
        return self._model, elapsed

    def _unload_model(self) -> None:
        old_kind = self._model_kind
        self._model = None
        self._model_kind = None
        gc.collect()
        if self._torch is not None:
            self._torch.cuda.empty_cache()
        if old_kind:
            self._emit("model_unloaded", model=MODEL_DIRECTORIES[old_kind])

    def unload(self) -> None:
        if not self._job_lock.acquire(blocking=False):
            raise EngineBusyError("El motor está generando audio.")
        try:
            self._unload_model()
        finally:
            self._job_lock.release()

    def _seed(self, seed: int) -> None:
        random.seed(seed)
        import numpy as np
        np.random.seed(seed % (2**32))
        torch = self._import_torch()
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)

    def _generate(self, method: str, text: str, seed: int, **kwargs: Any) -> tuple[Any, int, float, int]:
        self._seed(seed)
        max_tokens = min(1536, max(512, len(text) * 3))
        started = time.perf_counter()
        with self._import_torch().inference_mode():
            waves, sample_rate = getattr(self._model, method)(
                text=text, max_new_tokens=max_tokens, non_streaming_mode=True, **kwargs,
            )
        elapsed = time.perf_counter() - started
        if len(waves) != 1:
            raise EngineError("El modelo devolvió un número de fragmentos inesperado.")
        return waves[0], int(sample_rate), elapsed, max_tokens

    def _design(self, spec: dict[str, Any], text: str, seed: int, payload: dict[str, Any], job_dir: Path, progress: Any) -> dict[str, Any]:
        self._progress(progress, "Cargando el modelo de diseño de voz…", 0.04, payload)
        _, load_seconds = self._load_model("design")
        self._progress(progress, "Generando el candidato de voz…", 0.12, payload)
        instruct = payload.get("instruct", spec["instruct"])
        wave, sample_rate, generation_seconds, token_limit = self._generate(
            "generate_voice_design", text, seed, language=spec["language"], instruct=instruct,
        )
        self._check_cancel(payload)
        raw = audio.write_float_wav(job_dir / "original.wav", wave, sample_rate)
        del wave
        self._progress(progress, "Guardando una referencia original, pendiente de aprobación…", 0.84, payload)
        candidate = self._profiles.save_candidate(
            spec["id"], Path(raw["audio_path"]), text,
            {"instruct": instruct, "seed": seed, "sample_rate": sample_rate,
             "duration_seconds": raw["duration_seconds"], "model": MODEL_REPOSITORIES["design"], "job_id": job_dir.name},
        )
        segments = [{"index": 1, "text": text, "audio_path": raw["audio_path"], "start_seconds": 0.0,
                     "end_seconds": raw["duration_seconds"], "gap_after_seconds": 0.0,
                     "paragraph_index": 0, "seed": seed, "max_new_tokens": token_limit}]
        return {"raw": raw, "segments": segments, "generation_seconds": generation_seconds,
                "model_load_seconds": load_seconds, "candidate_id": candidate["candidate_id"],
                "reference_audio_path": candidate["audio_path"], "approved": False}

    def _narrate(self, spec: dict[str, Any], chunks: list[TextChunk], seed: int, payload: dict[str, Any], job_dir: Path, progress: Any) -> dict[str, Any]:
        reference = self._profiles.reference(spec["id"], payload.get("allow_unreviewed") is True)
        self._progress(progress, "Cargando el modelo de narración…", 0.04, payload)
        _, load_seconds = self._load_model("narrate")
        self._progress(progress, "Preparando la referencia aprobada…", 0.09, payload)
        clone_prompt = self._model.create_voice_clone_prompt(
            ref_audio=reference["audio_path"], ref_text=reference["reference_text"], x_vector_only_mode=False,
        )
        self._check_cancel(payload)
        segments: list[dict[str, Any]] = []
        generation_seconds = 0.0
        sample_rate = None
        try:
            for index, chunk in enumerate(chunks):
                self._progress(progress, f"Narrando fragmento {index + 1} de {len(chunks)}…", 0.1 + 0.74 * index / len(chunks), payload)
                segment_seed = (seed + index) % (2**32)
                wave, rate, seconds, token_limit = self._generate(
                    "generate_voice_clone", chunk.text, segment_seed,
                    language=spec["language"], voice_clone_prompt=clone_prompt,
                )
                self._check_cancel(payload)
                if sample_rate is not None and rate != sample_rate:
                    raise EngineError("El modelo cambió de frecuencia de muestreo entre fragmentos.")
                sample_rate = rate
                path = job_dir / "segments" / f"{index + 1:04d}.wav"
                info = audio.write_float_wav(path, wave, rate)
                del wave
                generation_seconds += seconds
                gap = 0.0 if index == len(chunks) - 1 else (0.2 if chunk.paragraph_end else 0.12)
                segments.append({"index": index + 1, "text": chunk.text, "audio_path": info["audio_path"],
                                 "duration_seconds": info["duration_seconds"], "paragraph_index": chunk.paragraph_index,
                                 "gap_after_seconds": gap, "seed": segment_seed, "max_new_tokens": token_limit})
        finally:
            clone_prompt = None
        self._progress(progress, "Uniendo los fragmentos y sus pausas…", 0.85, payload)
        raw = audio.assemble_wav(segments, job_dir / "original.wav", int(sample_rate))
        return {"raw": raw, "segments": segments, "generation_seconds": generation_seconds,
                "model_load_seconds": load_seconds, "candidate_id": reference["candidate_id"],
                "reference_audio_path": reference["audio_path"], "approved": bool(reference.get("approved"))}

    def _reference(self, spec: dict[str, Any], text: str, payload: dict[str, Any], job_dir: Path, progress: Any) -> dict[str, Any]:
        self._progress(progress, "Preparando el fragmento de referencia local…", 0.1, payload)
        prepared = references.prepare_reference(
            self.runtime_dir, Path(payload["reference_upload_path"]), job_dir, text,
            payload["start_seconds"], payload["duration_seconds"], payload["original_filename"],
        )
        self._check_cancel(payload)
        raw = prepared["raw"]
        self._progress(progress, "Guardando la referencia como borrador, sin aprobar…", 0.8, payload)
        candidate = self._profiles.save_candidate(
            spec["id"], Path(raw["audio_path"]), text,
            {**prepared["metadata"], "job_id": job_dir.name}, source="local_authorized_reference_import",
        )
        return {"raw": raw, "segments": [{"index": 1, "text": text, "audio_path": raw["audio_path"],
                "start_seconds": 0.0, "end_seconds": raw["duration_seconds"], "gap_after_seconds": 0.0,
                "paragraph_index": 0}], "generation_seconds": 0.0, "model_load_seconds": 0.0,
                "candidate_id": candidate["candidate_id"], "reference_audio_path": candidate["audio_path"],
                "approved": False, "warnings": prepared["warnings"], "source": "local_authorized_reference_import"}

    def run_job(self, kind: str, payload: dict[str, Any], progress: Callable[[str, float], Any] | None = None) -> dict[str, Any]:
        if kind not in {*MODEL_DIRECTORIES, "reference"}:
            raise ValueError("El tipo de trabajo debe ser design, reference o narrate.")
        if not isinstance(payload, dict):
            raise ValueError("Los datos del trabajo deben ser un objeto.")
        if not self._job_lock.acquire(blocking=False):
            raise EngineBusyError("Ya hay un trabajo de voz en curso. Espera a que termine.")
        self._cancel_event.clear()
        started = time.perf_counter()
        job_dir: Path | None = None
        succeeded = False
        try:
            spec = get_profile_spec(payload.get("profile_id", ""))
            original_text = payload.get("text", spec["reference_text"] if kind == "design" else "")
            if not isinstance(original_text, str) or len(original_text) > MAX_TEXT_CHARACTERS:
                raise ValueError(f"El texto debe contener como máximo {MAX_TEXT_CHARACTERS:,} caracteres.")
            text = normalize_text(original_text)
            # A reference transcript must describe exactly the uploaded speech.
            if kind != "reference":
                text = apply_pronunciations(text, payload.get("pronunciations"))
            chunks = segment_text(text)
            seed = payload.get("seed", 42)
            if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
                raise ValueError("La semilla debe ser un entero entre 0 y 4294967295.")
            if kind == "design":
                if len(text) > MAX_CHUNK_CHARACTERS:
                    raise ValueError("El diseño de una referencia admite hasta 500 caracteres. Usa un texto breve de prueba.")
                instruct = payload.get("instruct", spec["instruct"])
                if not isinstance(instruct, str) or not instruct.strip() or len(instruct) > 4000:
                    raise ValueError("La descripción de voz debe contener entre 1 y 4000 caracteres.")
            elif kind == "reference":
                references.validate_settings(text, payload.get("start_seconds"), payload.get("duration_seconds"))
                references.staging_path(self.runtime_dir, Path(payload.get("reference_upload_path", "")))
            else:
                # Reject an unapproved profile before importing or allocating any GPU model.
                self._profiles.reference(spec["id"], payload.get("allow_unreviewed") is True)
            self._progress(progress, "Preparando el trabajo local…", 0.0, payload)
            job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"
            job_dir = self.runtime_dir / "jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            (job_dir / "text.txt").write_text(text, encoding="utf-8", newline="\n")
            if original_text != text:
                (job_dir / "original-text.txt").write_text(original_text, encoding="utf-8", newline="\n")
            if self._torch is not None and kind != "reference":
                self._torch.cuda.reset_peak_memory_stats()
            self._emit("job_started", job_id=job_id, kind=kind, profile_id=spec["id"])
            if kind == "design":
                generated = self._design(spec, text, seed, payload, job_dir, progress)
            elif kind == "reference":
                generated = self._reference(spec, text, payload, job_dir, progress)
            else:
                generated = self._narrate(spec, chunks, seed, payload, job_dir, progress)
            raw = generated["raw"]
            output = raw
            warnings = list(generated.get("warnings", []))
            mastering = None
            if payload.get("master", True):
                self._progress(progress, "Preparando el máster de sonoridad…", 0.9, payload)
                try:
                    mastered = audio.master_wav(Path(raw["audio_path"]), job_dir / "master-48k.wav", self.runtime_dir / "bin" / "ffmpeg.exe")
                    output = mastered
                    mastering = {key: value for key, value in mastered.items() if key not in {"audio_path", "frames", "duration_seconds", "sample_rate", "subtype"}}
                except (audio.AudioError, OSError, subprocess.SubprocessError) as exc:
                    warnings.append(str(exc))
            self._check_cancel(payload)
            elapsed = time.perf_counter() - started
            duration = output["duration_seconds"]
            peak = float(self._torch.cuda.max_memory_allocated() / (1024**3)) if self._torch is not None and kind != "reference" else None
            metrics = {
                "generation_seconds": generated["generation_seconds"],
                "model_load_seconds": generated["model_load_seconds"],
                "elapsed_seconds": elapsed,
                "real_time_factor": elapsed / duration if duration else None,
                "generation_real_time_factor": generated["generation_seconds"] / duration if duration else None,
                "cuda_peak_allocated_gib": peak,
                "raw_sample_rate": raw["sample_rate"],
                "raw_subtype": raw["subtype"],
                "mastering": mastering,
            }
            result = {
                "job_id": job_id, "kind": kind, "profile_id": spec["id"], "candidate_id": generated["candidate_id"],
                "approved": generated["approved"], "audio_path": output["audio_path"],
                "raw_audio_path": raw["audio_path"], "master_audio_path": output["audio_path"] if mastering else None,
                "reference_audio_path": generated["reference_audio_path"], "sample_rate": output["sample_rate"],
                "duration_seconds": duration, "segments": generated["segments"],
                "timing_kind": "chunk_boundaries_not_word_alignment", "metrics": metrics, "warnings": warnings,
            }
            if generated.get("source"):
                result["source"] = generated["source"]
            write_json(job_dir / "result.json", result)
            self._progress(progress, "Audio listo. Escucha el resultado antes de aprobar o publicar.", 1.0, payload)
            self._emit("job_completed", job_id=job_id, kind=kind, profile_id=spec["id"], duration_seconds=duration)
            succeeded = True
            return result
        except BaseException:
            # Preserve generated files for inspection and release the resident
            # GPU model. Cancellation callbacks keep their original exception.
            try:
                self._unload_model()
            except Exception:
                _LOGGER.warning("No se pudo completar la liberación de memoria CUDA.")
            if job_dir is not None:
                try:
                    write_json(job_dir / "status.json", {"status": "interrupted", "updated_at": utc_now()})
                except OSError:
                    _LOGGER.warning("No se pudo guardar el estado del trabajo interrumpido.")
            raise
        finally:
            try:
                if succeeded and not self._keep_model_loaded:
                    try:
                        self._unload_model()
                    except Exception:
                        _LOGGER.warning("No se pudo completar la liberación de memoria CUDA.")
            finally:
                self._job_lock.release()
