"""Reference import boundaries and CPU-only candidate persistence."""

from __future__ import annotations

import io
import json
import math
import threading
import time
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from astro_tts import audio, references
from astro_tts.engine import TTSEngine
from astro_tts.profiles import ProfileStore
from astro_tts.server import create_app

BASE = "http://127.0.0.1:8095"
HEADERS = {"Origin": BASE}
TEXT = "La ciencia comienza con una pregunta sencilla y exige paciencia y precisión."


def source_wave(seconds=6.0, amplitude=0.2, rate=44100):
    buffer = io.BytesIO()
    samples = amplitude * np.sin(2 * np.pi * 180 * np.arange(round(seconds * rate)) / rate)
    sf.write(buffer, samples, rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def staged(runtime, data):
    path = runtime / "incoming" / uuid.uuid4().hex / "source.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def decoder(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "ffmpeg.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"fake")
    calls = []

    def decode(command, timeout):
        calls.append(command)
        # Decode the test fixture using SoundFile; assert the real process command
        # limits protocols, demuxer and output duration before creating any WAV.
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-f") + 1] == "wav"
        assert command[command.index("-map") + 1] == "0:a:0"
        assert timeout == 60
        source = command[command.index("-i") + 1]
        start = float(command[command.index("-ss") + 1])
        duration = float(command[command.index("-t") + 1])
        values, rate = sf.read(source)
        beginning = round(start * rate)
        values = values[beginning:beginning + round(duration * rate)]
        # This fixture deliberately uses one fixed rate to make boundaries exact.
        assert rate == 24000
        sf.write(command[-1], values, 24000, subtype="FLOAT", format="WAV")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(audio, "_run_ffmpeg", decode)
    return calls


@pytest.mark.parametrize("text,start,duration", [
    ("", 0, 15), ("x" * 501, 0, 15), (TEXT, -1, 15), (TEXT, math.nan, 15),
    (TEXT, math.inf, 15), (TEXT, 0, 4.99), (TEXT, 0, 30.01), (TEXT, 0, math.inf),
])
def test_reference_settings_are_bounded(text, start, duration):
    with pytest.raises(ValueError):
        references.validate_settings(text, start, duration)


def test_cleanup_never_removes_paths_outside_staging(tmp_path):
    outside = tmp_path / "private.wav"
    outside.write_bytes(b"private")
    references.cleanup_upload(tmp_path, outside)
    assert outside.read_bytes() == b"private"
    owned = staged(tmp_path, b"temporary")
    references.cleanup_upload(tmp_path, owned)
    assert not owned.exists()
    assert not owned.parent.exists()


def test_cpu_import_preserves_sources_active_voices_and_transcript(tmp_path, decoder):
    uploaded_bytes = source_wave(rate=24000)
    uploaded = staged(tmp_path, uploaded_bytes)
    store = ProfileStore(tmp_path)
    previous_es = store.save_candidate("es-narrador", uploaded, "Anterior.", {})
    store.approve("es-narrador", previous_es["candidate_id"])
    previous_en = store.save_candidate("en-storyteller", uploaded, "The English voice.", {})
    store.approve("en-storyteller", previous_en["candidate_id"])
    english_index = (tmp_path / "profiles/en-storyteller/profile.json").read_bytes()
    engine = TTSEngine(tmp_path)
    with patch.object(engine, "_load_model", side_effect=AssertionError("Must not load Qwen")), \
         patch.object(engine, "_import_torch", side_effect=AssertionError("Must not import Torch")):
        result = engine.run_job("reference", {"profile_id": "es-narrador", "text": TEXT,
            "reference_upload_path": str(uploaded), "start_seconds": 0.5, "duration_seconds": 5.0,
            "original_filename": "narrador.wav", "master": False, "pronunciations": {"ciencia": "CHANGED"}})
    assert result["kind"] == "reference" and result["source"] == "local_authorized_reference_import"
    assert result["approved"] is False
    assert result["duration_seconds"] == 5.0
    assert result["metrics"]["model_load_seconds"] == 0
    assert result["metrics"]["cuda_peak_allocated_gib"] is None
    assert (Path(result["raw_audio_path"]).parent / "source.wav").read_bytes() == uploaded_bytes
    candidate = store.candidate("es-narrador", result["candidate_id"])
    assert candidate["reference_text"] == TEXT
    assert candidate["source"] == "local_authorized_reference_import"
    assert candidate["sample_rate"] == 24000
    assert sf.info(candidate["audio_path"]).subtype == "FLOAT"
    assert store.status("es-narrador")["active_candidate_id"] == previous_es["candidate_id"]
    assert store.status("es-narrador")["draft_candidate_id"] == result["candidate_id"]
    assert (tmp_path / "profiles/en-storyteller/profile.json").read_bytes() == english_index
    assert uploaded.read_bytes() == uploaded_bytes


@pytest.mark.parametrize("seconds,amplitude,start,duration,error", [
    (6, 0.0, 0, 5, "silencioso"), (6, 0.2, 0, 15, "termina antes"),
    (6, 0.2, 3, 5, "entre 5 y 30"),
])
def test_silent_or_out_of_bounds_segments_never_replace_a_profile(tmp_path, decoder, seconds, amplitude, start, duration, error):
    source = staged(tmp_path, source_wave(seconds, amplitude, rate=24000))
    engine = TTSEngine(tmp_path)
    with pytest.raises(audio.AudioError, match=error):
        engine.run_job("reference", {"profile_id": "es-narrador", "text": TEXT,
            "reference_upload_path": str(source), "start_seconds": start, "duration_seconds": duration,
            "original_filename": "sample.wav", "master": False})
    assert not engine.get_profiles()[0]["draft_ready"]
    assert not engine._job_lock.locked()


