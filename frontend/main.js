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
const endInterviewBtn = document.getElementById('endInterviewBtn');
const summaryModal = document.getElementById('summaryModal');
const summaryLoading = document.getElementById('summaryLoading');
const summaryContent = document.getElementById('summaryContent');
const summaryError = document.getElementById('summaryError');
const resumeFileInput = document.getElementById('resumeFile');
const resumeDropZone = document.getElementById('resumeDropZone');
const resumeDropText = document.getElementById('resumeDropText');
const resumeStatus = document.getElementById('resumeStatus');

let appState = 'idle';
let ws = null;
let audioCtx = null;
let micStream = null;
let analyserNode = null;
let workletNode = null;
let playbackQueueTime = 0;
let resumeText = '';

function setState(newState) {
  appState = newState;

  appContainer.classList.remove('state-idle', 'state-listening', 'state-processing', 'state-speaking');
  appContainer.classList.add(`state-${newState}`);

  const intensityMap = { idle: 0.0, listening: 0.6, processing: 0.35, speaking: 1.0 };
  if (typeof window.setShaderIntensity === 'function') {
    window.setShaderIntensity(intensityMap[newState] ?? 0.0);
  }

  if (typeof window.setWaveActive === 'function') {
    window.setWaveActive(newState === 'speaking' || newState === 'listening');
  }

  if (newState === 'idle') {
    statusArea.style.display = 'none';
    endInterviewBtn.style.display = 'none';
  } else {
    statusArea.style.display = 'flex';
    endInterviewBtn.style.display = 'block';
  }

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

function connectWebSocket(profile) {
  ws = new WebSocket('ws://localhost:8001/ws/interview');
  ws.binaryType = 'arraybuffer';

  ws.onopen = async () => {
    connectionDot.classList.add('connected');
    connectionLabel.textContent = 'Connected';

    ws.send(JSON.stringify(profile));

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
  } else if (msg.type === 'summary') {
    populateSummary(msg.data);
  } else if (msg.type === 'summary_error') {
    showSummaryError();
  }
}

function handleAudioMessage(arrayBuffer) {
  setState('speaking');
  playPCM(arrayBuffer);
}

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

  await audioCtx.audioWorklet.addModule('pcm-worklet.js');
  workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');

  workletNode.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN && appState === 'listening') {
      ws.send(e.data);
    }
  };

  source.connect(workletNode);
}

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

beginBtn.addEventListener('click', () => {
  const name = document.getElementById('candidateName').value.trim();
  const role = document.getElementById('targetRole').value.trim();

  if (!name || !role) {
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
    resume_text: resumeText,
  };

  beginBtn.disabled = true;
  beginBtn.textContent = 'Connecting…';

  connectWebSocket(profile);
});

['candidateName', 'targetRole'].forEach(id => {
  document.getElementById(id).addEventListener('input', () => {
    document.getElementById(id).classList.remove('input-error');
  });
});

function setResumeState(state, message = '') {
  resumeDropZone.dataset.uploadState = state;

  switch (state) {
    case 'loading':
      resumeDropText.textContent = 'Uploading…';
      resumeStatus.textContent = '';
      resumeStatus.className = 'resume-status';
      break;
    case 'success':
      resumeDropText.textContent = message || 'Resume uploaded';
      resumeStatus.textContent = '✓ Ready';
      resumeStatus.className = 'resume-status resume-status--success';
      break;
    case 'error':
      resumeDropText.textContent = 'Click to retry';
      resumeStatus.textContent = message || 'Upload failed';
      resumeStatus.className = 'resume-status resume-status--error';
      break;
    default:
      resumeDropText.textContent = 'Click to upload PDF';
      resumeStatus.textContent = '';
      resumeStatus.className = 'resume-status';
  }
}

resumeFileInput.addEventListener('change', async () => {
  const file = resumeFileInput.files[0];
  if (!file) return;

  resumeText = '';
  setResumeState('loading');

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/upload-resume', { method: 'POST', body: formData });
    const body = await res.json();

    if (!res.ok) {
      setResumeState('error', body.detail || `Server error ${res.status}`);
      return;
    }

    resumeText = body.resume_text || '';
    const shortName = file.name.length > 24 ? file.name.slice(0, 21) + '…' : file.name;
    setResumeState('success', shortName);
  } catch (err) {
    console.error('Resume upload failed:', err);
    setResumeState('error', 'Network error — try again');
  }
});

orbWrapper.addEventListener('click', () => {
  if (appState !== 'idle') stopSession();
});

orbWrapper.addEventListener('keydown', (e) => {
  if ((e.key === 'Enter' || e.key === ' ') && appState !== 'idle') {
    e.preventDefault();
    stopSession();
  }
});

endInterviewBtn.addEventListener('click', () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  summaryModal.style.display = 'flex';
  summaryLoading.style.display = 'flex';
  summaryContent.style.display = 'none';
  summaryError.style.display = 'none';

  if (micStream) micStream.getTracks().forEach(t => t.stop());

  ws.send(JSON.stringify({ type: 'end_interview' }));

  endInterviewBtn.style.display = 'none';

  const summaryTimeout = setTimeout(() => {
    if (summaryLoading.style.display !== 'none') {
      showSummaryError();
    }
  }, 35000);

  const clearOnLoad = new MutationObserver(() => {
    if (summaryLoading.style.display === 'none') {
      clearTimeout(summaryTimeout);
      clearOnLoad.disconnect();
    }
  });
  clearOnLoad.observe(summaryLoading, { attributes: true, attributeFilter: ['style'] });
});

function populateSummary(data) {
  document.getElementById('summaryOverall').textContent = data.overall_impression || '—';

  const strengthsList = document.getElementById('summaryStrengths');
  strengthsList.innerHTML = '';
  (data.strengths || []).forEach(s => {
    const li = document.createElement('li');
    li.textContent = s;
    strengthsList.appendChild(li);
  });

  const areasList = document.getElementById('summaryAreas');
  areasList.innerHTML = '';
  (data.areas_to_improve || []).forEach(a => {
    const li = document.createElement('li');
    li.textContent = a;
    areasList.appendChild(li);
  });

  document.getElementById('summaryComms').textContent = data.communication_notes || '—';

  const stepsList = document.getElementById('summaryNextSteps');
  stepsList.innerHTML = '';
  (data.suggested_next_steps || []).forEach(s => {
    const li = document.createElement('li');
    li.textContent = s;
    stepsList.appendChild(li);
  });

  summaryLoading.style.display = 'none';
  summaryContent.style.display = 'block';

  stopSession();
}

function showSummaryError() {
  summaryLoading.style.display = 'none';
  summaryError.style.display = 'block';
  stopSession();
}

document.getElementById('closeSummaryBtn').addEventListener('click', () => {
  summaryModal.style.display = 'none';
});

summaryModal.addEventListener('click', (e) => {
  if (e.target === summaryModal) summaryModal.style.display = 'none';
});

setState('idle');