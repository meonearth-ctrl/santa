// app.js — Santa browser UI. Plain JavaScript, no framework.
// Records the microphone with an AudioWorklet, encodes 16 kHz mono WAV in the
// browser, uploads it to Santa, polls the job, and inserts the transcript at
// the cursor of an editable text area. The same page serves the Mac window
// (http://127.0.0.1:8765) and the paired iPhone (https://<mac>.local:8766);
// /api/config says which, and Mac-only controls are hidden on the phone.
'use strict';

const $ = (id) => document.getElementById(id);
const TARGET_RATE = 16000;
const MAX_RECORD_SECONDS = 30 * 60;
const VOICE_SAMPLE_SECONDS = 20;

const ui = {
  state: 'ready',          // ready | recording | processing | complete | error
  config: null,
  settings: null,
  job: null,
  caret: null,             // saved selection in the transcript
  client: 'mac',           // 'mac' | 'phone'
};

// ── API helpers ───────────────────────────────────────────────────────────
async function api(method, path, body, extraHeaders) {
  const headers = Object.assign({ 'X-Santa': '1' }, extraHeaders || {});
  let payload = body;
  if (body && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, { method, headers, body: payload, cache: 'no-store', credentials: 'same-origin' });
  } catch (e) {
    throw { code: 'offline', message: ui.client === 'phone'
      ? 'Cannot reach your Mac. Check that it is awake, on the same Wi-Fi, and that Santa is running with iPhone access on.'
      : 'Cannot reach the Santa server. Is it still running? Start it again with the Santa launcher.' };
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON */ }
  if (!resp.ok) throw (data && data.error) || { code: 'http_' + resp.status, message: 'Request failed (' + resp.status + ').' };
  return data;
}

// ── State & messages ──────────────────────────────────────────────────────
const STATE_LABEL = { ready: 'Ready', recording: 'Recording', processing: 'Processing…', complete: 'Complete', error: 'Error' };

function setState(state, detail) {
  ui.state = state;
  const badge = $('stateBadge');
  badge.textContent = detail || STATE_LABEL[state];
  badge.className = 'badge state-' + state;
  const recording = state === 'recording' && rec.target === 'dictation';
  const busy = state === 'processing';
  $('recordBtn').setAttribute('aria-pressed', recording ? 'true' : 'false');
  $('recordLabel').textContent = recording ? 'Stop recording' : 'Start recording';
  $('recordBtn').disabled = busy || (state === 'recording' && rec.target !== 'dictation');
  $('micSel').disabled = state === 'recording';
  $('cancelBtn').hidden = !(busy || recording);
  $('progressWrap').hidden = !busy;
  if (state !== 'recording') $('meterFill').style.width = '0%';
}

function showAlert(message, kind) {
  const el = $('alert');
  if (!message) { el.hidden = true; return; }
  el.textContent = message;
  el.className = 'alert ' + (kind || 'error');
  el.hidden = false;
}

// ── Config, settings, model status ────────────────────────────────────────
async function loadConfig() {
  ui.config = await api('GET', '/api/config');
  ui.settings = ui.config.settings;
  ui.client = ui.config.client;
  document.body.classList.toggle('is-phone', ui.client === 'phone');
  const langSel = $('languageSel');
  langSel.innerHTML = '';
  for (const l of ui.config.languages) langSel.add(new Option(l.label, l.key));
  langSel.value = ui.settings.language;
  const outSel = $('outputSel');
  outSel.innerHTML = '';
  for (const o of ui.config.output_languages) outSel.add(new Option(o.key === 'same' ? 'As spoken' : o.label + ' (translate)', o.key));
  outSel.value = ui.settings.output_language || 'same';
  $('hinglishOutSel').value = ui.settings.hinglish_output;
  $('diarizeChk').checked = !!ui.settings.diarize_files;
  $('speakersSel').value = String(ui.settings.num_speakers || 0);
  syncLanguageUI();
  if (ui.client === 'phone') {
    $('sourceInput').placeholder = '…or paste a link (YouTube, Loom, Drive, direct .mp4)';
    $('promptInput').value = ui.settings.user_prompt || '';
    $('diarizeDefaultChk').checked = !!ui.settings.diarize_files;
    return;
  }
  const modelSel = $('modelSel');
  modelSel.innerHTML = '';
  for (const m of ui.config.models) {
    const label = `${m.label} — ${formatMB(m.download_mb)}, ~${m.ram_gb} GB RAM${m.cached ? ' ✓ downloaded' : ''}${m.fits_memory ? '' : ' (too big)'}`;
    const opt = new Option(label, m.key);
    opt.disabled = !m.fits_memory;
    modelSel.add(opt);
  }
  modelSel.value = ui.settings.model;
  updateModelNote();
  const watchLang = $('watchLangSel');
  watchLang.innerHTML = '';
  for (const l of ui.config.languages) watchLang.add(new Option(l.label, l.key));
  const stratSel = $('strategySel');
  stratSel.innerHTML = '';
  for (const s of ui.config.hinglish_strategies) stratSel.add(new Option(s.label, s.key));
  fillSettingsForm();
  renderModelStatus(ui.config.model_status);
  $('historyBtn').hidden = !ui.settings.history_enabled;
}

function formatMB(mb) { return mb >= 1000 ? (mb / 1000).toFixed(1) + ' GB' : mb + ' MB'; }

function updateModelNote() {
  const m = ui.config.models.find((x) => x.key === $('modelSel').value);
  $('modelNote').textContent = m ? `${m.note} Download ≈ ${formatMB(m.download_mb)} (once), memory ≈ ${m.ram_gb} GB. Your Mac has ${ui.config.ram_gb} GB.` : '';
}

