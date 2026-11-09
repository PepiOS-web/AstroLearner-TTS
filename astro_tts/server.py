"""A loopback-only HTTP interface with one serialized inference worker.

Importing this module never loads a speech model. The engine owns inference and
voice persistence; this module owns validation, the job queue and safe downloads.
"""

from __future__ import annotations

import copy
import heapq
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.datastructures import UploadFile

from . import references

LOG = logging.getLogger(__name__)
PROFILE_IDS = frozenset({"es-narrador", "en-storyteller"})
STATIC_DIR = Path(__file__).parent / "static"
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
RESULT_FIELDS = frozenset({
    "job_id", "kind", "profile_id", "candidate_id", "approved", "sample_rate",
    "duration_seconds", "segments", "timing_kind", "metrics", "warnings", "source",
})


def default_runtime() -> Path:
    configured = os.environ.get("ASTRO_TTS_RUNTIME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "AstroLearnerTTS").resolve()


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["design", "narrate"]
    profile_id: Literal["es-narrador", "en-storyteller"]
    text: str = Field(strict=True, min_length=1, max_length=18000)
    seed: int = Field(default=42, strict=True, ge=0, le=4294967295)
    instruct: str | None = Field(default=None, strict=True, max_length=3000)
    pronunciations: dict[str, str] | None = None

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Escribe un texto para sintetizar.")
        return value.strip()

    @field_validator("pronunciations", mode="before")
    @classmethod
    def valid_pronunciations(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, dict) or len(value) > 100:
            raise ValueError("Usa un objeto con un máximo de 100 pronunciaciones.")
        for key, replacement in value.items():
            if not isinstance(key, str) or not isinstance(replacement, str):
                raise ValueError("Las pronunciaciones deben contener palabras y reemplazos de texto.")
            if not key.strip() or not replacement.strip() or len(key) > 200 or len(replacement) > 300:
                raise ValueError("Cada pronunciación necesita una palabra y un reemplazo breves.")
        return value

    @model_validator(mode="after")
    def design_sample_limit(self) -> "JobRequest":
        if self.kind == "design" and len(self.text) > 500:
            raise ValueError("El diseño de voz admite hasta 500 caracteres; usa una muestra breve.")
        return self


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(strict=True, min_length=1, max_length=120)


class JobCancelled(Exception):
    """Cooperative cancellation between inference steps."""


def _serializable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _public_result_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public_result_value(item) for key, item in value.items()
                if not key.endswith(("_path", "_url"))}
    if isinstance(value, list):
        return [_public_result_value(item) for item in value]
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Constante JSON no válida: {value}")


