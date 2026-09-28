// app.js — Santa browser UI. Plain JavaScript, no framework.
// Records the microphone with an AudioWorklet, encodes 16 kHz mono WAV in the
// browser, uploads it to the local Santa server, polls the job, and inserts the
// transcript at the cursor of an editable text area. The same page is meant to
// work later from a phone browser (Phase 2), so nothing here is desktop-only.
'use strict';

const $ = (id) => document.getElementById(id);
const TARGET_RATE = 16000;
const MAX_RECORD_SECONDS = 30 * 60;

const ui = {
  state: 'ready',          // ready | recording | processing | complete | error
  config: null,
  settings: null,
  job: null,
  caret: null,             // saved selection in the transcript
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
    resp = await fetch(path, { method, headers, body: payload, cache: 'no-store' });
  } catch (e) {
    throw { code: 'offline', message: 'Cannot reach the Santa server. Is it still running? Start it again with the Santa launcher.' };
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
  const recording = state === 'recording';
  const busy = state === 'processing';
  $('recordBtn').setAttribute('aria-pressed', recording ? 'true' : 'false');
  $('recordLabel').textContent = recording ? 'Stop recording' : 'Start recording';
  $('recordBtn').disabled = busy;
  $('uploadBtn').disabled = busy || recording;
  $('micSel').disabled = recording;
  $('cancelBtn').hidden = !(busy || recording);
  $('progressWrap').hidden = !busy;
  if (!recording) $('meterFill').style.width = '0%';
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
  const langSel = $('languageSel');
  langSel.innerHTML = '';
  for (const l of ui.config.languages) langSel.add(new Option(l.label, l.key));
  langSel.value = ui.settings.language;
  $('hinglishOutSel').value = ui.settings.hinglish_output;
  syncLanguageUI();

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
  $('modelSel').value = s.model;
  $('engineSel').value = s.engine;
  $('engineField').hidden = !ui.config.accelerator_available;
  $('strategySel').value = s.hinglish_strategy;
  $('cleanupChk').checked = s.cleanup;
  $('vadChk').checked = s.vad_filter;
  $('promptInput').value = s.user_prompt;
  $('beamSel').value = String(s.beam_size);
  $('historyChk').checked = s.history_enabled;
  $('maxUploadInput').value = s.max_upload_mb;
  $('maxDurInput').value = s.max_duration_min;
  updateModelNote();
}

async function saveSettings(changes) {
  const res = await api('PUT', '/api/settings', changes);
  ui.settings = res.settings;
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
    const busy = ['checking', 'downloading', 'loading'].includes(st.model.state) ||
      (st.accelerator && ['loading', 'downloading'].includes(st.accelerator.state));
    statusTimer = setTimeout(pollStatus, busy ? 700 : 3000);
  } catch (e) {
    $('modelPill').textContent = 'Santa server not reachable';
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
const rec = { ctx: null, stream: null, node: null, source: null, chunks: [], length: 0, started: 0, tick: null };

async function startRecording() {
  showAlert('');
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showAlert('This browser page cannot use the microphone here. Open Santa at http://127.0.0.1:8765 in Safari, Chrome or Edge.');
    return;
  }
  const deviceId = $('micSel').value;
  const constraints = { audio: { channelCount: 1, echoCancellation: false, noiseSuppression: true, autoGainControl: true } };
  if (deviceId) constraints.audio.deviceId = { exact: deviceId };
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia(constraints);
  } catch (e) {
    setState('error');
    showAlert(micErrorMessage(e));
    return;
  }
  try {
    rec.ctx = new AudioContext();
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
  setState('recording');
  rec.tick = setInterval(() => {
    const secs = (performance.now() - rec.started) / 1000;
    $('timer').textContent = formatTime(secs);
    if (secs >= MAX_RECORD_SECONDS) stopRecording(true);
  }, 250);
  listMics(); // labels become available after permission is granted
}

function micErrorMessage(e) {
  const name = e && e.name;
  if (name === 'NotAllowedError' || name === 'SecurityError')
    return 'Microphone access was denied. Allow the microphone for this page in your browser (address-bar icon), and in macOS System Settings › Privacy & Security › Microphone for your browser.';
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
  return ctx;
}