function fillSettingsForm() {
  const s = ui.settings;
  $('promptInput').value = s.user_prompt || '';
  $('diarizeDefaultChk').checked = !!s.diarize_files;
  if (ui.client === 'phone') return;
  $('modelSel').value = s.model;
  $('engineSel').value = s.engine;
  $('engineField').hidden = !ui.config.accelerator_available;
  $('strategySel').value = s.hinglish_strategy;
  $('cleanupChk').checked = s.cleanup;
  $('vadChk').checked = s.vad_filter;
  $('beamSel').value = String(s.beam_size);
  $('historyChk').checked = s.history_enabled;
  $('maxUploadInput').value = s.max_upload_mb;
  $('maxDurInput').value = s.max_duration_min;
  $('exportChk').checked = s.export_enabled;
  $('exportDirInput').value = s.export_dir;
  $('watchChk').checked = s.watch_enabled;
  $('watchDirInput').value = s.watch_dir;
  $('watchLangSel').value = s.watch_language;
  $('phoneChk').checked = !!s.phone_enabled;
  $('phoneSet').hidden = !ui.config.phone_available;
  updateModelNote();
  refreshPhoneStatus();
}

async function saveSettings(changes) {
  const res = await api('PUT', '/api/settings', changes);
  ui.settings = Object.assign(ui.settings, res.settings);
  $('historyBtn').hidden = !ui.settings.history_enabled;
  if (!ui.settings.history_enabled) $('historyPanel').hidden = true;
  return ui.settings;
}

function renderModelStatus(st, accel) {
  const pill = $('modelPill');
  if (!st) return;
  let text = st.message || st.state;
  if (st.state === 'downloading' && st.progress != null) text += ` (${Math.round(st.progress * 100)}%)`;
  if (accel && accel.state !== 'ready') text += ' · ' + accel.message;
  else if (accel && accel.state === 'ready') text += ' · GPU fast path on';
  pill.textContent = text;
  pill.className = 'pill ' + (st.state === 'ready' ? 'ready' : st.state === 'error' ? 'error' : 'busy');
  pill.title = text;
}

let statusTimer = null;
async function pollStatus() {
  try {
    const st = await api('GET', '/api/status');
    renderModelStatus(st.model, st.accelerator);
    if (ui.client === 'phone') $('modelPill').textContent = st.model.state === 'ready' ? 'Connected to your Mac' : $('modelPill').textContent;
    if (st.watch && st.watch.state !== 'off') $('modelPill').textContent += ' · ' + st.watch.message;
    for (const extra of [st.speakers, st.translation]) {
      if (extra && extra.state === 'downloading') $('modelPill').textContent += ' · ' + extra.message;
    }
    if (st.phone && st.phone.running) $('modelPill').textContent += ' · iPhone access on';
    const busy = ['checking', 'downloading', 'loading'].includes(st.model.state) ||
      (st.accelerator && ['loading', 'downloading'].includes(st.accelerator.state));
    statusTimer = setTimeout(pollStatus, busy ? 700 : 3000);
  } catch (e) {
    $('modelPill').textContent = ui.client === 'phone' ? 'Mac not reachable' : 'Santa server not reachable';
    $('modelPill').className = 'pill error';
    statusTimer = setTimeout(pollStatus, 3000);
  }
}

function syncLanguageUI() {
  const lang = $('languageSel').value;
  $('hinglishOutField').hidden = lang !== 'hinglish';
  $('autoHint').hidden = lang !== 'auto';
  const tag = { en: 'en', hi: 'hi', ar: 'ar', bn: 'bn', hinglish: 'hi' }[lang];
  if (tag) $('transcript').setAttribute('lang', tag); else $('transcript').removeAttribute('lang');
}

// ── Microphones ───────────────────────────────────────────────────────────
async function listMics() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  const devices = await navigator.mediaDevices.enumerateDevices();
  const sel = $('micSel');
  const current = sel.value || (ui.settings && ui.settings.microphone_id) || '';
  sel.innerHTML = '';
  sel.add(new Option('System default microphone', ''));
  let n = 0;
  for (const d of devices) {
    if (d.kind !== 'audioinput' || d.deviceId === 'default' || d.deviceId === '') continue;
    n += 1;
    sel.add(new Option(d.label || `Microphone ${n} (names appear after you allow access)`, d.deviceId));
  }
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

// ── Recording ─────────────────────────────────────────────────────────────
// target: 'dictation' (transcribe) or 'voice' (a sample to recognise someone later)
const rec = { ctx: null, stream: null, node: null, source: null, chunks: [], length: 0, started: 0, tick: null, target: 'dictation', wake: null, voiceName: '' };

async function startRecording(target) {
  showAlert('');
  rec.target = target || 'dictation';
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showAlert(ui.client === 'phone'
      ? 'The microphone needs a secure connection. Finish step 1 of "Set up iPhone" (install and trust the certificate), then reopen Santa.'
      : 'This browser page cannot use the microphone here. Open Santa at http://127.0.0.1:8765 in Safari, Chrome or Edge.');
    return;
  }
  // Create the audio context inside the tap itself: iOS only lets audio start from a user gesture.
  try { rec.ctx = new AudioContext(); } catch (e) { showAlert('This browser cannot record audio.'); return; }
  const deviceId = ui.client === 'mac' ? $('micSel').value : '';
  const constraints = { audio: { channelCount: 1, echoCancellation: false, noiseSuppression: true, autoGainControl: true } };
  if (deviceId) constraints.audio.deviceId = { exact: deviceId };
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia(constraints);
  } catch (e) {
    cleanupRecorder(); setState('error'); showAlert(micErrorMessage(e));
    return;
  }
  try {
    if (rec.ctx.state === 'suspended') await rec.ctx.resume();
    await rec.ctx.audioWorklet.addModule('/static/recorder-worklet.js');
    rec.source = rec.ctx.createMediaStreamSource(rec.stream);
    rec.node = new AudioWorkletNode(rec.ctx, 'santa-recorder');
    rec.chunks = []; rec.length = 0;
    rec.node.port.onmessage = (ev) => {
      const block = ev.data;
      rec.chunks.push(block); rec.length += block.length;
      let peak = 0;
      for (let i = 0; i < block.length; i++) { const v = Math.abs(block[i]); if (v > peak) peak = v; }
      $('meterFill').style.width = Math.min(100, Math.round(Math.sqrt(peak) * 100)) + '%';
    };
    rec.source.connect(rec.node);
    // Worklet must be pulled by the graph; route through a muted gain so nothing is played back.
    const mute = rec.ctx.createGain(); mute.gain.value = 0;
    rec.node.connect(mute).connect(rec.ctx.destination);
  } catch (e) {
    cleanupRecorder();
    setState('error');
    showAlert('Could not start audio capture in this browser: ' + (e.message || e));
    return;
  }
  const track = rec.stream.getAudioTracks()[0];
  track.onended = () => { if (ui.state === 'recording') { stopRecording(true); showAlert('The microphone was disconnected. What was recorded so far is being transcribed.', 'warn'); } };
  rec.started = performance.now();
  setState('recording', rec.target === 'voice' ? 'Recording voice sample' : undefined);
  keepScreenOn(true);
  const limit = rec.target === 'voice' ? VOICE_SAMPLE_SECONDS : MAX_RECORD_SECONDS;
  rec.tick = setInterval(() => {
    const secs = (performance.now() - rec.started) / 1000;
    $('timer').textContent = formatTime(secs);
    if (rec.target === 'voice') $('voiceRecBtn').textContent = `Recording… ${Math.max(0, Math.ceil(limit - secs))} s (click to finish)`;
    if (secs >= limit) stopRecording(true);
  }, 250);
  if (ui.client === 'mac') listMics(); // labels become available after permission is granted
}

