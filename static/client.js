/* static/client.js */

const socket = io();
const statusEl = document.getElementById("status-text");
const gameEl = document.getElementById("game-no");
const pointEl = document.getElementById("point-no");
const heardBox = document.getElementById("heard");
const heardText = document.getElementById("heard-text");

const btnStart = document.getElementById("btn-start");
const btnStop  = document.getElementById("btn-stop");
const btnUndo  = document.getElementById("btn-undo");
const btnPoint = document.getElementById("btn-point");
const btnGame  = document.getElementById("btn-game");
const btnExport= document.getElementById("btn-export");

const VISIBLE_EVENT_ROWS = 10; // show only the latest 10 rows

let audio = {
  ctx: null,
  source: null,
  processor: null,
  stream: null,
  running: false,
  inRate: 48000,
  outRate: 16000
};

/* -------- Socket wiring -------- */
socket.on("connect", () => {
  statusEl.textContent = "connected";
});

socket.on("disconnect", () => {
  statusEl.textContent = "disconnected";
});

socket.on("status", (msg) => {
  if (msg && msg.msg) statusEl.textContent = msg.msg;
});

socket.on("partial", ({text}) => {
  if (!text) return;
  heardText.textContent = text;
  heardBox.classList.add("is-partial");
});

socket.on("final", ({text}) => {
  heardText.textContent = text || "—";
  heardBox.classList.remove("is-partial");
});

socket.on("stats", (payload) => {
  if (!payload) return;
  gameEl.textContent = payload.game;
  pointEl.textContent = payload.point;
  renderPlayers(payload.players || []);
  renderEvents((payload.events || []));
});

/* -------- UI buttons -------- */
btnStart.addEventListener("click", startMic);
btnStop.addEventListener("click", stopMic);
btnUndo.addEventListener("click", () => socket.emit("command", {cmd:"undo"}));
btnPoint.addEventListener("click", () => socket.emit("command", {cmd:"new_point"}));
btnGame.addEventListener("click", () => socket.emit("command", {cmd:"new_game"}));
btnExport.addEventListener("click", () => socket.emit("command", {cmd:"export"}));

/* -------- Renderers -------- */
function renderPlayers(players){
  const tbody = document.querySelector("#players tbody");
  tbody.innerHTML = "";
  for (const p of players) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p.player)}</td>
      <td class="num">${p.throws}</td>
      <td class="num">${p.completions}</td>
      <td class="num">${p.turnovers}</td>
      <td class="num">${p.drops ?? 0}</td>
      <td class="num">${p.hucks}</td>
      <td class="num">${p.huck_completions}</td>
      <td class="num">${p.ds}</td>
      <td class="num">${p.assists}</td>
      <td class="num">${p.scores}</td>
    `;
    tbody.appendChild(tr);
  }
}

function eventDetails(ev){
  switch(ev.type){
    case "throw": return `${ev.thrower} → ${ev.receiver} (${ev.outcome})`;
    case "huck":  return `Huck ${ev.thrower} → ${ev.receiver} (${ev.outcome})`;
    case "assist":return `${ev.player} assist`;
    case "score": return `${ev.player} score`;
    case "d":     return `${ev.player} D`;
    case "drop":  return `${ev.player} drop`;
    case "new_point": return `— new point —`;
    case "new_game":  return `— new game —`;
    default: return ev.type || "";
  }
}

function renderEvents(events){
  // Events are already newest-first from server; render only the latest 10
  const view = events.slice(0, VISIBLE_EVENT_ROWS);
  const tbody = document.querySelector("#events tbody");
  tbody.innerHTML = "";
  for (const ev of view) {
    const tr = document.createElement("tr");
    const gp = `${ev.game}/${ev.point}`;
    tr.innerHTML = `
      <td>${escapeHtml(ev.t || "")}</td>
      <td>${escapeHtml(gp)}</td>
      <td>${escapeHtml(ev.type)}</td>
      <td>${escapeHtml(eventDetails(ev))}</td>
    `;
    tbody.appendChild(tr);
  }
}

/* -------- Mic streaming (16k PCM Int16 via Socket.IO) -------- */
async function startMic(){
  if (audio.running) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        noiseSuppression: true,
        echoCancellation: true,
        autoGainControl: true
      }
    });
    audio.stream = stream;

    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    audio.ctx = ctx;
    audio.inRate = ctx.sampleRate;
    audio.source = ctx.createMediaStreamSource(stream);

    const bufSize = 4096;
    const processor = ctx.createScriptProcessor(bufSize, 1, 1);
    audio.processor = processor;

    processor.onaudioprocess = (e) => {
      if (!audio.running) return;
      const input = e.inputBuffer.getChannelData(0);
      const down = downsampleBuffer(input, audio.inRate, audio.outRate);
      const pcm = floatTo16BitPCM(down);
      // Send as binary ArrayBuffer
      socket.emit("audio_chunk", pcm.buffer);
    };

    audio.source.connect(processor);
    processor.connect(ctx.destination);

    audio.running = true;
    btnStart.disabled = true;
    btnStop.disabled = false;
    statusEl.textContent = "mic streaming…";
  } catch (err) {
    console.error(err);
    statusEl.textContent = "mic error";
  }
}

function stopMic(){
  if (!audio.running) return;
  try {
    audio.running = false;
    if (audio.processor) {
      audio.processor.disconnect();
      audio.processor.onaudioprocess = null;
    }
    if (audio.source) audio.source.disconnect();
    if (audio.ctx) audio.ctx.close();
    if (audio.stream) {
      for (const t of audio.stream.getAudioTracks()) t.stop();
    }
  } catch (e) {
    // ignore
  }
  btnStart.disabled = false;
  btnStop.disabled = true;
  statusEl.textContent = "mic stopped";
}

/* -------- helpers -------- */
function downsampleBuffer(buffer, inRate, outRate){
  if (outRate === inRate) return buffer;
  const ratio = inRate / outRate;
  const newLen = Math.floor(buffer.length / ratio);
  const result = new Float32Array(newLen);
  let off = 0;
  for (let i = 0; i < newLen; i++){
    result[i] = buffer[Math.floor(off)];
    off += ratio;
  }
  return result;
}

function floatTo16BitPCM(input){
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++){
    let s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return out;
}

function escapeHtml(s){
  return (s ?? "").toString()
    .replace(/&/g,"&amp;")
    .replace(/</g,"&lt;")
    .replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}