async function stopRecording(send) {
  const sampleRate = rec.ctx ? rec.ctx.sampleRate : 48000;
  const chunks = rec.chunks, length = rec.length;
  const ctx = cleanupRecorder();
  if (ctx) ctx.close();
  rec.chunks = []; rec.length = 0;
  if (!send) { setState('ready', 'Cancelled'); return; }
  if (length < sampleRate * 0.3) {
    setState('error'); showAlert('The recording was too short — nothing to transcribe.'); return;
  }
  const pcm = new Float32Array(length);
  let off = 0; for (const c of chunks) { pcm.set(c, off); off += c.length; }
  const wav = encodeWav(downsample(pcm, sampleRate, TARGET_RATE), TARGET_RATE);
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

// ── Transcription jobs ────────────────────────────────────────────────────
async function submitAudio(blob, filename, source) {
  setState('processing', source === 'upload' ? 'Uploading…' : 'Processing…');
  $('progressFill').style.width = '0%';
  const lang = $('languageSel').value;
  const q = new URLSearchParams({ language: lang, filename, source });
  let job;
  try {
    job = await api('POST', '/api/transcribe?' + q.toString(), blob, { 'Content-Type': blob.type || 'application/octet-stream' });
  } catch (e) {
    setState('error'); showAlert(e.message || 'Upload failed.'); return;
  }
  ui.job = job.id;
  pollJob(job.id);
}

const JOB_LABEL = { queued: 'Waiting in queue…', decoding: 'Reading audio…', waiting_model: 'Waiting for model…', transcribing: 'Transcribing…' };

async function pollJob(id) {
  if (ui.job !== id) return;
  let job;
  try { job = await api('GET', '/api/jobs/' + id); }
  catch (e) { setState('error'); showAlert(e.message); ui.job = null; return; }
  if (job.state === 'done') { ui.job = null; onResult(job.result); return; }
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

let lastResult = null;
function onResult(r) {
  lastResult = r;
  if (r.empty || !r.text) {
    setState('complete', 'No speech found');
    showAlert('No speech was recognised in this audio.', 'info');
  } else {
    insertText(r.text);
    setState('complete');
    showAlert(r.script_warning || '', 'warn');
  }
  renderResultDetails(r);
  if (ui.settings.history_enabled && !$('historyPanel').hidden) loadHistory();
}

function renderResultDetails(r) {
  $('lastResult').hidden = false;
  const meta = $('resultMeta');
  meta.innerHTML = '';
  const rows = [
    ['Language setting', $('languageSel').selectedOptions[0].text],
    ['Detected', r.detected_language_name ? r.detected_language_name + (r.language_probability != null ? ` (${Math.round(r.language_probability * 100)}%)` : '') : '—'],
    ['Audio length', r.audio_seconds + ' s'],
    ['Transcription time', r.transcribe_seconds + ' s'],
    ['Model', r.model],
    ['Engine', r.engine || '—'],
  ];
  for (const [k, v] of rows) { const dt = document.createElement('dt'); dt.textContent = k; const dd = document.createElement('dd'); dd.textContent = v; meta.append(dt, dd); }
  $('rawText').textContent = r.raw_text || '(empty)';
  $('romanBlock').hidden = !r.romanized_text;
  $('romanText').textContent = r.romanized_text || '';
}

// ── Transcript editing ────────────────────────────────────────────────────
function insertText(text) {
  const ta = $('transcript');
  const sel = ui.caret || { start: ta.value.length, end: ta.value.length };
  const before = ta.value.slice(0, sel.start), after = ta.value.slice(sel.end);
  const sepBefore = before && !/\s$/.test(before) ? (/[\n]$/.test(before) ? '' : ' ') : '';
  const sepAfter = after && !/^\s/.test(after) ? ' ' : '';
  ta.value = before + sepBefore + text + sepAfter + after;
  const pos = (before + sepBefore + text).length;
  ta.setSelectionRange(pos, pos);
  ui.caret = { start: pos, end: pos };
  ta.focus();
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
function wire() {
  $('recordBtn').onclick = () => (ui.state === 'recording' ? stopRecording(true) : startRecording());
  $('cancelBtn').onclick = cancelCurrent;
  $('uploadBtn').onclick = () => $('fileInput').click();
  $('fileInput').onchange = async (ev) => {
    const f = ev.target.files[0]; ev.target.value = '';
    if (!f) return;
    const maxBytes = ui.settings.max_upload_mb * 1e6;
    if (f.size > maxBytes) { setState('error'); showAlert(`That file is ${(f.size / 1e6).toFixed(0)} MB; the limit is ${ui.settings.max_upload_mb} MB (Settings).`); return; }
    showAlert('');
    $('timer').textContent = '—';
    await submitAudio(f, f.name, 'upload');
  };
  $('languageSel').onchange = async () => { syncLanguageUI(); await saveSettings({ language: $('languageSel').value }); };
  $('hinglishOutSel').onchange = () => saveSettings({ hinglish_output: $('hinglishOutSel').value });
  $('micSel').onchange = () => saveSettings({ microphone_id: $('micSel').value, microphone_label: $('micSel').selectedOptions[0].text });
  const ta = $('transcript');
  ['keyup', 'mouseup', 'blur', 'input'].forEach((ev) => ta.addEventListener(ev, rememberCaret));
  $('copyBtn').onclick = copyTranscript;
  $('exportBtn').onclick = exportTranscript;
  $('clearBtn').onclick = () => {
    if ($('transcript').value && !confirmClear()) return;
    $('transcript').value = ''; ui.caret = null; showAlert('');
  };
  $('insertRawBtn').onclick = () => lastResult && insertText(lastResult.raw_text);
  $('insertRomanBtn').onclick = () => lastResult && lastResult.romanized_text && insertText(lastResult.romanized_text);
  $('settingsBtn').onclick = () => { fillSettingsForm(); $('settingsDlg').showModal(); };
  $('modelSel').onchange = updateModelNote;
  $('loadModelBtn').onclick = async () => {
    try { await api('POST', '/api/models/load', { model: $('modelSel').value }); ui.settings.model = $('modelSel').value; clearTimeout(statusTimer); pollStatus(); }
    catch (e) { showAlert(e.message); }
  };
  $('saveSettingsBtn').onclick = async () => {
    try {
      await saveSettings({
        model: $('modelSel').value, engine: $('engineSel').value, hinglish_strategy: $('strategySel').value,
        cleanup: $('cleanupChk').checked, vad_filter: $('vadChk').checked,
        user_prompt: $('promptInput').value, beam_size: Number($('beamSel').value),
        history_enabled: $('historyChk').checked,
        max_upload_mb: Number($('maxUploadInput').value), max_duration_min: Number($('maxDurInput').value),
      });
      $('settingsDlg').close();
      clearTimeout(statusTimer); pollStatus();
    } catch (e) { showAlert(e.message); }
  };
  $('historyBtn').onclick = async () => { const p = $('historyPanel'); p.hidden = !p.hidden; if (!p.hidden) loadHistory(); };
  $('clearHistoryBtn').onclick = async () => { await api('DELETE', '/api/history'); loadHistory(); };
  $('quitBtn').onclick = async () => {
    try { await api('POST', '/api/shutdown'); } catch (e) {}
    document.body.innerHTML = '<p style="padding:40px;font:18px system-ui">Santa has stopped. You can close this tab. Start it again with the Santa launcher.</p>';
  };
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) navigator.mediaDevices.addEventListener('devicechange', listMics);

  document.addEventListener('keydown', (e) => {
    if (e.altKey && !e.metaKey && !e.ctrlKey) {
      const k = e.code;
      if (k === 'KeyS') { e.preventDefault(); if (ui.state !== 'processing') $('recordBtn').click(); }
      else if (k === 'KeyC') { e.preventDefault(); copyTranscript(); }
      else if (k === 'KeyE') { e.preventDefault(); exportTranscript(); }
      else if (k === 'KeyU') { e.preventDefault(); if (!$('uploadBtn').disabled) $('uploadBtn').click(); }
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

(async function init() {
  wire();
  setState('ready');
  try {
    await loadConfig();
    await listMics();
  } catch (e) {
    showAlert(e.message || 'Could not load Santa settings.');
  }
  pollStatus();
})();