// A locked iPhone screen stops the microphone; keep it awake while recording.
async function keepScreenOn(on) {
  try {
    if (on && navigator.wakeLock && !rec.wake) rec.wake = await navigator.wakeLock.request('screen');
    else if (!on && rec.wake) { await rec.wake.release(); rec.wake = null; }
  } catch (e) { rec.wake = null; }
}

function micErrorMessage(e) {
  const name = e && e.name;
  if (name === 'NotAllowedError' || name === 'SecurityError')
    return ui.client === 'phone'
      ? 'Microphone access was denied. On the iPhone: Settings › Apps › Safari › Microphone › Allow (or tap "aA" in the address bar › Website Settings).'
      : 'Microphone access was denied. Allow the microphone for this page in your browser (address-bar icon), and in macOS System Settings › Privacy & Security › Microphone for your browser.';
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError')
    return 'No microphone was found. Connect one, or check System Settings › Sound › Input.';
  if (name === 'OverconstrainedError')
    return 'The selected microphone is not available any more. Pick another one from the list.';
  if (name === 'NotReadableError')
    return 'The microphone is busy or blocked by another app (e.g. a call). Close that app and try again.';
  return 'Could not open the microphone: ' + (e && (e.message || e.name) || e);
}

function cleanupRecorder() {
  clearInterval(rec.tick);
  try { rec.source && rec.source.disconnect(); } catch (e) {}
  try { rec.node && rec.node.disconnect(); } catch (e) {}
  if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop());
  const ctx = rec.ctx;
  rec.ctx = rec.stream = rec.node = rec.source = null;
  keepScreenOn(false);
  return ctx;
}

async function stopRecording(send) {
  const sampleRate = rec.ctx ? rec.ctx.sampleRate : 48000;
  const chunks = rec.chunks, length = rec.length;
  const ctx = cleanupRecorder();
  if (ctx) ctx.close();
  rec.chunks = []; rec.length = 0;
  const target = rec.target;
  rec.target = 'dictation';
  if (target === 'voice') $('voiceRecBtn').textContent = 'Record 20 s sample';
  if (!send) { setState('ready', 'Cancelled'); return; }
  if (length < sampleRate * 0.3) {
    setState('error'); showAlert('The recording was too short — nothing to transcribe.'); return;
  }
  const pcm = new Float32Array(length);
  let off = 0; for (const c of chunks) { pcm.set(c, off); off += c.length; }
  const wav = encodeWav(downsample(pcm, sampleRate, TARGET_RATE), TARGET_RATE);
  if (target === 'voice') { await saveVoiceSample(wav, length / sampleRate); return; }
  await submitAudio(wav, 'recording.wav', 'recording');
}

function downsample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    // Average the source samples that fall in this output slot (simple low-pass).
    const start = Math.floor(i * ratio), end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0; for (let j = start; j < end; j++) sum += input[j];
    out[i] = end > start ? sum / (end - start) : input[start] || 0;
  }
  return out;
}

function encodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE');
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, samples.length * 2, true);
  for (let i = 0, o = 44; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: 'audio/wav' });
}