class JobQueue:
    def __init__(self, engine: Any, runtime: Path, capacity: int = 8):
        self.engine = engine
        self.runtime = runtime.resolve()
        self.capacity = capacity
        self.jobs: dict[str, dict] = {}
        self.pending: deque[str] = deque()
        self.condition = threading.Condition(threading.RLock())
        self.stopping = False
        self.thread: threading.Thread | None = None
        self._restore_completed()

    def _register_result(self, job: dict, result: dict, *, restored_directory: Path | None = None) -> None:
        data = _serializable(result)
        public_result = _public_result_value({key: value for key, value in data.items() if key in RESULT_FIELDS})
        paths = {
            "main": ("audio_path", "output_path"),
            "raw": ("raw_audio_path", "raw_path"),
            "master": ("master_audio_path", "master_path"),
        }
        for variant, keys in paths.items():
            value = next((data.get(key) for key in keys if data.get(key)), None)
            path = self.safe_audio_path(value) if value else None
            if path is None:
                continue
            if restored_directory is not None:
                allowed_names = {
                    "main": {"original.wav", "master-48k.wav"},
                    "raw": {"original.wav"},
                    "master": {"master-48k.wav"},
                }[variant]
                if path.parent != restored_directory or path.name not in allowed_names:
                    continue
            job["_audio"][variant] = str(path)
            url_key = "audio_url" if variant == "main" else f"{variant}_audio_url"
            public_result[url_key] = f"/api/jobs/{job['id']}/audio?variant={variant}"
        job["result"] = public_result

    def _restore_completed(self) -> None:
        """Read only twenty recent engine results; never restore pending work."""
        jobs_root = (self.runtime / "jobs").resolve()
        if jobs_root != self.runtime / "jobs":
            return

        def candidates():
            for path in jobs_root.glob("*/result.json"):
                try:
                    directory = path.parent.resolve(strict=True)
                    # Do not follow a job-directory junction outside the jobs root.
                    if directory.parent == jobs_root and path.resolve(strict=True).parent == directory:
                        yield path.stat().st_mtime, str(path), path
                except OSError:
                    continue

        for modified, _, path in sorted(heapq.nlargest(20, candidates())):
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                directory = path.parent.resolve(strict=True)
                if (directory / "status.json").exists():
                    # The engine writes this marker when interrupted, even if a
                    # result was saved just before a cancellation callback.
                    continue
                result = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
                if (not isinstance(result, dict) or result.get("kind") not in {"design", "narrate", "reference"}
                        or result.get("profile_id") not in PROFILE_IDS):
                    continue
                if result.get("job_id") != directory.name:
                    continue
                duration = result.get("duration_seconds")
                if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                    continue
                uuid.UUID(result["candidate_id"])
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"astrolearner-tts/{os.path.normcase(str(self.runtime))}/{directory.name}"))
                job = {
                    "id": job_id, "kind": result["kind"], "profile_id": result["profile_id"],
                    "state": "completed", "created_at": modified, "started_at": None,
                    "finished_at": modified, "progress": {"message": "Audio preparado", "fraction": 1},
                    "result": None, "error": None, "_payload": {}, "_audio": {},
                }
                self._register_result(job, result, restored_directory=directory)
                if "main" in job["_audio"]:
                    self.jobs[job_id] = job
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                LOG.warning("Se omitió un resultado local no válido: %s", path.name)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._work, daemon=True, name="astro-tts-worker")
        self.thread.start()

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            for job in self.jobs.values():
                if job["state"] == "queued":
                    job["state"] = "cancelled"
                    self._cleanup_reference(job)
                elif job["state"] == "running":
                    job["state"] = "cancel_requested"
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=1)

    def submit(self, request: JobRequest) -> dict:
        return self._enqueue(request.kind, request.profile_id, request.model_dump(exclude={"kind"}, exclude_none=True))

    def submit_reference(self, profile_id: str, text: str, source: Path, start: float, duration: float, filename: str) -> dict:
        return self._enqueue("reference", profile_id, {
            "profile_id": profile_id, "text": text, "reference_upload_path": str(source),
            "start_seconds": start, "duration_seconds": duration, "original_filename": filename,
        })

    def _enqueue(self, kind: str, profile_id: str, payload: dict) -> dict:
        with self.condition:
            if self.stopping:
                raise HTTPException(503, "El estudio se está cerrando.")
            if len(self.pending) >= self.capacity:
                raise HTTPException(429, "La cola está llena. Espera a que termine un trabajo.")
            job_id = str(uuid.uuid4())
            job = {
                "id": job_id, "kind": kind, "profile_id": profile_id,
                "state": "queued", "created_at": time.time(), "started_at": None,
                "finished_at": None, "progress": {"message": "En cola", "fraction": 0},
                "result": None, "error": None, "_payload": payload,
                "_audio": {},
            }
            self.jobs[job_id] = job
            self.pending.append(job_id)
            # Keep twenty recent terminal jobs, plus every unfinished job.
            done = [key for key, item in self.jobs.items() if item["state"] in TERMINAL_STATES]
            for key in done[:-20]:
                del self.jobs[key]
            self.condition.notify()
            return self.public(job)

    def public(self, job: dict) -> dict:
        return copy.deepcopy({k: v for k, v in job.items() if not k.startswith("_")})

    def get(self, job_id: str) -> dict:
        with self.condition:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, "Trabajo no encontrado.")
            return self.public(job)

    def cancel(self, job_id: str) -> dict:
        with self.condition:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, "Trabajo no encontrado.")
            if job["state"] == "queued":
                self.pending.remove(job_id)
                job.update(state="cancelled", finished_at=time.time(), progress={"message": "Cancelado antes de empezar", "fraction": 0})
                self._cleanup_reference(job)
            elif job["state"] == "running":
                job["state"] = "cancel_requested"
                job["progress"]["message"] = "Cancelación solicitada. Esperando a terminar el paso actual."
            return self.public(job)

    def has_design_inflight(self, profile_id: str) -> bool:
        with self.condition:
            return any(j["profile_id"] == profile_id and j["kind"] in {"design", "reference"} and j["state"] not in TERMINAL_STATES for j in self.jobs.values())

    def _cleanup_reference(self, job: dict) -> None:
        if job["kind"] == "reference":
            references.cleanup_upload(self.runtime, job["_payload"].get("reference_upload_path"))

    def _progress(self, job: dict, message: Any = "", fraction: float | None = None, **kwargs: Any) -> bool:
        with self.condition:
            if job["state"] == "cancel_requested" or self.stopping:
                raise JobCancelled()
            update = dict(message) if isinstance(message, dict) else {"message": str(message)}
            update.update(kwargs)
            if fraction is not None:
                update["fraction"] = max(0.0, min(1.0, float(fraction)))
            job["progress"].update(_serializable(update))
        return True

    def _work(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.pending)
                if self.stopping:
                    return
                job = self.jobs[self.pending.popleft()]
                if job["state"] != "queued":
                    continue
                job.update(state="running", started_at=time.time())
            try:
                if job["kind"] == "narrate":
                    profile = next((p for p in self.engine.get_profiles() if p.get("id") == job["profile_id"]), None)
                    if not profile or not profile.get("approved"):
                        raise ValueError("Aprueba una voz antes de narrar con este perfil.")
                result = self.engine.run_job(job["kind"], job["_payload"], lambda message="", fraction=None, **kw: self._progress(job, message, fraction, **kw))
                with self.condition:
                    if job["state"] == "cancel_requested" or self.stopping:
                        raise JobCancelled()
                    self._register_result(job, result or {})
                    job.update(state="completed", progress={"message": "Audio preparado", "fraction": 1})
            except Exception as exc:
                with self.condition:
                    cancelled = isinstance(exc, JobCancelled) or job["state"] == "cancel_requested" or self.stopping
                    job["state"] = "cancelled" if cancelled else "failed"
                    job["error"] = None if cancelled else str(exc)[:1000]
                    job["progress"]["message"] = "Cancelado al terminar el paso actual" if cancelled else "No se pudo completar"
                if not cancelled:
                    LOG.exception("TTS job %s failed", job["id"])
            finally:
                self._cleanup_reference(job)
                with self.condition:
                    job["finished_at"] = time.time()

    def safe_audio_path(self, value: Any) -> Path | None:
        try:
            path = Path(value).resolve(strict=True)
            roots = (self.runtime / "outputs", self.runtime / "jobs", self.runtime / "profiles")
            if path.suffix.lower() != ".wav" or not path.is_file():
                return None
            if not any(path.is_relative_to(root.resolve()) for root in roots):
                return None
            with path.open("rb") as audio:
                header = audio.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
                return None
            return path
        except (OSError, ValueError, TypeError):
            return None


