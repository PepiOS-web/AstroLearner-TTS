"""HTTP boundaries and serialized inference; no real model or GPU is needed."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astro_tts.server import create_app
from astro_tts.engine import TTSEngine
from astro_tts.profiles import ProfileStore

BASE = "http://127.0.0.1:8095"
HEADERS = {"Origin": BASE}


def wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b"\x00\x00" * 2400)
    return path


class FakeEngine:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.profiles = [
            {"id": "es-narrador", "name": "Narrador español", "language": "Spanish", "approved": False, "draft_ready": False, "description": "Una voz original, cálida y clara."},
            {"id": "en-storyteller", "name": "English storyteller", "language": "English", "approved": False, "draft_ready": False},
        ]
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.fail_next = False
        self.output_override = None
        self.active = 0
        self.max_active = 0

    def get_profiles(self):
        return copy.deepcopy(self.profiles)

    def doctor(self):
        return {"ready": True, "gpu": {"available": True, "name": "Fake GPU"}}

    def approve_profile(self, profile_id, candidate_id=None):
        profile = next(p for p in self.profiles if p["id"] == profile_id)
        if not profile["draft_ready"]:
            raise ValueError("No hay borrador.")
        if candidate_id and profile.get("draft_candidate_id") != candidate_id:
            raise ValueError("Este borrador ha cambiado.")
        profile.update(approved=True, draft_ready=False, audio_path=profile["draft_audio_path"], active_candidate_id=profile["draft_candidate_id"])
        return copy.deepcopy(profile)

    def run_job(self, kind, payload, progress):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append((kind, copy.deepcopy(payload)))
        self.started.set()
        try:
            progress("Preparando", 0.1)
            if self.block:
                self.release.wait(timeout=10)
            progress("Inferencia terminada", 0.8)
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("Fallo de inferencia controlado")
            candidate = str(uuid.uuid4())
            path = self.output_override or wav(self.runtime / "jobs" / candidate / "original.wav")
            p = next(p for p in self.profiles if p["id"] == payload["profile_id"])
            if kind == "design":
                p.update(draft_ready=True, draft_candidate_id=candidate, draft_audio_path=str(path))
            return {"audio_path": str(path), "raw_audio_path": str(path), "duration_seconds": 0.1, "metrics": {"generation_seconds": 0.03, "real_time_factor": 0.3}, "candidate_id": candidate}
        finally:
            self.active -= 1


@pytest.fixture
def studio(tmp_path):
    engine = FakeEngine(tmp_path)
    app = create_app(tmp_path, engine=engine)
    with TestClient(app, base_url=BASE, headers=HEADERS) as client:
        try:
            yield client, engine
        finally:
            engine.release.set()


def submit(client, **overrides):
    payload = {"kind": "design", "profile_id": "es-narrador", "text": "Una pequeña pregunta abre un universo.", "seed": 42}
    payload.update(overrides)
    return client.post("/api/jobs", json=payload)


def finished(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    pytest.fail("The fake job did not finish in five seconds")


def test_health_and_static_do_not_generate_or_import_a_model(studio):
    client, engine = studio
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["app"] == "AstroLearnerTTS"
    assert health.json()["doctor"]["gpu"]["name"] == "Fake GPU"
    assert client.get("/").status_code == 200
    assert "script-src 'self'" in client.get("/").headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../server.py").status_code == 404
    assert not engine.calls


def test_host_and_origin_guards_block_remote_sites(studio):
    client, engine = studio
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 403
    assert submit(client, text="válido").status_code == 202
    for origin in ("https://evil.example", "null", "http://127.0.0.1:9000", "http://localhost.evil.example:8095"):
        assert client.post("/api/jobs", headers={"Origin": origin}, json={}).status_code == 403
        assert client.get("/api/profiles", headers={"Origin": origin}).status_code == 403
    with TestClient(create_app(engine.runtime, engine=engine), base_url=BASE) as no_origin:
        assert no_origin.post("/api/jobs", json={}).status_code == 403
        assert no_origin.get("/api/health").status_code == 200


@pytest.mark.parametrize("field,value", [("text", "   "), ("text", "x" * 18001), ("text", 123), ("seed", -1), ("seed", 4294967296), ("seed", True), ("seed", "42"), ("profile_id", "../../secret"), ("kind", "clone"), ("pronunciations", {"word": 42}), ("pronunciations", ["word"]), ("instruct", "x" * 3001)])
def test_input_contract_is_strict_and_bounded(studio, field, value):
    client, engine = studio
    assert submit(client, **{field: value}).status_code == 422
    assert not engine.calls


def test_design_must_be_explicitly_approved_before_narration(studio):
    client, engine = studio
    assert submit(client, kind="narrate").status_code == 409
    job = finished(client, submit(client).json()["id"])
    assert job["state"] == "completed"
    candidate = job["result"]["candidate_id"]
    assert submit(client, kind="narrate").status_code == 409
    assert client.post("/api/profiles/es-narrador/approve", json={"candidate_id": "stale-candidate"}).status_code == 409
    approved = client.post("/api/profiles/es-narrador/approve", json={"candidate_id": candidate})
    assert approved.status_code == 200
    assert approved.json()["approved"] is True
    assert approved.json()["active_audio_url"].endswith("variant=active")
    narration = finished(client, submit(client, kind="narrate", pronunciations={"AstroLearner": "Astro Lérner"}).json()["id"])
    assert narration["state"] == "completed"
    assert engine.calls[-1][1]["pronunciations"] == {"AstroLearner": "Astro Lérner"}


def test_downloads_use_only_registered_wav_paths(studio, tmp_path):
    client, engine = studio
    job = finished(client, submit(client).json()["id"])
    url = job["result"]["audio_url"]
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"
    assert "audio_path" not in job["result"]
    assert client.get(f"/api/jobs/{uuid.uuid4()}/audio").status_code == 404
    assert client.get(url.replace("variant=main", "variant=../../server.py")).status_code == 422
    # A harmless-looking extension outside the runtime audio roots is refused.
    engine.output_override = wav(tmp_path / "private.wav")
    outside = finished(client, submit(client).json()["id"])
    assert "audio_url" not in outside["result"]
    assert client.get(f"/api/jobs/{outside['id']}/audio").status_code == 404
    # Files with a .wav suffix must also contain an actual RIFF/WAVE header.
    invalid = tmp_path / "outputs" / "source.wav"
    invalid.parent.mkdir(exist_ok=True)
    invalid.write_text("print('source must not be served')")
    engine.output_override = invalid
    invalid_job = finished(client, submit(client).json()["id"])
    assert client.get(f"/api/jobs/{invalid_job['id']}/audio").status_code == 404


def test_single_worker_bounded_queue_and_pending_cancellation(studio):
    client, engine = studio
    engine.block = True
    running = submit(client).json()
    assert engine.started.wait(timeout=2)
    queued = [submit(client).json() for _ in range(8)]
    assert submit(client).status_code == 429
    # Polling and health remain usable while a model forward is blocked.
    assert client.get("/api/health").status_code == 200
    assert client.get(f"/api/jobs/{running['id']}").json()["state"] == "running"
    assert client.post("/api/profiles/es-narrador/approve", json={"candidate_id": "draft"}).status_code == 409
    cancelled = client.post(f"/api/jobs/{queued[0]['id']}/cancel", json={}).json()
    assert cancelled["state"] == "cancelled"
    replacement = submit(client)
    assert replacement.status_code == 202
    engine.release.set()
    for item in [running, *queued[1:], replacement.json()]:
        assert finished(client, item["id"])["state"] == "completed"
    assert engine.max_active == 1
    assert len(engine.calls) == 9


def test_running_cancellation_waits_for_safe_callback(studio):
    client, engine = studio
    engine.block = True
    job_id = submit(client).json()["id"]
    assert engine.started.wait(timeout=2)
    request = client.post(f"/api/jobs/{job_id}/cancel", json={}).json()
    assert request["state"] == "cancel_requested"
    assert client.get(f"/api/jobs/{job_id}").json()["state"] == "cancel_requested"
    assert engine.active == 1
    engine.release.set()
    assert finished(client, job_id)["state"] == "cancelled"
    assert client.get(f"/api/jobs/{job_id}/audio").status_code == 404


def test_failed_inference_does_not_stop_the_worker(studio):
    client, engine = studio
    engine.fail_next = True
    failed = finished(client, submit(client).json()["id"])
    assert failed["state"] == "failed"
    assert failed["error"] == "Fallo de inferencia controlado"
    succeeded = finished(client, submit(client).json()["id"])
    assert succeeded["state"] == "completed"
    assert not any(key.startswith("_") for key in failed)
    assert len(client.get("/api/jobs").json()["jobs"]) == 2


def test_profile_audio_and_approval_reject_unknown_profiles(studio):
    client, _ = studio
    assert client.get("/api/profiles/not-a-profile/audio").status_code == 404
    assert client.get("/api/profiles/es-narrador/audio?variant=draft").status_code == 404
    assert client.post("/api/profiles/es-narrador/approve", json={"candidate_id": "draft"}).status_code == 409
    assert client.post("/api/profiles/not-a-profile/approve", json={"candidate_id": "draft"}).status_code == 404
    assert client.post("/api/profiles/es-narrador/approve", json={}).status_code == 422


def test_real_profile_store_rejects_stale_approval_and_audio_identity(tmp_path):
    store = ProfileStore(tmp_path)
    source = wav(tmp_path / "source.wav")
    first = store.save_candidate("es-narrador", source, "Primera voz.", {})["candidate_id"]
    engine = TTSEngine(tmp_path)
    with TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
        draft_url = f"/api/profiles/es-narrador/audio?variant=draft&v={first}"
        assert client.get(draft_url).status_code == 200
        second = store.save_candidate("es-narrador", source, "Segunda voz.", {})["candidate_id"]
        assert client.get(draft_url).status_code == 409
        assert client.post("/api/profiles/es-narrador/approve", json={"candidate_id": first}).status_code == 409
        assert not store.status("es-narrador")["approved"]
        assert client.get(f"/api/profiles/es-narrador/audio?variant=draft&v={second}").status_code == 200
        assert client.post("/api/profiles/es-narrador/approve", json={"candidate_id": second}).status_code == 200
        active_url = f"/api/profiles/es-narrador/audio?variant=active&v={second}"
        assert client.get(active_url).status_code == 200
        assert client.get(f"/api/profiles/es-narrador/audio?variant=draft&v={second}").status_code == 409
        third = store.save_candidate("es-narrador", source, "Tercera voz.", {})["candidate_id"]
        assert client.get(active_url).status_code == 200
        assert client.post("/api/profiles/es-narrador/approve", json={"candidate_id": third}).status_code == 200
        assert client.get(active_url).status_code == 409
        assert client.get("/api/profiles/es-narrador/audio?variant=active&v=invalid").status_code == 422


def saved_result(runtime, job_id, **overrides):
    directory = runtime / "jobs" / job_id
    original = wav(directory / "original.wav")
    result = {
        "job_id": job_id, "kind": "narrate", "profile_id": "es-narrador",
        "candidate_id": uuid.uuid4().hex, "approved": True,
        "audio_path": str(original), "raw_audio_path": str(original), "master_audio_path": None,
        "duration_seconds": 0.1, "sample_rate": 24000,
        "segments": [{"text": "Una historia.", "audio_path": str(original)}],
        "metrics": {"generation_seconds": 0.03}, "warnings": [],
    }
    result.update(overrides)
    path = directory / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path, result


def test_completed_history_survives_restart_with_stable_ids_and_safe_downloads(tmp_path):
    outside = wav(tmp_path / "private.wav")
    saved_result(tmp_path, "20261108-120000-123456789abc", audio_url="https://untrusted.example/audio.wav", master_audio_path=str(outside))
    engine = FakeEngine(tmp_path)
    for _ in range(2):
        with TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
            jobs = client.get("/api/jobs").json()["jobs"]
            assert len(jobs) == 1
            job = jobs[0]
            assert job["state"] == "completed"
            assert job["kind"] == "narrate"
            assert uuid.UUID(job["id"]).version == 5
            if _ == 0:
                restored_id = job["id"]
            else:
                assert job["id"] == restored_id
            assert client.get(f"/api/jobs/{job['id']}").status_code == 200
            assert client.get(job["result"]["audio_url"]).content[:4] == b"RIFF"
            assert client.get(job["result"]["raw_audio_url"]).status_code == 200
            assert "master_audio_url" not in job["result"]
            assert "audio_path" not in job["result"]["segments"][0]
            assert "untrusted.example" not in json.dumps(job)
            assert not engine.calls


def test_new_queued_narration_is_restored_after_restart(tmp_path):
    class PersistingFakeEngine(FakeEngine):
        def run_job(self, kind, payload, progress):
            result = super().run_job(kind, payload, progress)
            directory = Path(result["audio_path"]).parent
            result.update(job_id=directory.name, kind=kind, profile_id=payload["profile_id"])
            (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
            return result

    engine = PersistingFakeEngine(tmp_path)
    engine.profiles[0]["approved"] = True
    with TestClient(create_app(tmp_path, engine=engine), base_url=BASE, headers=HEADERS) as client:
        produced = finished(client, submit(client, kind="narrate").json()["id"])
        assert produced["state"] == "completed"
    with TestClient(create_app(tmp_path, engine=FakeEngine(tmp_path)), base_url=BASE, headers=HEADERS) as client:
        restored = client.get("/api/jobs").json()["jobs"]
        assert len(restored) == 1
        assert restored[0]["result"]["job_id"] == produced["result"]["job_id"]
        assert restored[0]["kind"] == "narrate"
        assert client.get(restored[0]["result"]["audio_url"]).status_code == 200


def test_completed_history_reads_only_twenty_recent_results(tmp_path):
    for index in range(25):
        path, _ = saved_result(tmp_path, f"20261108-120000-{index:012x}")
        os.utime(path, (1000 + index, 1000 + index))
    with TestClient(create_app(tmp_path, engine=FakeEngine(tmp_path)), base_url=BASE, headers=HEADERS) as client:
        jobs = client.get("/api/jobs").json()["jobs"]
        assert len(jobs) == 20
        assert [job["result"]["job_id"] for job in jobs] == [f"20261108-120000-{index:012x}" for index in range(24, 4, -1)]


@pytest.mark.parametrize("invalid", ["outside", "other-job", "interrupted", "unknown-profile", "mismatched-id", "invalid-json"])
def test_completed_history_rejects_invalid_or_interrupted_results(tmp_path, invalid):
    path, result = saved_result(tmp_path, "20261108-120000-123456789abc")
    if invalid == "outside":
        result["audio_path"] = str(wav(tmp_path / "outside.wav"))
    elif invalid == "other-job":
        result["audio_path"] = str(wav(tmp_path / "jobs" / "other-job" / "original.wav"))
    elif invalid == "interrupted":
        (path.parent / "status.json").write_text('{"status":"interrupted"}', encoding="utf-8")
    elif invalid == "unknown-profile":
        result["profile_id"] = "unexpected-profile"
    elif invalid == "mismatched-id":
        result["job_id"] = "another-job"
    path.write_text("{broken" if invalid == "invalid-json" else json.dumps(result), encoding="utf-8")
    with TestClient(create_app(tmp_path, engine=FakeEngine(tmp_path)), base_url=BASE, headers=HEADERS) as client:
        assert client.get("/api/jobs").json()["jobs"] == []