function formatTime(secs) {
  const m = Math.floor(secs / 60), s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

// ── Dictation jobs (the big record button) ────────────────────────────────
function jobQuery(extra) {
  const q = new URLSearchParams(Object.assign({ language: $('languageSel').value, output_language: $('outputSel').value }, extra || {}));
  return q.toString();
}

async function submitAudio(blob, filename, source) {
  setState('processing', 'Processing…');
  $('progressFill').style.width = '0%';
  let job;
  try {
    job = await api('POST', '/api/transcribe?' + jobQuery({ filename, source }), blob, { 'Content-Type': blob.type || 'application/octet-stream' });
  } catch (e) {
    setState('error'); showAlert(e.message || 'Upload failed.'); return;
  }
  ui.job = job.id;
  pollJob(job.id);
}

const JOB_LABEL = {
  queued: 'Waiting in queue…', downloading: 'Downloading…', decoding: 'Reading audio…', waiting_model: 'Waiting for model…',
  transcribing: 'Transcribing…', speakers: 'Separating speakers…', translating: 'Translating…',
};

async function pollJob(id) {
  if (ui.job !== id) return;
  let job;
  try { job = await api('GET', '/api/jobs/' + id); }
  catch (e) { setState('error'); showAlert(e.message); ui.job = null; return; }
  if (job.state === 'done') { ui.job = null; onResult(job.result, job.id); return; }
  if (job.state === 'error') { ui.job = null; setState('error'); showAlert(job.error.message); return; }
  if (job.state === 'cancelled') { ui.job = null; setState('ready', 'Cancelled'); return; }
  setState('processing', JOB_LABEL[job.state] || 'Processing…');
  const indeterminate = job.progress == null;
  $('progressWrap').classList.toggle('indeterminate', indeterminate);
  if (!indeterminate) $('progressFill').style.width = Math.round(job.progress * 100) + '%';
  setTimeout(() => pollJob(id), 350);
}

async function cancelCurrent() {
  if (ui.state === 'recording') { stopRecording(false); return; }
  if (ui.job) {
    const id = ui.job; ui.job = null;
    try { await api('POST', `/api/jobs/${id}/cancel`); } catch (e) {}
    setState('ready', 'Cancelled');
  }
}

let lastResult = null, lastJobId = null;
function onResult(r, jobId) {
  lastResult = r; lastJobId = jobId;
  if (r.empty || !r.text) {
    setState('complete', 'No speech found');
    showAlert('No speech was recognised in this audio.', 'info');
  } else {
    insertText(r.text);
    setState('complete');
    const notes = [r.script_warning, r.translation_error, r.speaker_error].filter(Boolean);
    showAlert(notes.join(' '), 'warn');
  }
  renderResultDetails(r, jobId);
  if (ui.settings.history_enabled && !$('historyPanel').hidden) loadHistory();
}

function renderResultDetails(r, jobId) {
  $('lastResult').hidden = false;
  const meta = $('resultMeta');
  meta.innerHTML = '';
  const langLabel = { ...Object.fromEntries(ui.config.languages.map((l) => [l.key, l.label])) }[r.language_mode] || r.language_mode;
  const rows = [
    ['Language setting', langLabel],
    ['Detected', r.detected_language_name ? r.detected_language_name + (r.language_probability != null ? ` (${Math.round(r.language_probability * 100)}%)` : '') : '—'],
    ['Audio length', r.audio_seconds + ' s'],
    ['Transcription time', r.transcribe_seconds + ' s'],
    ['Model', r.model],
    ['Engine', r.engine || '—'],
  ];
  if (r.source_name && r.source_name !== 'recording.wav') rows.unshift(['File', r.source_name]);
  if (ui.client === 'mac') rows.push(['Saved files', r.exported ? r.exported.join('\n') : (r.export_error || '—')]);
  for (const [k, v] of rows) { const dt = document.createElement('dt'); dt.textContent = k; const dd = document.createElement('dd'); dd.textContent = v; meta.append(dt, dd); }
  const tr = r.translation;
  $('translationNote').hidden = !tr;
  $('translationNote').textContent = tr ? `Translated from ${tr.from_name} to ${tr.to_name} on your Mac (offline). Machine translation can be wrong — the original is below.` : '';
  $('originalBlock').hidden = !tr;
  $('originalText').textContent = tr ? r.original_text : '';
  $('rawText').textContent = r.raw_text || '(empty)';
  $('romanBlock').hidden = !r.romanized_text;
  $('romanText').textContent = r.romanized_text || '';
  renderSpeakers(r, jobId);
}

// Speakers found in a file, with "name this voice" so Santa recognises them next time.
function renderSpeakers(r, jobId) {
  const list = $('speakersList');
  list.innerHTML = '';
  $('speakersBlock').hidden = !(r.speakers && r.speakers.length);
  for (const sp of r.speakers || []) {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = `${sp.name}${sp.known ? ' ✓ recognised' : ''} · ${formatTime(sp.seconds)} talking`;
    li.append(label);
    if (!sp.known) {
      const input = document.createElement('input'); input.type = 'text'; input.maxLength = 40; input.placeholder = 'Who is this?';
      const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'secondary small'; btn.textContent = 'Remember voice';
      btn.onclick = async () => {
        const name = input.value.trim();
        if (!name) { input.focus(); return; }
        try {
          await api('POST', `/api/jobs/${jobId}/name-speaker`, { speaker_id: sp.id, name });
          const ta = $('transcript');
          ta.value = ta.value.split(sp.id + ':').join(name + ':');
          sp.name = name; sp.known = true; renderSpeakers(r, jobId);
          showAlert(`Saved. Santa will label ${name} automatically in future files.`, 'info');
        } catch (e) { showAlert(e.message); }
      };
      li.append(input, btn);
    }
    list.append(li);
  }
}

// ── Files, paths and links (a queue of jobs) ──────────────────────────────
const fileJobs = new Map();   // job id -> { li, name }

function fileOptions() {
  return { diarize: $('diarizeChk').checked, num_speakers: Number($('speakersSel').value) };
}

async function addFiles(files) {
  const maxBytes = ui.settings.max_upload_mb * 1e6;
  for (const f of files) {
    const li = fileRow(f.name, 'Uploading…');
    if (f.size > maxBytes) { markRow(li, 'error', `Too big: ${(f.size / 1e6).toFixed(0)} MB (limit ${ui.settings.max_upload_mb} MB, Settings)`); continue; }
    try {
      const o = fileOptions();
      const job = await api('POST', '/api/transcribe?' + jobQuery({ filename: f.name, source: 'upload', diarize: o.diarize ? '1' : '0', speakers: String(o.num_speakers) }),
        f, { 'Content-Type': f.type || 'application/octet-stream' });
      trackFileJob(job, li);
    } catch (e) { markRow(li, 'error', e.message); }
  }
}

async function addSource(text) {
  const li = fileRow(text.length > 80 ? text.slice(0, 77) + '…' : text, 'Starting…');
  try {
    const job = await api('POST', '/api/transcribe-source', Object.assign({ source: text, language: $('languageSel').value, output_language: $('outputSel').value }, fileOptions()));
    trackFileJob(job, li);
  } catch (e) { markRow(li, 'error', e.message); }
}

function fileRow(name, status) {
  const li = document.createElement('li');
  li.innerHTML = '<div class="fj-name"></div><div class="fj-status"></div><div class="fj-bar"><div></div></div><div class="fj-actions"></div>';
  li.querySelector('.fj-name').textContent = name;
  li.querySelector('.fj-status').textContent = status;
  $('fileJobs').prepend(li);
  return li;
}

function markRow(li, kind, text) {
  li.className = kind;
  li.querySelector('.fj-status').textContent = text;
  li.querySelector('.fj-bar').hidden = kind !== 'busy';
}

function trackFileJob(job, li) {
  fileJobs.set(job.id, { li });
  const cancel = document.createElement('button'); cancel.className = 'secondary small'; cancel.textContent = 'Cancel';
  cancel.onclick = () => api('POST', `/api/jobs/${job.id}/cancel`).catch(() => {});
  li.querySelector('.fj-actions').replaceChildren(cancel);
  pollFileJob(job.id);
}

async function pollFileJob(id) {
  const entry = fileJobs.get(id);
  if (!entry) return;
  let job;
  try { job = await api('GET', '/api/jobs/' + id); }
  catch (e) { markRow(entry.li, 'error', e.message); fileJobs.delete(id); return; }
  const li = entry.li;
  if (job.source_name) li.querySelector('.fj-name').textContent = job.source_name;
  if (job.state === 'done') {
    fileJobs.delete(id);
    const r = job.result;
    const bits = [`${formatTime(r.audio_seconds)} of audio`, `${r.transcribe_seconds} s`];
    if (r.speakers) bits.push(`${r.speakers.length} speaker${r.speakers.length === 1 ? '' : 's'}`);
    if (r.translation) bits.push(`translated to ${r.translation.to_name}`);
    if (r.exported) bits.push('saved .json + .srt');
    markRow(li, 'done', (r.empty ? 'No speech found' : 'Done') + ' · ' + bits.join(' · '));
    const actions = li.querySelector('.fj-actions');
    actions.replaceChildren();
    if (!r.empty) {
      const show = document.createElement('button'); show.className = 'secondary small'; show.textContent = 'Details';
      show.onclick = () => { renderResultDetails(r, id); $('lastResult').open = true; $('lastResult').scrollIntoView({ behavior: 'smooth' }); };
      actions.append(show);
      const ta = $('transcript');
      ui.caret = { start: ta.value.length, end: ta.value.length };
      insertText((ta.value ? '\n\n' : '') + `── ${job.source_name || 'file'} ──\n` + r.text);
      lastResult = r; lastJobId = id;
      renderResultDetails(r, id);
    }
    const notes = [r.translation_error, r.speaker_error, r.export_error].filter(Boolean);
    if (notes.length) showAlert(notes.join(' '), 'warn');
    return;
  }
  if (job.state === 'error') { fileJobs.delete(id); markRow(li, 'error', job.error.message); li.querySelector('.fj-actions').replaceChildren(); return; }
  if (job.state === 'cancelled') { fileJobs.delete(id); markRow(li, 'error', 'Cancelled'); li.querySelector('.fj-actions').replaceChildren(); return; }
  markRow(li, 'busy', JOB_LABEL[job.state] || 'Working…');
  const bar = li.querySelector('.fj-bar');
  bar.classList.toggle('indeterminate', job.progress == null);
  bar.firstElementChild.style.width = job.progress == null ? '30%' : Math.round(job.progress * 100) + '%';
  setTimeout(() => pollFileJob(id), 800);
}

// ── Transcript editing ────────────────────────────────────────────────────
function insertText(text) {
  const ta = $('transcript');
  const sel = ui.caret || { start: ta.value.length, end: ta.value.length };
  const before = ta.value.slice(0, sel.start), after = ta.value.slice(sel.end);
  const sepBefore = before && !/\s$/.test(before) && !/^\s/.test(text) ? ' ' : '';
  const sepAfter = after && !/^\s/.test(after) ? ' ' : '';
  ta.value = before + sepBefore + text + sepAfter + after;
  const pos = (before + sepBefore + text).length;
  ta.setSelectionRange(pos, pos);
  ui.caret = { start: pos, end: pos };
  if (ui.client === 'mac') ta.focus();       // on the phone this would pop the keyboard up
}

function rememberCaret() {
  const ta = $('transcript');
  ui.caret = { start: ta.selectionStart, end: ta.selectionEnd };
}

async function copyTranscript() {
  const text = $('transcript').value;
  if (!text) { showAlert('Nothing to copy yet.', 'info'); return; }
  let ok = false;
  try {
    // Some browsers wait silently when the window isn't focused; don't hang the UI.
    await Promise.race([navigator.clipboard.writeText(text),
      new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 1500))]);
    ok = true;
  } catch (e) {
    const ta = $('transcript'); ta.focus(); ta.select();
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
  }
  showAlert(ok ? 'Transcript copied to the clipboard.'
               : 'Could not copy automatically. The text is selected — press ⌘C to copy it.', ok ? 'info' : 'warn');
}