def finish(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    pytest.fail("Import did not finish")


def test_http_import_preview_approval_restore_and_staging_cleanup(tmp_path, decoder):
    engine = TTSEngine(tmp_path)
    with patch.object(audio, "master_wav", side_effect=audio.AudioError("Test preserves raw WAV")), \
         TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
        response = client.post("/api/profiles/es-narrador/reference", files={"audio": ("../../sample.wav", source_wave(rate=24000), "audio/wav")},
            data={"text": TEXT, "start_seconds": "0", "duration_seconds": "5"})
        assert response.status_code == 202, response.text
        job = finish(client, response.json()["id"])
        assert job["state"] == "completed", job
        assert job["kind"] == "reference"
        assert "reference_upload_path" not in json.dumps(job)
        candidate = job["result"]["candidate_id"]
        profiles = client.get("/api/profiles").json()["profiles"]
        assert not profiles[0]["approved"] and profiles[0]["draft_ready"]
        assert client.get(f"/api/profiles/es-narrador/audio?variant=draft&v={candidate}").content[:4] == b"RIFF"
        assert client.post("/api/profiles/es-narrador/approve", json={"candidate_id": candidate}).status_code == 200
    assert not list((tmp_path / "incoming").glob("*/*"))
    with TestClient(create_app(tmp_path, engine=TTSEngine(tmp_path)), base_url=BASE, headers=HEADERS) as reopened:
        restored = reopened.get("/api/jobs").json()["jobs"]
        assert len(restored) == 1 and restored[0]["kind"] == "reference"
        assert reopened.get(restored[0]["result"]["raw_audio_url"]).content[:4] == b"RIFF"


@pytest.mark.parametrize("filename,data,fields,expected", [
    ("sample.wav", b"not wav", {"text": TEXT}, 422),
    ("sample.m3u", b"#EXTM3U\nhttp://example.invalid/audio.mp3", {"text": TEXT}, 422),
    ("sample.wav", source_wave(rate=24000), {"text": TEXT, "start_seconds": "nan"}, 422),
    ("sample.wav", source_wave(rate=24000), {"text": TEXT, "duration_seconds": "31"}, 422),
    ("sample.wav", source_wave(rate=24000), {"text": "x" * 501}, 422),
])
def test_http_rejects_invalid_reference_before_queuing(tmp_path, filename, data, fields, expected):
    engine = TTSEngine(tmp_path)
    with TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
        response = client.post("/api/profiles/es-narrador/reference", files={"audio": (filename, data)}, data=fields)
        assert response.status_code == expected, response.text
        assert not client.get("/api/jobs").json()["jobs"]
    assert not list((tmp_path / "incoming").glob("*/*"))


def test_actual_body_and_file_limits_and_origin_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(references, "MAX_MULTIPART_BYTES", 2000)
    engine = TTSEngine(tmp_path)
    with TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
        # A streaming body has no Content-Length: the endpoint must count bytes.
        chunks = (part for part in [b"x" * 1001, b"x" * 1001])
        assert client.post("/api/profiles/es-narrador/reference", content=chunks,
            headers={"Content-Type": "multipart/form-data; boundary=test"}).status_code == 413
        assert client.post("/api/profiles/es-narrador/reference", files={"audio": ("sample.wav", source_wave(.03, rate=24000))},
            data={"text": TEXT}).status_code == 413
        assert client.post("/api/profiles/es-narrador/reference", headers={"Origin": "https://evil.example"},
            files={"audio": ("sample.wav", b"RIFF")}, data={"text": TEXT}).status_code == 403
        assert not client.get("/api/jobs").json()["jobs"]


def test_http_failure_cleans_staging_and_keeps_original_profile(tmp_path, decoder):
    engine = TTSEngine(tmp_path)
    with TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
        response = client.post("/api/profiles/es-narrador/reference", files={"audio": ("silence.wav", source_wave(amplitude=0, rate=24000))},
            data={"text": TEXT, "duration_seconds": "5"})
        job = finish(client, response.json()["id"])
        assert job["state"] == "failed" and "silencioso" in job["error"]
    assert not list((tmp_path / "incoming").glob("*/*"))
    assert not ProfileStore(tmp_path).status("es-narrador")["draft_ready"]


def test_queued_reference_uses_existing_worker_and_cancel_removes_only_its_upload(tmp_path, monkeypatch):
    engine = TTSEngine(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_job(kind, payload, progress):
        calls.append(kind)
        started.set()
        release.wait(timeout=5)
        return {}

    monkeypatch.setattr(engine, "run_job", blocked_job)
    with TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
        try:
            design = client.post("/api/jobs", json={"kind": "design", "profile_id": "es-narrador", "text": TEXT}).json()
            assert started.wait(timeout=2)
            response = client.post("/api/profiles/es-narrador/reference", files={"audio": ("sample.wav", source_wave(rate=24000))},
                data={"text": TEXT, "duration_seconds": "5"})
            assert response.status_code == 202
            reference = response.json()
            assert reference["state"] == "queued" and reference["kind"] == "reference"
            uploads = list((tmp_path / "incoming").glob("*/source.wav"))
            assert len(uploads) == 1
            unrelated = tmp_path / "incoming" / "keep.txt"
            unrelated.write_text("keep")
            assert client.post(f"/api/jobs/{reference['id']}/cancel", json={}).json()["state"] == "cancelled"
            assert not uploads[0].exists() and unrelated.read_text() == "keep"
            assert calls == ["design"]
            # JSON clients cannot submit arbitrary local reference paths.
            assert client.post("/api/jobs", json={"kind": "reference", "profile_id": "es-narrador", "text": TEXT,
                "reference_upload_path": str(unrelated)}).status_code == 422
        finally:
            release.set()
        assert finish(client, design["id"])["state"] == "completed"