def create_app(runtime_dir: Path | str | None = None, *, engine: Any = None, port: int = 8095) -> FastAPI:
    runtime = Path(runtime_dir).expanduser().resolve() if runtime_dir else default_runtime()
    if engine is None:
        from .engine import TTSEngine

        engine = TTSEngine(runtime)
    queue = JobQueue(engine, runtime)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if port == 80:
        allowed_hosts |= {"127.0.0.1", "localhost"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        queue.start()
        try:
            yield
        finally:
            queue.close()

    app = FastAPI(title="AstroLearner TTS", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.queue = queue
    app.state.engine = engine
    app.state.runtime = runtime

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return JSONResponse({"detail": "Solo se admiten conexiones locales."}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse({"detail": "Origen no permitido."}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin not in allowed_origins:
            return JSONResponse({"detail": "Las modificaciones requieren el origen local del estudio."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_profile(profile_id: str) -> dict:
        if profile_id not in PROFILE_IDS:
            raise HTTPException(404, "Perfil no encontrado.")
        profile = next((p for p in engine.get_profiles() if p.get("id") == profile_id), None)
        if profile is None:
            raise HTTPException(404, "Perfil no encontrado.")
        return profile

    def public_profile(profile: dict) -> dict:
        data = _serializable(profile)
        for variant, field in (("active", "audio_path"), ("draft", "draft_audio_path")):
            if data.get(field) and queue.safe_audio_path(data[field]):
                data[f"{variant}_audio_url"] = f"/api/profiles/{profile['id']}/audio?variant={variant}"
        return {k: v for k, v in data.items() if not k.endswith("_path")}

    @app.get("/api/health")
    def health():
        try:
            return {"app": "AstroLearnerTTS", "ok": True, "doctor": _serializable(engine.doctor()), "local_only": True}
        except Exception as exc:
            return {"app": "AstroLearnerTTS", "ok": False, "doctor": {"error": str(exc)[:500]}, "local_only": True}

    @app.get("/api/profiles")
    def profiles():
        return {"profiles": [public_profile(p) for p in engine.get_profiles() if p.get("id") in PROFILE_IDS]}

    @app.get("/api/jobs")
    def jobs():
        with queue.condition:
            return {"jobs": [queue.public(j) for j in list(queue.jobs.values())[-20:]][::-1]}

    @app.post("/api/jobs", status_code=202)
    def submit_job(body: JobRequest):
        profile = get_profile(body.profile_id)
        if body.kind == "narrate" and not profile.get("approved"):
            raise HTTPException(409, "Escucha y aprueba una voz antes de narrar.")
        return queue.submit(body)

    @app.post("/api/profiles/{profile_id}/reference", status_code=202)
    async def import_reference(profile_id: str, request: Request):
        get_profile(profile_id)
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
            raise HTTPException(415, "Envía el archivo y su transcripción como formulario multipart.")
        # Bound the actual request stream before invoking the multipart parser,
        # including chunked requests without a Content-Length header.
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError as exc:
            raise HTTPException(400, "Tamaño de formulario no válido.") from exc
        if declared > references.MAX_MULTIPART_BYTES:
            raise HTTPException(413, "El archivo de referencia admite como máximo 25 MiB.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > references.MAX_MULTIPART_BYTES:
                raise HTTPException(413, "El archivo de referencia admite como máximo 25 MiB.")
            body.extend(chunk)

        async def receive():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        parsed = Request(request.scope, receive=receive)
        temporary = None
        admitted = False
        try:
            async with parsed.form(max_files=1, max_fields=3, max_part_size=128 * 1024) as form:
                fields = list(form.multi_items())
                if any(key not in {"audio", "text", "start_seconds", "duration_seconds"} for key, _ in fields) or len({key for key, _ in fields}) != len(fields):
                    raise HTTPException(422, "El formulario contiene campos desconocidos o repetidos.")
                uploaded = form.get("audio")
                text = form.get("text")
                if not isinstance(uploaded, UploadFile) or not isinstance(text, str):
                    raise HTTPException(422, "Selecciona un archivo de audio y escribe su transcripción exacta.")
                try:
                    start = float(form.get("start_seconds", "0"))
                    duration = float(form.get("duration_seconds", "15"))
                    references.validate_settings(text, start, duration)
                    filename, suffix = references.upload_filename(uploaded.filename)
                except (ValueError, TypeError) as exc:
                    raise HTTPException(422, str(exc)) from exc
                incoming = runtime / "incoming"
                if incoming.resolve() != incoming:
                    raise HTTPException(409, "La carpeta temporal de referencias no es válida.")
                directory = incoming / uuid.uuid4().hex
                directory.mkdir(parents=True, exist_ok=False)
                temporary = directory / ("source" + suffix)
                size = 0
                header = bytearray()
                with temporary.open("xb") as destination:
                    while data := await uploaded.read(1024 * 1024):
                        size += len(data)
                        if size > references.MAX_UPLOAD_BYTES:
                            raise HTTPException(413, "El archivo de referencia admite como máximo 25 MiB.")
                        if len(header) < 64:
                            header.extend(data[:64-len(header)])
                        destination.write(data)
                if not size or not references.valid_header(bytes(header), suffix):
                    raise HTTPException(422, "El archivo está vacío o su contenido no coincide con el formato de audio.")
                result = queue.submit_reference(profile_id, text.strip(), temporary, start, duration, filename)
                admitted = True
                return result
        finally:
            if not admitted and temporary is not None:
                references.cleanup_upload(runtime, temporary)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: uuid.UUID):
        return queue.get(str(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: uuid.UUID):
        return queue.cancel(str(job_id))

    @app.post("/api/profiles/{profile_id}/approve")
    def approve(profile_id: str, body: ApprovalRequest):
        get_profile(profile_id)
        with queue.condition:
            if queue.has_design_inflight(profile_id) or any(j["state"] in {"running", "cancel_requested"} for j in queue.jobs.values()):
                raise HTTPException(409, "Espera a que termine el trabajo en curso antes de aprobar la voz.")
            try:
                profile = engine.approve_profile(profile_id, candidate_id=body.candidate_id)
            except (ValueError, FileNotFoundError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from exc
        return public_profile(profile)

    @app.get("/api/jobs/{job_id}/audio")
    def job_audio(job_id: uuid.UUID, variant: Literal["main", "raw", "master"] = Query("main")):
        with queue.condition:
            job = queue.jobs.get(str(job_id))
            if not job or job["state"] != "completed":
                raise HTTPException(404, "Audio no disponible.")
            path = queue.safe_audio_path(job["_audio"].get(variant))
        if path is None:
            raise HTTPException(404, "Audio no disponible.")
        return FileResponse(path, media_type="audio/wav", filename=f"astrolearner-{job['profile_id']}-{variant}.wav")

    @app.get("/api/profiles/{profile_id}/audio")
    def profile_audio(profile_id: str, variant: Literal["active", "draft"] = Query("active"), v: uuid.UUID | None = Query(None)):
        profile = get_profile(profile_id)
        if v is not None:
            candidate = profile.get("draft_candidate_id" if variant == "draft" else "active_candidate_id")
            if candidate is None or uuid.UUID(candidate) != v:
                raise HTTPException(409, "Esta referencia ha cambiado. Actualiza el perfil y escucha la versión actual.")
        path = queue.safe_audio_path(profile.get("draft_audio_path" if variant == "draft" else "audio_path"))
        if path is None:
            raise HTTPException(404, "Esta voz todavía no tiene un audio de referencia.")
        return FileResponse(path, media_type="audio/wav", filename=f"astrolearner-{profile_id}-{variant}.wav")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    return app