async function shareTranscript() {
  const text = $('transcript').value;
  if (!text) { showAlert('Nothing to share yet.', 'info'); return; }
  try { await navigator.share({ text }); } catch (e) { /* the user closed the share sheet */ }
}

function exportTranscript() {
  const text = $('transcript').value;
  if (!text) { showAlert('Nothing to export yet.', 'info'); return; }
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}-${String(d.getHours()).padStart(2, '0')}${String(d.getMinutes()).padStart(2, '0')}`;
  a.href = URL.createObjectURL(blob);
  a.download = `santa-${stamp}.txt`;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

// ── Read aloud (only when asked; uses the device's own voices) ────────────
// Only voices that run on this device (localService) are used, so the text
// never goes to an online speech service.
function textLanguage(text) {
  const counts = { ar: 0, hi: 0, bn: 0, en: 0 };
  for (const ch of text) {
    const c = ch.codePointAt(0);
    if (c >= 0x0600 && c <= 0x06ff) counts.ar++;
    else if (c >= 0x0900 && c <= 0x097f) counts.hi++;
    else if (c >= 0x0980 && c <= 0x09ff) counts.bn++;
    else if (/[A-Za-z]/.test(ch)) counts.en++;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
}

function pickVoice(lang) {
  const voices = speechSynthesis.getVoices().filter((v) => v.localService && v.lang.toLowerCase().startsWith(lang));
  const rank = (v) => (/premium|enhanced|siri/i.test(v.name) ? 0 : 1) + (v.default ? 0 : 0.5);
  return voices.sort((a, b) => rank(a) - rank(b))[0] || null;
}

function speakChunks(text) {
  // Long utterances stall in Safari; speak sentence-sized pieces in a queue.
  const parts = text.replace(/\s+/g, ' ').match(/[^.!?।؟\n]{1,220}[.!?।؟]?/g) || [text];
  return parts.map((p) => p.trim()).filter(Boolean);
}

function toggleSpeak() {
  if (!('speechSynthesis' in window)) { showAlert('Reading aloud is not supported in this browser.', 'warn'); return; }
  if (speechSynthesis.speaking || speechSynthesis.pending) { speechSynthesis.cancel(); $('speakBtn').textContent = 'Read aloud'; return; }
  const ta = $('transcript');
  const selected = ta.value.slice(ta.selectionStart, ta.selectionEnd);
  const text = (selected.trim() ? selected : ta.value).trim();
  if (!text) { showAlert('Nothing to read yet.', 'info'); return; }
  const paragraphs = text.split(/\n{2,}/);
  const queue = [];
  const missing = new Set();
  for (const para of paragraphs) {
    const lang = textLanguage(para);
    const voice = pickVoice(lang);
    if (!voice) { missing.add(lang); continue; }
    for (const piece of speakChunks(para)) {
      const u = new SpeechSynthesisUtterance(piece);
      u.voice = voice; u.lang = voice.lang; u.rate = 1.0;
      queue.push(u);
    }
  }
  if (missing.size) {
    const names = { ar: 'Arabic', hi: 'Hindi', bn: 'Bengali', en: 'English' };
    const list = [...missing].map((l) => names[l]).join(', ');
    showAlert(`No ${list} voice is installed on this device. ${ui.client === 'phone'
      ? 'Add one in Settings › Accessibility › Spoken Content › Voices.'
      : 'Add one in System Settings › Accessibility › Spoken Content › System voice › Manage Voices.'}`, 'warn');
  }
  if (!queue.length) return;
  queue[queue.length - 1].onend = () => { $('speakBtn').textContent = 'Read aloud'; };
  queue[queue.length - 1].onerror = () => { $('speakBtn').textContent = 'Read aloud'; };
  $('speakBtn').textContent = 'Stop reading';
  for (const u of queue) speechSynthesis.speak(u);
}

// ── Known voices ──────────────────────────────────────────────────────────
async function loadVoices() {
  let data;
  try { data = await api('GET', '/api/voices'); } catch (e) { return; }
  const list = $('voicesList');
  list.innerHTML = '';
  if (!data.voices.length) { const li = document.createElement('li'); li.className = 'hint'; li.textContent = 'No voices saved.'; list.append(li); }
  for (const v of data.voices) {
    const li = document.createElement('li');
    const span = document.createElement('span'); span.textContent = `${v.name} · ${Math.round(v.seconds)} s of audio`;
    const del = document.createElement('button'); del.type = 'button'; del.className = 'secondary small danger-text'; del.textContent = 'Delete';
    del.onclick = async () => { await api('DELETE', '/api/voices/' + encodeURIComponent(v.name)); loadVoices(); };
    li.append(span, del); list.append(li);
  }
}

async function saveVoiceSample(wav, seconds) {
  const name = rec.voiceName;
  $('voiceMsg').textContent = 'Saving voice…';
  try {
    await api('POST', '/api/voices?' + new URLSearchParams({ name, filename: 'voice.wav' }), wav, { 'Content-Type': 'audio/wav' });
    $('voiceMsg').textContent = `Saved ${name}'s voice (${Math.round(seconds)} s). Santa will label them automatically when speakers are separated.`;
    $('voiceName').value = '';
    setState('ready');
    loadVoices();
  } catch (e) { $('voiceMsg').textContent = e.message; setState('ready'); }
}

