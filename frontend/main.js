/* ========================================================================
   AI Voice Interviewer — Main Application
   ========================================================================
   States: idle → listening → processing → speaking → listening …
   ======================================================================== */

// ─── DOM References ───
const appContainer = document.getElementById('appContainer');
const orbWrapper = document.getElementById('orbWrapper');
const audioBarsEl = document.getElementById('audioBars');
const statusText = document.getElementById('statusText');
const statusHint = document.getElementById('statusHint');
const transcriptPanel = document.getElementById('transcriptPanel');
const transcriptEmpty = document.getElementById('transcriptEmpty');
const connectionDot = document.getElementById('connectionDot');
const connectionLabel = document.getElementById('connectionLabel');

// ─── App State ───
let appState = 'idle'; // idle | listening | processing | speaking
let ws = null;
let audioCtx = null;
let micStream = null;
let analyserNode = null;
let workletNode = null;
let playbackQueueTime = 0;
let animFrameId = null;

// ─── Audio Bars Setup ───
const NUM_BARS = 48;
const BAR_RADIUS = 120; // distance from center to bar base

(function createAudioBars() {
  for (let i = 0; i < NUM_BARS; i++) {
    const bar = document.createElement('div');
    bar.className = 'audio-bar';
    const angle = (i / NUM_BARS) * 360;
    // Position bars in a circle
    const rad = (angle * Math.PI) / 180;
    const x = Math.cos(rad) * BAR_RADIUS;
    const y = Math.sin(rad) * BAR_RADIUS;
    bar.style.left = `calc(50% + ${x}px - 1.5px)`;
    bar.style.top = `calc(50% + ${y}px)`;
    bar.style.height = '4px';
    bar.style.transform = `rotate(${angle + 90}deg)`;
    audioBarsEl.appendChild(bar);
  }
})();

const audioBars = audioBarsEl.querySelectorAll('.audio-bar');

// ─── State Management ───
function setState(newState) {
  appState = newState;

  // Remove all state classes
  appContainer.classList.remove('state-idle', 'state-listening', 'state-processing', 'state-speaking');
  appContainer.classList.add(`state-${newState}`);

  // Update status text
  switch (newState) {
    case 'idle':
      statusText.textContent = 'Tap to begin';
      statusHint.textContent = 'Start your mock interview session';
      break;
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
function connectWebSocket() {
  ws = new WebSocket('ws://localhost:8001/ws/interview');
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    connectionDot.classList.add('connected');
    connectionLabel.textContent = 'Connected';
  };

  ws.onclose = () => {
    connectionDot.classList.remove('connected');
    connectionLabel.textContent = 'Disconnected';
    // If we were in an active state, go back to idle
    if (appState !== 'idle') {
      stopSession();
    }
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
    // User's transcribed speech
    addTranscriptEntry('You', msg.text, 'user');
    setState('processing');
  } else if (msg.type === 'reply_complete') {
    // AI finished replying
    addTranscriptEntry('Coach', msg.text, 'ai');
    // After audio finishes playing, go back to listening
    // We schedule this based on playback queue
    const delayMs = Math.max(0, (playbackQueueTime - audioCtx.currentTime) * 1000) + 300;
    setTimeout(() => {
      if (appState === 'speaking') {
        setState('listening');
      }
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

  // Create a new AudioContext for playback at 24kHz if needed
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
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true
    }
  });

  const source = audioCtx.createMediaStreamSource(micStream);

  // Analyser for visualizing audio bars
  analyserNode = audioCtx.createAnalyser();
  analyserNode.fftSize = 256;
  analyserNode.smoothingTimeConstant = 0.7;
  source.connect(analyserNode);

  // PCM worklet for sending audio to backend
  await audioCtx.audioWorklet.addModule('pcm-worklet.js');
  workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');

  workletNode.port.onmessage = (e) => {
    // Only send mic audio when actively listening — prevents feedback loop
    // where the AI's own TTS playback gets picked up by the mic
    if (ws && ws.readyState === WebSocket.OPEN && appState === 'listening') {
      ws.send(e.data);
    }
  };

  source.connect(workletNode);

  // Start visualizing
  startVisualization();
}

// ─── Audio Visualization Loop ───
function startVisualization() {
  const bufferLength = analyserNode.frequencyBinCount;
  const dataArray = new Uint8Array(bufferLength);

  function animate() {
    animFrameId = requestAnimationFrame(animate);

    if (appState !== 'listening') {
      // Reset bars when not listening
      audioBars.forEach(bar => {
        bar.style.height = '4px';
      });
      return;
    }

    analyserNode.getByteFrequencyData(dataArray);

    // Map frequency data to bars
    const step = Math.floor(bufferLength / NUM_BARS);
    for (let i = 0; i < NUM_BARS; i++) {
      const idx = Math.min(i * step, bufferLength - 1);
      const value = dataArray[idx];
      // Map 0-255 to 4-40px height
      const height = 4 + (value / 255) * 36;
      audioBars[i].style.height = `${height}px`;
    }
  }

  animate();
}

function stopVisualization() {
  if (animFrameId) {
    cancelAnimationFrame(animFrameId);
    animFrameId = null;
  }
  // Reset bars
  audioBars.forEach(bar => {
    bar.style.height = '4px';
  });
}

// ─── Session Control ───
async function startSession() {
  try {
    connectWebSocket();
    await startMicrophone();
    playbackQueueTime = 0;
    setState('listening');
  } catch (err) {
    console.error('Failed to start session:', err);
    statusText.textContent = 'Microphone access denied';
    statusHint.textContent = 'Please allow microphone access and try again';
  }
}

function stopSession() {
  setState('idle');
  stopVisualization();

  // Close WebSocket
  if (ws) {
    ws.close();
    ws = null;
  }

  // Stop microphone
  if (micStream) {
    micStream.getTracks().forEach(track => track.stop());
    micStream = null;
  }

  // Close audio context
  if (audioCtx) {
    audioCtx.close();
    audioCtx = null;
  }

  analyserNode = null;
  workletNode = null;
  playbackQueueTime = 0;
}

// ─── Transcript Helpers ───
function addTranscriptEntry(label, text, type) {
  // Remove the empty state message
  if (transcriptEmpty) {
    transcriptEmpty.style.display = 'none';
  }

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

// ─── Event Listeners ───
orbWrapper.addEventListener('click', () => {
  if (appState === 'idle') {
    startSession();
  } else {
    stopSession();
  }
});

// Keyboard accessibility
orbWrapper.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    orbWrapper.click();
  }
});

// Initialize
setState('idle');