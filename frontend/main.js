/* ========================================================================
   AI Voice Interviewer — Main Application
   ========================================================================
   Flow: Setup form → Begin → WS connect → send profile → mic start → session
   States: idle → listening → processing → speaking → listening …
   ======================================================================== */

// ─── DOM References ───
const appContainer = document.getElementById('appContainer');
const orbWrapper = document.getElementById('orbWrapper');
const statusText = document.getElementById('statusText');
const statusHint = document.getElementById('statusHint');
const statusArea = document.getElementById('statusArea');
const transcriptPanel = document.getElementById('transcriptPanel');
const transcriptEmpty = document.getElementById('transcriptEmpty');
const connectionDot = document.getElementById('connectionDot');
const connectionLabel = document.getElementById('connectionLabel');
const setupForm = document.getElementById('setupForm');
const formPanel = document.getElementById('formPanel');
const beginBtn = document.getElementById('beginBtn');

// ─── App State ───
let appState = 'idle'; // idle | listening | processing | speaking
let ws = null;
let audioCtx = null;
let micStream = null;
let analyserNode = null;
let workletNode = null;
let playbackQueueTime = 0;

// ─── State Management ───
function setState(newState) {
  appState = newState;

  appContainer.classList.remove('state-idle', 'state-listening', 'state-processing', 'state-speaking');
  appContainer.classList.add(`state-${newState}`);

  // Drive WebGL shader intensity
  const intensityMap = { idle: 0.0, listening: 0.6, processing: 0.35, speaking: 1.0 };
  if (typeof window.setShaderIntensity === 'function') {
    window.setShaderIntensity(intensityMap[newState] ?? 0.0);
  }

  // Drive circular wave emission — active while speaking or listening
  if (typeof window.setWaveActive === 'function') {
    window.setWaveActive(newState === 'speaking' || newState === 'listening');
  }

  // Show/hide status area
  if (newState === 'idle') {
    statusArea.style.display = 'none';
  } else {
    statusArea.style.display = 'flex';
  }

  // Update status text
  switch (newState) {
    case 'listening':
      statusText.textContent = 'Listening…';
      statusHint.textContent = 'Speak naturally — I\'m hearing you';
      break;
    case 'processing':
      statusText.textContent = 'Thinking…';
      statusHint.textContent = 'Analyzing your response';
      break;
    case 'speaking':
      statusText.textContent = 'Speaking…';
      statusHint.textContent = 'Interview coach is responding';
      break;
  }
}

// ─── WebSocket Connection ───
function connectWebSocket(profile) {
  ws = new WebSocket('ws://localhost:8001/ws/interview');
  ws.binaryType = 'arraybuffer';

  ws.onopen = async () => {
    connectionDot.classList.add('connected');
    connectionLabel.textContent = 'Connected';

    // Server expects the setup JSON as the very first message
    ws.send(JSON.stringify(profile));

    // Hide left panel, show transcript, start mic
    formPanel.style.display = 'none';
    transcriptPanel.style.display = 'block';

    try {
      await startMicrophone();
      playbackQueueTime = 0;
      setState('listening');
    } catch (err) {
      console.error('Microphone error:', err);
      statusArea.style.display = 'flex';
      statusText.textContent = 'Microphone access denied';
      statusHint.textContent = 'Please allow microphone access and try again';
      resetForm();
    }
  };

  ws.onclose = () => {
    connectionDot.classList.remove('connected');
    connectionLabel.textContent = 'Disconnected';
    if (appState !== 'idle') stopSession();
  };

  ws.onerror = () => {
    connectionDot.classList.remove('connected');
    connectionLabel.textContent = 'Connection error';
  };

  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      handleJsonMessage(JSON.parse(event.data));
    } else {
      handleAudioMessage(event.data);
    }
  };
}