// ── iPhone access (Mac side) ──────────────────────────────────────────────
async function refreshPhoneStatus() {
  if (ui.client !== 'mac' || !ui.config.phone_available) return null;
  let st;
  try { st = await api('GET', '/api/phone'); } catch (e) { $('phoneStatus').textContent = e.message; return null; }
  $('phoneStatus').textContent = st.running
    ? `On — ${st.url} · ${st.devices.length} paired device${st.devices.length === 1 ? '' : 's'}`
    : (st.error || 'Off. Your phone cannot reach Santa.');
  $('phoneSetupBtn').disabled = !st.running;
  $('caFingerprint').textContent = st.fingerprint || '—';
  $('phoneUrl').textContent = st.url || '';
  if (st.setup_url) {
    $('setupQr').innerHTML = st.setup_qr_svg || ''; $('setupQr').hidden = !st.setup_qr_svg;
    $('setupUrl').textContent = `Or type in Safari: ${st.setup_url} (open for 10 minutes)`;
  }
  const list = $('deviceList');
  list.innerHTML = '';
  if (!st.devices.length) { const li = document.createElement('li'); li.className = 'hint'; li.textContent = 'No paired devices yet.'; list.append(li); }
  for (const d of st.devices) {
    const li = document.createElement('li');
    const span = document.createElement('span');
    span.textContent = `${d.name} (${d.kind === 'shortcut' ? 'Shortcut key' : 'browser'}) · paired ${new Date(d.created * 1000 || d.created).toLocaleDateString()}`;
    const del = document.createElement('button'); del.type = 'button'; del.className = 'secondary small danger-text'; del.textContent = 'Remove';
    del.onclick = async () => { await api('DELETE', '/api/phone/devices/' + d.id); refreshPhoneStatus(); };
    li.append(span, del); list.append(li);
  }
  return st;
}

let pairingTimer = null;
async function showPairing() {
  try {
    const p = await api('POST', '/api/phone/pairing');
    $('pairingBox').hidden = false;
    $('pairingQr').innerHTML = p.qr_svg || '';
    $('pairingCode').textContent = p.code.replace(/(\d{3})(\d{3})/, '$1 $2');
    clearInterval(pairingTimer);
    pairingTimer = setInterval(() => {
      const left = Math.round(p.expires - Date.now() / 1000);
      $('pairingExpiry').textContent = left > 0 ? `Valid for ${formatTime(left)} · works once` : 'Expired — press Show pairing code again.';
      if (left <= 0) clearInterval(pairingTimer);
      if (left % 5 === 0) refreshPhoneStatus();
    }, 1000);
  } catch (e) { alertInDialog(e.message); }
}

