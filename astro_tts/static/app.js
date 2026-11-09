const $ = (id) => document.getElementById(id);
const examples = {
  "es-narrador": "Hay un instante, justo antes del amanecer, en el que todo parece posible. Las estrellas siguen ahí, aunque ya no podamos verlas. En AstroLearner exploramos esas pequeñas preguntas que abren universos enteros. Hoy vamos a mirar un poco más lejos, con curiosidad, con calma y con los pies en la Tierra.",
  "en-storyteller": "There is a moment, just before sunrise, when anything feels possible. The stars are still there, even when we can no longer see them. At AstroLearner, we explore the little questions that open up entire universes. Today, let's look a little further, with curiosity, a sense of wonder, and our feet firmly on the ground.",
};
const labels = { queued: "En cola", running: "Generando", cancel_requested: "Cancelación solicitada", completed: "Completado", cancelled: "Cancelado", failed: "Error" };
const terminal = new Set(["completed", "failed", "cancelled"]);
const state = { profiles: [], jobs: [], profileId: "es-narrador", mode: "design", forms: new Map(), referenceForms: new Map(), referenceSourceUrl: null, referenceSubmitting: false, jobId: null, audioKey: "", listened: new Set(), candidateId: null, audioVariants: {}, variant: "main", connected: false, polling: false, completionSignature: "", polls: 0 };
let toastTimer;

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

async function api(path, options = {}) {
  const headers = options.body instanceof FormData ? { ...(options.headers || {}) } : { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  let data;
  try { data = await response.json(); } catch { throw new Error("El estudio no ha enviado una respuesta válida."); }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((e) => e.msg).join(" · ") : data.detail;
    throw new Error(detail || "No se pudo completar la solicitud.");
  }
  return data;
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
}

function profile() { return state.profiles.find((p) => p.id === state.profileId); }
function formKey() { return `${state.profileId}:${state.mode}`; }
function saveForm() {
  state.forms.set(formKey(), { text: $("script-text").value, instruct: $("instruct").value, seed: $("seed").value, pronunciations: $("pronunciations").value });
  saveReferenceForm();
}
function restoreForm() {
  const current = profile();
  const values = state.forms.get(formKey()) || { text: examples[state.profileId] || "", instruct: current?.instruct || current?.description || "", seed: "42", pronunciations: "" };
  $("script-text").value = values.text;
  $("instruct").value = values.instruct;
  $("seed").value = values.seed;
  $("pronunciations").value = values.pronunciations;
  updateCount();
  restoreReferenceForm();
}
function updateCount() { $("text-count").textContent = `${$("script-text").value.length.toLocaleString("es-ES")} / ${state.mode === "design" ? "500" : "18.000"}`; }

function saveReferenceForm() {
  const previous = state.referenceForms.get(state.profileId) || {};
  state.referenceForms.set(state.profileId, { file: $("reference-audio").files[0] || previous.file || null, text: $("reference-text").value, start: $("reference-start").value, duration: $("reference-duration").value });
}

function updateReferenceCount() {
  $("reference-text-count").textContent = `${$("reference-text").value.length} / 500`;
}