function handleJsonMessage(msg) {
  if (msg.type === 'transcript') {
    addTranscriptEntry('You', msg.text, 'user');
    setState('processing');
  } else if (msg.type === 'reply_complete') {
    addTranscriptEntry('Coach', msg.text, 'ai');
    const delayMs = Math.max(0, (playbackQueueTime - audioCtx.currentTime) * 1000) + 300;
    setTimeout(() => {
      if (appState === 'speaking') setState('listening');
    }, delayMs);
  }
}

function handleAudioMessage(arrayBuffer) {
  setState('speaking');
  playPCM(arrayBuffer);
}

// ─── Audio Playback ───
function playPCM(arrayBuffer) {
  if (!audioCtx) return;

  const int16 = new Int16Array(arrayBuffer);
  const float32 = Float32Array.from(int16, x => x / 32768);

  const buffer = audioCtx.createBuffer(1, float32.length, 24000);
  buffer.copyToChannel(float32, 0);

  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(audioCtx.destination);

  const startAt = Math.max(audioCtx.currentTime, playbackQueueTime);
  src.start(startAt);
  playbackQueueTime = startAt + buffer.duration;
}

// ─── Microphone & Audio Worklet ───
async function startMicrophone() {
  audioCtx = new AudioContext({ sampleRate: 16000 });
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
  });

  const source = audioCtx.createMediaStreamSource(micStream);

  analyserNode = audioCtx.createAnalyser();
  analyserNode.fftSize = 256;
  analyserNode.smoothingTimeConstant = 0.7;
  source.connect(analyserNode);

  // PCM worklet — sends raw audio to backend
  await audioCtx.audioWorklet.addModule('pcm-worklet.js');
  workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');

  workletNode.port.onmessage = (e) => {
    // Only forward mic audio while listening — prevents TTS feedback loop
    if (ws && ws.readyState === WebSocket.OPEN && appState === 'listening') {
      ws.send(e.data);
    }
  };

  source.connect(workletNode);
}

// ─── Session Stop ───
function stopSession() {
  setState('idle');

  if (ws) { ws.close(); ws = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }

  analyserNode = null;
  workletNode = null;
  playbackQueueTime = 0;

  resetForm();
}

function resetForm() {
  formPanel.style.display = 'flex';
  transcriptPanel.style.display = 'none';
  beginBtn.disabled = false;
  beginBtn.textContent = 'Begin Interview';
}

// ─── Transcript Helpers ───
function addTranscriptEntry(label, text, type) {
  if (transcriptEmpty) transcriptEmpty.style.display = 'none';

  const entry = document.createElement('div');
  entry.className = 'transcript-entry';
  entry.innerHTML = `
    <div class="transcript-label ${type}">${label}</div>
    <div class="transcript-content">${escapeHtml(text)}</div>
  `;
  transcriptPanel.appendChild(entry);
  transcriptPanel.scrollTop = transcriptPanel.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ─── Begin Button ───
beginBtn.addEventListener('click', () => {
  const name = document.getElementById('candidateName').value.trim();
  const role = document.getElementById('targetRole').value.trim();

  if (!name || !role) {
    // Shake the missing fields
    if (!name) document.getElementById('candidateName').classList.add('input-error');
    if (!role) document.getElementById('targetRole').classList.add('input-error');
    return;
  }

  const profile = {
    type: 'setup',
    name,
    role,
    company: document.getElementById('company').value.trim(),
    tech_stack: document.getElementById('techStack').value
      .split(',').map(s => s.trim()).filter(Boolean),
  };

  beginBtn.disabled = true;
  beginBtn.textContent = 'Connecting…';

  connectWebSocket(profile);
});

// Clear error state on input
['candidateName', 'targetRole'].forEach(id => {
  document.getElementById(id).addEventListener('input', () => {
    document.getElementById(id).classList.remove('input-error');
  });
});

// ─── Orb Click — stop only ───
orbWrapper.addEventListener('click', () => {
  if (appState !== 'idle') stopSession();
});

orbWrapper.addEventListener('keydown', (e) => {
  if ((e.key === 'Enter' || e.key === ' ') && appState !== 'idle') {
    e.preventDefault();
    stopSession();
  }
});

// ─── Initialize ───
setState('idle');