function alertInDialog(message) { $('pairingExpiry').textContent = message; $('pairingBox').hidden = false; }

// ── Pairing (phone side) ──────────────────────────────────────────────────
async function pairWith(code) {
  $('pairMsg').textContent = 'Pairing…';
  const standalone = window.navigator.standalone || matchMedia('(display-mode: standalone)').matches;
  const device = (/iPhone/.test(navigator.userAgent) ? 'iPhone' : /iPad/.test(navigator.userAgent) ? 'iPad' : 'Phone') + (standalone ? ' (Home Screen)' : ' (Safari)');
  try {
    await api('POST', '/api/pair', { code, device_name: device });
    history.replaceState(null, '', '/');
    $('pairMsg').textContent = 'Paired.';
    await startApp();
  } catch (e) { $('pairMsg').textContent = e.message; }
}

function showPairScreen() {
  $('mainPanel').hidden = true;
  $('pairPanel').hidden = false;
  $('settingsBtn').hidden = true; $('quitBtn').hidden = true;
  $('modelPill').textContent = 'Not paired';
  const m = location.hash.match(/pair=([A-Za-z0-9_-]+)/);
  if (m) pairWith(m[1]);
}

// ── History ───────────────────────────────────────────────────────────────
async function loadHistory() {
  const data = await api('GET', '/api/history');
  const list = $('historyList');
  list.innerHTML = '';
  if (!data.entries.length) { const li = document.createElement('li'); li.textContent = 'No saved transcripts.'; list.append(li); return; }
  for (const e of data.entries) {
    const li = document.createElement('li');
    const t = document.createElement('div'); t.className = 'h-text'; t.dir = 'auto'; t.textContent = e.text;
    const m = document.createElement('div'); m.className = 'h-meta';
    m.append(new Date(e.created * 1000).toLocaleString(), ` · ${e.language_mode} · ${e.model} · ${e.duration_s}s `);
    const ins = document.createElement('button'); ins.className = 'secondary small'; ins.textContent = 'Insert';
    ins.onclick = () => insertText(e.text);
    const del = document.createElement('button'); del.className = 'secondary small danger-text'; del.textContent = 'Delete';
    del.onclick = async () => { await api('DELETE', '/api/history/' + e.id); loadHistory(); };
    m.append(ins, del);
    li.append(t, m); list.append(li);
  }
}

// ── Wiring ────────────────────────────────────────────────────────────────
function openSettings(focusVocab) {
  fillSettingsForm(); loadVoices();
  $('settingsDlg').showModal();
  if (focusVocab) { $('promptInput').focus(); $('vocabSet').scrollIntoView(); }
}