function setReferencePreview(file) {
  const player = $("reference-source-player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  if (state.referenceSourceUrl) URL.revokeObjectURL(state.referenceSourceUrl);
  state.referenceSourceUrl = null;
  player.hidden = !file;
  if (file) {
    state.referenceSourceUrl = URL.createObjectURL(file);
    player.src = state.referenceSourceUrl;
    player.load();
  }
  $("reference-file-info").textContent = file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} MiB · archivo guardado para este perfil` : "MP3, WAV, FLAC, M4A u OGG · máximo 25 MiB.";
}

function restoreReferenceForm() {
  const values = state.referenceForms.get(state.profileId) || { file: null, text: "", start: "0", duration: "15" };
  $("reference-audio").value = "";
  $("reference-text").value = values.text;
  $("reference-start").value = values.start;
  $("reference-duration").value = values.duration;
  setReferencePreview(values.file);
  updateReferenceCount();
}

function isVoicePreparation(kind) { return kind === "design" || kind === "reference"; }

function renderProfiles() {
  const list = $("profile-list");
  list.replaceChildren();
  for (const p of state.profiles) {
    const card = element("button", `profile-card${p.id === state.profileId ? " selected" : ""}`);
    card.type = "button";
    card.setAttribute("aria-pressed", String(p.id === state.profileId));
    const top = element("div", "profile-card-top");
    top.append(element("span", "profile-icon", p.id.startsWith("es") ? "ES" : "EN"), element("span", "selection-indicator"));
    card.append(top, element("h3", "", p.name || (p.id.startsWith("es") ? "Narrador español" : "English storyteller")), element("span", "profile-language", p.id.startsWith("es") ? "Español · Narración" : "English · Storytelling"), element("span", `profile-status${p.approved ? " approved" : ""}`, p.approved ? (p.draft_ready ? "Aprobada · nuevo borrador" : "Voz aprobada") : (p.draft_ready ? "Borrador para escuchar" : "Pendiente de diseño")));
    card.addEventListener("click", () => switchView(p.id, state.mode));
    list.append(card);
  }
}

function renderMode() {
  const design = state.mode === "design";
  for (const button of document.querySelectorAll(".tab")) {
    const active = button.dataset.mode === state.mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  }
  $("editor").setAttribute("aria-labelledby", `tab-${state.mode}`);
  $("editor-kicker").textContent = design ? "DISEÑO DE VOZ" : "NARRACIÓN DE GUION";
  $("editor-title").textContent = design ? "Dale carácter." : "Dale vida a tu historia.";
  $("editor-description").textContent = design ? "Describe una voz o usa una grabación de referencia. Escucha la muestra antes de aprobarla." : "Tu voz aprobada, al servicio del guion. Separa los párrafos para marcar cada idea.";
  $("selected-language").textContent = state.profileId.startsWith("es") ? "ESPAÑOL" : "ENGLISH";
  $("style-field").hidden = !design;
  $("reference-section").hidden = !design;
  $("text-label").textContent = design ? "Texto de prueba" : "Tu guion";
  $("script-text").maxLength = design ? 500 : 18000;
  $("text-help").textContent = design ? "Una muestra breve ayuda a comparar voces." : "Cada párrafo se procesa como un bloque de narración.";
  $("generate-label").textContent = design ? "Generar muestra" : "Generar narración";
  $("submit-note").textContent = design ? "Tu voz aprobada se conserva mientras exploras nuevos diseños." : "El motor trabaja por párrafos. Puedes seguir su progreso aquí.";
  $("approval-notice").hidden = design || Boolean(profile()?.approved);
  $("approval-notice").textContent = "Primero prepara, escucha y aprueba una voz para este perfil.";
  $("generate-button").disabled = !state.connected || (!design && !profile()?.approved);
  $("reference-button").disabled = !state.connected || state.referenceSubmitting;
  $("reference-button").textContent = state.referenceSubmitting ? "Enviando referencia…" : "Crear voz desde audio ↗";
  $("preview-bottom-note").textContent = design ? "Escuchar → Ajustar → Aprobar" : "Escribir → Narrar → Descargar";
  updateCount();
}

function switchView(profileId, mode, jobId = null) {
  saveForm();
  const previousAudio = $("audio-player");
  previousAudio.pause();
  previousAudio.removeAttribute("src");
  previousAudio.load();
  state.profileId = profileId;
  state.mode = mode === "reference" ? "design" : mode;
  state.jobId = jobId;
  state.audioKey = "";
  state.variant = "main";
  $("form-error").hidden = true;
  $("reference-error").hidden = true;
  restoreForm();
  renderProfiles();
  renderMode();
  renderPreview();
}

function numeric(result, keys) {
  for (const key of keys) if (typeof result?.[key] === "number" && Number.isFinite(result[key]) && result[key] >= 0) return result[key];
  return null;
}
function renderMetrics(result) {
  const container = $("metrics");
  container.replaceChildren();
  result = { ...(result?.metrics || {}), ...(result || {}) };
  const duration = numeric(result, ["duration_seconds", "duration_s", "duration"]);
  const generation = numeric(result, ["generation_seconds", "generation_time_s", "generation_time_seconds", "elapsed_seconds"]);
  const rtf = numeric(result, ["rtf", "real_time_factor"]);
  for (const [label, value] of [["Duración", duration === null ? null : `${duration.toFixed(1)} s`], ["Generación", generation === null ? null : `${generation.toFixed(1)} s`], ["RTF", rtf === null ? null : `${rtf.toFixed(2)}×`]]) {
    if (value === null) continue;
    const metric = element("div", "metric");
    metric.append(element("strong", "", value), element("span", "", label));
    container.append(metric);
  }
}

function updateApproval() {
  const candidate = state.candidateId;
  const draftMatches = Boolean(candidate && candidate === profile()?.draft_candidate_id && profile()?.draft_ready);
  const busy = state.jobs.some((j) => j.profile_id === state.profileId && isVoicePreparation(j.kind) && !terminal.has(j.state));
  const show = state.mode === "design" && draftMatches;
  $("approve-button").hidden = !show;
  $("approval-help").hidden = !show;
  $("approve-button").disabled = !candidate || !state.listened.has(candidate) || busy;
  $("approve-button").textContent = busy ? "Preparando voz…" : (state.listened.has(candidate) ? "Aprobar esta voz" : "Escucha hasta el final");
  $("approval-help").textContent = busy ? "La muestra se podrá aprobar cuando termine la nueva preparación de voz." : "Escucha la muestra completa. Después, aprueba esta versión para narrar.";
}

function setAudio(variants, result, candidateId, key) {
  const available = Object.fromEntries(Object.entries(variants).filter(([, url]) => Boolean(url)));
  state.audioVariants = available;
  state.candidateId = candidateId;
  const names = { main: result?.kind === "reference" ? "Referencia" : (state.mode === "narrate" ? "Resultado" : "Muestra"), raw: "Audio original", master: "Máster" };
  if (!available[state.variant]) state.variant = available.master ? "master" : Object.keys(available)[0];
  const audio = $("audio-player");
  if (state.audioKey !== `${key}:${state.variant}`) {
    audio.pause();
    audio.src = available[state.variant];
    audio.load();
    state.audioKey = `${key}:${state.variant}`;
  }
  $("audio-container").hidden = false;
  $("audio-label").textContent = state.mode === "design" ? "Muestra de voz · escucha antes de aprobar" : (names[state.variant] || "Narración");
  $("download-audio").href = available[state.variant];
  const buttons = $("audio-variants");
  buttons.replaceChildren();
  if (Object.keys(available).length > 1) {
    for (const variant of Object.keys(available)) {
      const button = element("button", `variant-button${variant === state.variant ? " active" : ""}`, names[variant] || variant);
      button.type = "button";
      button.addEventListener("click", () => { state.variant = variant; renderPreview(); });
      buttons.append(button);
    }
  }
  renderMetrics(result);
  updateApproval();
}

function renderPreview() {
  const p = profile();
  const job = state.jobs.find((j) => j.id === state.jobId);
  const busy = job && !terminal.has(job.state);
  $("job-progress").hidden = !busy;
  if (busy) {
    $("job-state").textContent = labels[job.state];
    $("progress-message").textContent = job.progress?.message || "El motor local está preparando el audio…";
    const fraction = numeric(job.progress, ["fraction", "progress"]);
    if (fraction !== null) $("progress-bar").value = Math.min(1, fraction);
    else $("progress-bar").removeAttribute("value");
    $("cancel-button").disabled = job.state === "cancel_requested";
    $("cancel-button").textContent = job.state === "cancel_requested" ? "Esperando al motor…" : "Cancelar";
  }
  let result = job?.state === "completed" ? job.result : null;
  const warnings = Array.isArray(result?.warnings) ? result.warnings.filter((item) => typeof item === "string") : [];
  $("preview-warnings").textContent = warnings.slice(0, 3).join("\n");
  $("preview-warnings").hidden = !warnings.length;
  let variants = result ? { main: result.audio_url, raw: result.raw_audio_url, master: result.master_audio_url } : {};
  let candidateId = state.mode === "design" ? result?.candidate_id || result?.draft_candidate_id || null : null;
  let key = job?.id || "";
  if (!job && p) {
    const draft = state.mode === "design" && p.draft_ready && p.draft_audio_url;
    const url = draft ? p.draft_audio_url : p.active_audio_url;
    candidateId = draft ? p.draft_candidate_id : null;
    if (url) variants = { main: `${url}&v=${encodeURIComponent(draft ? p.draft_candidate_id || "draft" : p.active_candidate_id || "active")}` };
    key = `profile:${p.id}:${draft ? p.draft_candidate_id : p.active_candidate_id}`;
  }
  const hasAudio = Object.values(variants).some(Boolean);
  const currentDraft = candidateId && p?.draft_ready && candidateId === p?.draft_candidate_id;
  const approvedDesign = candidateId && candidateId === p?.active_candidate_id;
  $("preview-status").textContent = busy ? labels[job.state].toUpperCase() : (job?.state === "failed" ? "REVISIÓN NECESARIA" : (job?.state === "cancelled" ? "TRABAJO CANCELADO" : (hasAudio ? (candidateId ? (currentDraft ? "BORRADOR · SIN APROBAR" : (approvedDesign ? "VOZ APROBADA" : "MUESTRA DE DISEÑO")) : (state.mode === "narrate" && job ? "NARRACIÓN PREPARADA" : "VOZ APROBADA")) : "LISTO PARA CREAR")));
  $("preview-name").textContent = hasAudio || busy ? p?.name || "Voz AstroLearner" : "Tu próxima voz está aquí.";
  $("preview-caption").textContent = busy ? "Puedes seguir trabajando. El audio aparecerá cuando termine." : (job?.state === "failed" ? job.error || "Revisa el estado del motor y vuelve a intentarlo." : (job?.state === "cancelled" ? "El proceso se ha detenido en un punto seguro." : (hasAudio ? (currentDraft ? "Este borrador aún no se usa para narrar." : (candidateId && !approvedDesign ? "Versión anterior. Solo el borrador actual se puede aprobar." : (job && state.mode === "narrate" ? "Escucha el resultado y descarga tu archivo WAV." : "Esta es la identidad que se utiliza para narrar."))) : "Genera una muestra y tómate un momento para escucharla.")));
  if (hasAudio) setAudio(variants, result, candidateId, key);
  else {
    if (state.audioKey) { $("audio-player").pause(); $("audio-player").removeAttribute("src"); $("audio-player").load(); }
    state.audioKey = "";
    state.candidateId = null;
    $("audio-container").hidden = true;
  }
}

function renderJobs() {
  $("activity-count").textContent = String(state.jobs.length);
  const container = $("job-list");
  container.replaceChildren();
  if (!state.jobs.length) { container.append(element("p", "empty-activity", "Tus muestras y narraciones aparecerán aquí.")); return; }
  for (const job of state.jobs) {
    const row = element("div", "job-row");
    const p = state.profiles.find((v) => v.id === job.profile_id);
    const info = element("div", "job-info");
    const kindName = job.kind === "reference" ? "Referencia de audio" : (job.kind === "design" ? "Diseño de voz" : "Narración");
    info.append(element("strong", "", `${kindName} · ${p?.name || job.profile_id}`), element("small", "", job.error || job.progress?.message || labels[job.state]));
    row.append(element("span", "job-icon", job.kind === "reference" ? "↥" : (job.kind === "design" ? "✧" : "≋")), info, element("span", `job-tag ${job.state}`, labels[job.state] || job.state));
    const action = element("button", "job-action", job.state === "completed" ? "Escuchar ↗" : "Ver ↗");
    action.type = "button";
    action.addEventListener("click", () => switchView(job.profile_id, job.kind, job.id));
    row.append(action);
    container.append(row);
  }
}

async function refreshProfiles() {
  const data = await api("/api/profiles");
  state.profiles = data.profiles || [];
  renderProfiles();
  renderMode();
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    const doctor = health.doctor || {};
    state.connected = Boolean(health.ok);
    $("system-light").className = `status-light ${health.ok ? "ready" : "offline"}`;
    $("system-label").textContent = health.ok ? (doctor.ready === false ? "Estudio conectado · revisar motor" : "Estudio local conectado") : "Motor pendiente de revisión";
    const gpu = doctor.gpu_name || (typeof doctor.gpu === "string" ? doctor.gpu : doctor.gpu?.name) || doctor.cuda_device_name;
    $("gpu-label").textContent = gpu || (doctor.gpu?.available === false || doctor.cuda_available === false || doctor.torch_cuda_available === false ? "GPU no verificada" : "Estado del motor disponible");
    $("gpu-detail").textContent = doctor.error || doctor.warnings?.[0] || (doctor.model_loaded === true ? "Modelo cargado en el motor local." : "El modelo se carga al generar.");
    $("connection-detail").textContent = `${location.hostname} · ESTUDIO LOCAL`;
  } catch {
    state.connected = false;
    $("system-light").className = "status-light offline";
    $("system-label").textContent = "Sin conexión al estudio";
    $("gpu-label").textContent = "Motor no disponible";
    $("gpu-detail").textContent = "Comprueba que el lanzador local sigue abierto.";
  }
  renderMode();
}

async function poll() {
  if (state.polling) return;
  state.polling = true;
  try {
    const data = await api("/api/jobs");
    state.jobs = data.jobs || [];
    const signature = state.jobs.filter((j) => terminal.has(j.state)).map((j) => `${j.id}:${j.state}`).join("|");
    if (signature !== state.completionSignature) {
      state.completionSignature = signature;
      await refreshProfiles();
    }
    renderJobs();
    renderPreview();
    if (++state.polls % 20 === 0) await refreshHealth();
  } catch {
    state.connected = false;
    $("system-light").className = "status-light offline";
    $("system-label").textContent = "Reconectando al estudio…";
    if (++state.polls % 5 === 0) await refreshHealth();
  } finally { state.polling = false; }
}

$("voice-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("form-error").hidden = true;
  try {
    const text = $("script-text").value.trim();
    const seed = Number($("seed").value);
    const limit = state.mode === "design" ? 500 : 18000;
    if (!text || text.length > limit) throw new Error(`Escribe un texto de entre 1 y ${limit.toLocaleString("es-ES")} caracteres.`);
    if (!Number.isInteger(seed) || seed < 0 || seed > 4294967295 || !$("seed").value.trim()) throw new Error("La semilla debe ser un entero entre 0 y 4.294.967.295.");
    const payload = { kind: state.mode, profile_id: state.profileId, text, seed };
    if (state.mode === "design") payload.instruct = $("instruct").value.trim();
    const pronunciationText = $("pronunciations").value.trim();
    if (pronunciationText) {
      try { payload.pronunciations = JSON.parse(pronunciationText); } catch { throw new Error('Revisa las pronunciaciones. Usa un objeto JSON, por ejemplo: {"AstroLearner": "Astro Lérner"}.'); }
      if (!payload.pronunciations || Array.isArray(payload.pronunciations) || typeof payload.pronunciations !== "object" || Object.values(payload.pronunciations).some((v) => typeof v !== "string")) throw new Error("Cada pronunciación debe asociar una palabra con un reemplazo de texto.");
    }
    saveForm();
    $("generate-button").disabled = true;
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    state.jobId = job.id;
    state.variant = "main";
    renderJobs();
    renderPreview();
    toast("Trabajo añadido a la cola del estudio.");
  } catch (error) { $("form-error").textContent = error.message; $("form-error").hidden = false; }
  finally { renderMode(); }
});

$("reference-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("reference-error").hidden = true;
  const submittedProfile = state.profileId;
  try {
    saveReferenceForm();
    const values = state.referenceForms.get(submittedProfile);
    const file = values.file;
    const text = values.text.trim();
    const start = Number(values.start);
    const duration = Number(values.duration);
    if (!file) throw new Error("Selecciona el archivo de la voz de referencia.");
    if (!/\.(mp3|wav|flac|m4a|ogg)$/i.test(file.name)) throw new Error("Elige un archivo MP3, WAV, FLAC, M4A u OGG.");
    if (file.size > 25 * 1024 * 1024) throw new Error("El archivo supera el máximo de 25 MiB. Exporta un fragmento más corto.");
    if (!file.size) throw new Error("El archivo de audio está vacío.");
    if (!text || text.length > 500) throw new Error("Escribe la transcripción exacta del fragmento, con un máximo de 500 caracteres.");
    if (!values.start.trim() || !Number.isFinite(start) || start < 0) throw new Error("El inicio debe ser un número de segundos igual o mayor que cero.");
    if (!values.duration.trim() || !Number.isFinite(duration) || duration < 5 || duration > 30) throw new Error("La duración del fragmento debe estar entre 5 y 30 segundos.");
    const sourceDuration = $("reference-source-player").duration;
    if (Number.isFinite(sourceDuration) && start + duration > sourceDuration + 0.05) throw new Error("El fragmento termina después del audio. Ajusta el inicio o la duración.");
    const body = new FormData();
    body.append("audio", file, file.name);
    body.append("text", text);
    body.append("start_seconds", String(start));
    body.append("duration_seconds", String(duration));
    state.referenceSubmitting = true;
    renderMode();
    const job = await api(`/api/profiles/${encodeURIComponent(submittedProfile)}/reference`, { method: "POST", body });
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    if (state.profileId === submittedProfile && state.mode === "design") {
      state.jobId = job.id;
      state.variant = "main";
    }
    renderJobs();
    renderPreview();
    toast("Referencia añadida a la cola. Escucha el fragmento preparado antes de aprobarlo.");
  } catch (error) {
    if (state.profileId === submittedProfile) {
      $("reference-error").textContent = error.message;
      $("reference-error").hidden = false;
    } else toast(error.message);
  } finally {
    state.referenceSubmitting = false;
    renderMode();
  }
});

$("reference-audio").addEventListener("change", () => {
  const previous = state.referenceForms.get(state.profileId) || {};
  const file = $("reference-audio").files[0] || null;
  state.referenceForms.set(state.profileId, { ...previous, file, text: $("reference-text").value, start: $("reference-start").value, duration: $("reference-duration").value });
  setReferencePreview(file);
  $("reference-error").hidden = true;
});
$("reference-text").addEventListener("input", updateReferenceCount);
$("reference-source-player").addEventListener("error", () => {
  if ($("reference-source-player").getAttribute("src")) {
    $("reference-file-info").textContent = "El navegador no puede reproducir este formato. Puedes preparar el fragmento si conoces sus tiempos y transcripción.";
  }
});
window.addEventListener("beforeunload", () => {
  if (state.referenceSourceUrl) URL.revokeObjectURL(state.referenceSourceUrl);
});

$("cancel-button").addEventListener("click", async () => {
  if (!state.jobId) return;
  $("cancel-button").disabled = true;
  try {
    const updated = await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST", body: "{}" });
    state.jobs = state.jobs.map((j) => j.id === updated.id ? updated : j);
    renderJobs(); renderPreview();
    toast(updated.state === "cancelled" ? "Trabajo retirado de la cola." : "Cancelación solicitada. El paso de inferencia actual debe terminar primero.");
  } catch (error) { toast(error.message); $("cancel-button").disabled = false; }
});

$("approve-button").addEventListener("click", async () => {
  const candidate = state.candidateId;
  if (!candidate || !state.listened.has(candidate)) return;
  $("approve-button").disabled = true;
  try {
    await api(`/api/profiles/${state.profileId}/approve`, { method: "POST", body: JSON.stringify({ candidate_id: candidate }) });
    await refreshProfiles();
    state.jobId = null;
    renderPreview();
    toast("Voz aprobada. Ya puedes narrar con esta identidad.");
  } catch (error) { toast(error.message); updateApproval(); }
});

$("audio-player").addEventListener("play", () => $("preview-visual").classList.add("playing"));
$("audio-player").addEventListener("pause", () => $("preview-visual").classList.remove("playing"));
$("audio-player").addEventListener("ended", () => { $("preview-visual").classList.remove("playing"); if (state.candidateId) state.listened.add(state.candidateId); updateApproval(); });
$("audio-player").addEventListener("error", () => { if ($("audio-player").getAttribute("src")) toast("No se ha podido abrir el WAV. Comprueba el audio en la actividad del estudio."); });
$("script-text").addEventListener("input", updateCount);
$("load-example").addEventListener("click", () => { $("script-text").value = examples[state.profileId] || ""; updateCount(); saveForm(); });
for (const button of document.querySelectorAll(".tab")) {
  button.addEventListener("click", () => switchView(state.profileId, button.dataset.mode));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const mode = state.mode === "design" ? "narrate" : "design";
    switchView(state.profileId, mode); $(`tab-${mode}`).focus();
  });
}

async function start() {
  await refreshHealth();
  try { await refreshProfiles(); restoreForm(); await poll(); }
  catch (error) { $("profile-list").replaceChildren(element("div", "loading-text", error.message)); }
  setInterval(poll, 1500);
}
start();