function wire() {
  $('recordBtn').onclick = () => (ui.state === 'recording' ? stopRecording(true) : startRecording('dictation'));
  $('cancelBtn').onclick = cancelCurrent;
  $('uploadBtn').onclick = () => $('fileInput').click();
  $('fileInput').onchange = (ev) => { const files = [...ev.target.files]; ev.target.value = ''; if (files.length) addFiles(files); };
  const zone = $('dropZone');
  ['dragenter', 'dragover'].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove('over'); }));
  zone.addEventListener('drop', (e) => {
    const files = [...(e.dataTransfer.files || [])];
    if (files.length) { addFiles(files); return; }
    const text = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
    if (text) addSource(text.split('\n')[0].trim());
  });
  // Files dropped anywhere on the page go to the queue instead of opening in the browser.
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => { if (!zone.contains(e.target)) { e.preventDefault(); const f = [...(e.dataTransfer.files || [])]; if (f.length) addFiles(f); } });
  $('sourceForm').onsubmit = (e) => { e.preventDefault(); const t = $('sourceInput').value.trim(); if (t) { addSource(t); $('sourceInput').value = ''; } };
  $('languageSel').onchange = async () => { syncLanguageUI(); await saveSettings({ language: $('languageSel').value }); };
  $('outputSel').onchange = () => saveSettings({ output_language: $('outputSel').value });
  $('hinglishOutSel').onchange = () => saveSettings({ hinglish_output: $('hinglishOutSel').value });
  $('micSel').onchange = () => saveSettings({ microphone_id: $('micSel').value, microphone_label: $('micSel').selectedOptions[0].text });
  $('diarizeChk').onchange = () => saveSettings({ diarize_files: $('diarizeChk').checked });
  $('speakersSel').onchange = () => saveSettings({ num_speakers: Number($('speakersSel').value) });
  const ta = $('transcript');
  ['keyup', 'mouseup', 'blur', 'input', 'touchend'].forEach((ev) => ta.addEventListener(ev, rememberCaret));
  $('copyBtn').onclick = copyTranscript;
  $('shareBtn').hidden = !navigator.share;
  $('shareBtn').onclick = shareTranscript;
  $('speakBtn').onclick = toggleSpeak;
  $('exportBtn').onclick = exportTranscript;
  $('clearBtn').onclick = () => {
    if ($('transcript').value && !confirmClear()) return;
    $('transcript').value = ''; ui.caret = null; showAlert('');
  };
  $('insertRawBtn').onclick = () => lastResult && insertText(lastResult.raw_text);
  $('insertOriginalBtn').onclick = () => lastResult && lastResult.original_text && insertText(lastResult.original_text);
  $('insertRomanBtn').onclick = () => lastResult && lastResult.romanized_text && insertText(lastResult.romanized_text);
  $('settingsBtn').onclick = () => openSettings(false);
  $('modelSel').onchange = updateModelNote;
  $('loadModelBtn').onclick = async () => {
    try { await api('POST', '/api/models/load', { model: $('modelSel').value }); ui.settings.model = $('modelSel').value; clearTimeout(statusTimer); pollStatus(); }
    catch (e) { showAlert(e.message); }
  };
  $('voiceRecBtn').onclick = () => {
    if (ui.state === 'recording' && rec.target === 'voice') { stopRecording(true); return; }
    const name = $('voiceName').value.trim();
    if (!name) { $('voiceMsg').textContent = 'Type the person\'s name first.'; $('voiceName').focus(); return; }
    if (ui.state === 'recording') return;
    rec.voiceName = name;
    startRecording('voice');
  };
  $('saveSettingsBtn').onclick = async () => {
    try {
      const common = { user_prompt: $('promptInput').value, diarize_files: $('diarizeDefaultChk').checked };
      if (ui.client === 'phone') {
        await saveSettings(common);
      } else {
        await saveSettings(Object.assign(common, {
          model: $('modelSel').value, engine: $('engineSel').value, hinglish_strategy: $('strategySel').value,
          cleanup: $('cleanupChk').checked, vad_filter: $('vadChk').checked,
          beam_size: Number($('beamSel').value),
          history_enabled: $('historyChk').checked,
          max_upload_mb: Number($('maxUploadInput').value), max_duration_min: Number($('maxDurInput').value),
          export_enabled: $('exportChk').checked, export_dir: $('exportDirInput').value.trim(),
          watch_enabled: $('watchChk').checked, watch_dir: $('watchDirInput').value.trim(),
          watch_language: $('watchLangSel').value,
        }));
      }
      $('diarizeChk').checked = !!ui.settings.diarize_files;
      $('settingsDlg').close();
      clearTimeout(statusTimer); pollStatus();
    } catch (e) { showAlert(e.message); }
  };
  $('phoneChk').onchange = async () => {
    $('phoneStatus').textContent = $('phoneChk').checked ? 'Starting…' : 'Stopping…';
    try { await saveSettings({ phone_enabled: $('phoneChk').checked }); } catch (e) { showAlert(e.message); }
    setTimeout(refreshPhoneStatus, 600);
  };
  $('phoneSetupBtn').onclick = async () => { await refreshPhoneStatus(); $('phoneDlg').showModal(); };
  $('setupQrBtn').onclick = async () => {
    try { await api('POST', '/api/phone/setup'); await refreshPhoneStatus(); } catch (e) { $('setupUrl').textContent = e.message; }
  };
  $('airdropBtn').onclick = async () => { try { const r = await api('POST', '/api/phone/airdrop'); $('setupUrl').textContent = 'Saved to ' + r.path; } catch (e) { $('setupUrl').textContent = e.message; } };
  $('pairingBtn').onclick = showPairing;
  $('shortcutBtn').onclick = async () => {
    try {
      const r = await api('POST', '/api/phone/shortcut-token', { name: 'iPhone Shortcut' });
      $('shortcutBox').hidden = false;
      $('shortcutToken').textContent = r.token;
      $('shortcutUrl').textContent = r.url + 'api/transcribe?filename=shortcut.m4a&source=recording&wait=120&format=text';
      refreshPhoneStatus();
    } catch (e) { alertInDialog(e.message); }
  };
  $('historyBtn').onclick = async () => { const p = $('historyPanel'); p.hidden = !p.hidden; if (!p.hidden) loadHistory(); };
  $('clearHistoryBtn').onclick = async () => { await api('DELETE', '/api/history'); loadHistory(); };
  $('quitBtn').onclick = async () => {
    try { await api('POST', '/api/shutdown'); } catch (e) {}
    document.body.innerHTML = '<p style="padding:40px;font:18px system-ui">Santa has stopped. You can close this tab. Start it again with the Santa launcher.</p>';
  };
  $('pairBtn').onclick = () => { const c = $('pairCode').value.replace(/\D/g, ''); if (c.length === 6) pairWith(c); else $('pairMsg').textContent = 'Type the 6-digit code shown on your Mac.'; };
  $('pairCode').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('pairBtn').click(); });
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) navigator.mediaDevices.addEventListener('devicechange', listMics);

  document.addEventListener('keydown', (e) => {
    if (e.altKey && !e.metaKey && !e.ctrlKey) {
      const k = e.code;
      if (k === 'KeyS') { e.preventDefault(); if (ui.state !== 'processing') $('recordBtn').click(); }
      else if (k === 'KeyC') { e.preventDefault(); copyTranscript(); }
      else if (k === 'KeyE') { e.preventDefault(); exportTranscript(); }
      else if (k === 'KeyR') { e.preventDefault(); toggleSpeak(); }
      else if (k === 'KeyU') { e.preventDefault(); $('uploadBtn').click(); }
    } else if (e.key === 'Escape' && (ui.state === 'recording' || ui.state === 'processing') && !$('settingsDlg').open) {
      e.preventDefault(); cancelCurrent();
    }
  });
  window.addEventListener('beforeunload', (e) => { if (ui.state === 'recording' || ui.state === 'processing') { e.preventDefault(); e.returnValue = ''; } });
}

// A native confirm() dialog would block automation and feels heavy; a second
// click within 3 s confirms instead.
let clearArmed = 0;
function confirmClear() {
  const now = Date.now();
  if (now - clearArmed < 3000) { clearArmed = 0; $('clearBtn').textContent = 'Clear'; return true; }
  clearArmed = now; $('clearBtn').textContent = 'Click again to clear';
  setTimeout(() => { $('clearBtn').textContent = 'Clear'; }, 3000);
  return false;
}

async function startApp() {
  try {
    await loadConfig();
  } catch (e) {
    if (e.code === 'not_paired') { showPairScreen(); return; }
    showAlert(e.message || 'Could not load Santa settings.');
    return;
  }
  $('pairPanel').hidden = true; $('mainPanel').hidden = false; $('settingsBtn').hidden = false;
  if (ui.client === 'mac') {
    await listMics();
    if (location.hash === '#vocab') openSettings(true);
    if (location.hash === '#iphone') { openSettings(false); $('phoneSet').scrollIntoView(); }
  }
  clearTimeout(statusTimer);
  pollStatus();
}

(function init() {
  wire();
  setState('ready');
  startApp();
})();
