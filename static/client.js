// static/client.js

(() => {
  // ---------- Socket.IO ----------
  const socket = io({
    transports: ["polling", "websocket"],
    upgrade: true,
    timeout: 20000,
  });
  window.socket = socket;

  const statusText = document.getElementById("status-text");
  const heardBox  = document.querySelector(".panel.heard");
  const heardText = document.getElementById("heard-text");
  const partialPre = document.getElementById("partial");

  socket.on("connect",    () => statusText && (statusText.textContent = "connected"));
  socket.on("disconnect", (r) => statusText && (statusText.textContent = `disconnected: ${r}`));
  socket.io.on("error",   (e) => console.error("socket.io error", e));
  socket.on("connect_error", (e) => console.error("connect_error", e));

  // ---------- UI elements ----------
  const btnStart = document.getElementById("btn-start");
  const btnStop  = document.getElementById("btn-stop");
  const btnUndo  = document.getElementById("btn-undo");
  const btnPoint = document.getElementById("btn-point");
  const btnGame  = document.getElementById("btn-game");
  const btnExport= document.getElementById("btn-export");

  const tblPlayersBody = document.querySelector("#players tbody");
  const tblEventsBody  = document.querySelector("#events tbody");
  const gameNoEl = document.getElementById("game-no");
  const pointNoEl = document.getElementById("point-no");

  // ---------- Mic streaming ----------
  let audioCtx = null;
  let mediaStream = null;
  let sourceNode = null;
  let processor = null;

  const outSampleRate = 16000;
  const bufSize = 4096; // or 8192 for fewer emits

  function floatTo16BitPCM(float32) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      let s = Math.max(-1, Math.min(1, float32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return out;
  }

  function downsampleLinear(buffer, inRate, outRate) {
    if (outRate === inRate) return buffer;
    const ratio = inRate / outRate;
    const newLen = Math.floor(buffer.length / ratio);
    const out = new Float32Array(newLen);
    for (let i = 0; i < newLen; i++) {
      const idx = i * ratio;
      const idx0 = Math.floor(idx);
      const idx1 = Math.min(idx0 + 1, buffer.length - 1);
      const frac = idx - idx0;
      out[i] = buffer[idx0] * (1 - frac) + buffer[idx1] * frac;
    }
    return out;
  }

  async function startMic() {
    try {
      if (audioCtx && audioCtx.state === "suspended") await audioCtx.resume();
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      sourceNode = audioCtx.createMediaStreamSource(mediaStream);
      processor = audioCtx.createScriptProcessor(bufSize, 1, 1);

      processor.onaudioprocess = (e) => {
        try {
          const inBuf = e.inputBuffer.getChannelData(0);
          const down = downsampleLinear(inBuf, audioCtx.sampleRate, outSampleRate);
          const pcm16 = floatTo16BitPCM(down);
          socket.emit("audio_chunk", pcm16.buffer);
        } catch (err) {
          console.error("audio process error:", err);
        }
      };

      sourceNode.connect(processor);
      processor.connect(audioCtx.destination); // required in some browsers

      btnStart.disabled = true;
      btnStop.disabled  = false;
      console.log("mic started @", audioCtx.sampleRate, "Hz →", outSampleRate, "Hz");
    } catch (err) {
      console.error("mic error:", err);
      statusText && (statusText.textContent = "mic error (see console)");
    }
  }

  function stopMic() {
    try {
      if (processor) { processor.disconnect(); processor.onaudioprocess = null; processor = null; }
      if (sourceNode) { sourceNode.disconnect(); sourceNode = null; }
      if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
      btnStart.disabled = false;
      btnStop.disabled  = true;
      console.log("mic stopped");
    } catch (err) {
      console.error("stop mic error:", err);
    }
  }

  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState === "visible" && audioCtx && audioCtx.state === "suspended") {
      try { await audioCtx.resume(); } catch {}
    }
  });

  // ---------- Server events ----------
  socket.on("status", ({ msg }) => {
    if (statusText) statusText.textContent = msg || "";
  });

  socket.on("stats", (data) => {
    // Game/Point
    if (typeof data.game === "number") gameNoEl.textContent = String(data.game);
    if (typeof data.point === "number") pointNoEl.textContent = String(data.point);

    // Players table
    const players = Array.isArray(data.players)
      ? data.players
      : Object.entries(data.players || {}).map(([name, st]) => ({ player: name, ...st }));

    tblPlayersBody.innerHTML = "";
    for (const p of players) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(p.player || "")}</td>
        <td class="num">${p.throws ?? 0}</td>
        <td class="num">${p.completions ?? 0}</td>
        <td class="num">${p.turnovers ?? 0}</td>
        <td class="num">${p.hucks ?? 0}</td>
        <td class="num">${p.huck_completions ?? 0}</td>
        <td class="num">${p.ds ?? 0}</td>
        <td class="num">${p.assists ?? 0}</td>
        <td class="num">${p.scores ?? 0}</td>
      `;
      tblPlayersBody.appendChild(tr);
    }

    // Events table (show newest at bottom)
    const events = data.events || [];
    tblEventsBody.innerHTML = "";
    for (const ev of events) {
      const details = renderEventDetails(ev);
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(ev.t || "")}</td>
        <td>${(ev.game ?? "")}/${(ev.point ?? "")}</td>
        <td>${escapeHtml(ev.type || "")}</td>
        <td>${escapeHtml(details)}</td>
      `;
      tblEventsBody.appendChild(tr);
    }
  });

  // Live transcript for the "Heard" box + debug <pre>
  socket.on("partial", ({ text }) => {
    if (heardText) heardText.textContent = (text || "").trim();
    heardBox?.classList.add("is-partial");
    if (partialPre) partialPre.textContent = text || "";
  });

  socket.on("final", ({ text }) => {
    if (heardText) heardText.textContent = (text || "").trim();
    heardBox?.classList.remove("is-partial");
    if (partialPre) partialPre.textContent = text || "";
  });

  // ---------- Buttons ----------
  btnStart?.addEventListener("click", startMic);
  btnStop?.addEventListener("click", stopMic);

  btnUndo?.addEventListener("click", () => {
    socket.emit("command", { cmd: "undo" });
  });

  btnPoint?.addEventListener("click", () => {
    socket.emit("command", { cmd: "new_point" });
  });

  btnGame?.addEventListener("click", () => {
    socket.emit("command", { cmd: "new_game" });
  });

  // Manual export now — shows links to the files in /data/game X/
  btnExport?.addEventListener("click", async () => {
    try {
      const res = await fetch("/export", { method: "POST" });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "export failed");

      const links = (data.written_rel || [])
        .map(rel => `<a href="/download/${encodeDownloadPath(rel)}" target="_blank">${escapeHtml(rel)}</a>`)
        .join(" · ");

      toast(`Saved to /data/${escapeHtml(data.game_dir_rel)}<br>${links}`);
    } catch (e) {
      console.error(e);
      toast("Save failed. Check server logs.");
    }
  });

  // Optional keyboard shortcut for Undo
  document.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() === "u" && !e.repeat) socket.emit("command", { cmd: "undo" });
  });

  // ---------- Helpers ----------
  function encodeDownloadPath(relPath) {
    // encode each segment so spaces ("game 2") work: game%202/point_1.csv
    return relPath.split("/").map(encodeURIComponent).join("/");
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function renderEventDetails(ev) {
    switch (ev.type) {
      case "throw":
        return `${ev.thrower || ""} → ${ev.receiver || ""} (${ev.outcome || ""})`;
      case "huck":
        return `${ev.thrower || ""} ⇢ ${ev.receiver || ""} (${ev.outcome || ""})`;
      case "assist":
        return `${ev.player || ""} (assist)`;
      case "score":
        return `${ev.player || ""} (score)`;
      case "d":
        return `${ev.player || ""} (D)`;
      case "new_point":
        return `Point ${ev.point}`;
      case "new_game":
        return `Game ${ev.game}`;
      default:
        return "";
    }
  }

  function toast(msg) {
    console.log(msg);
    try {
      const el = document.createElement("div");
      el.innerHTML = msg;
      el.style.cssText = "position:fixed;right:12px;bottom:12px;background:#333;color:#fff;padding:8px 12px;border-radius:8px;opacity:.95;z-index:9999;max-width:80vw";
      document.body.appendChild(el);
      setTimeout(() => el.remove(), 4000);
    } catch {}
  }

  // Initialize displayed game/point from server-rendered values (if present)
  if (window.INIT_GAME)  gameNoEl.textContent = window.INIT_GAME;
  if (window.INIT_POINT) pointNoEl.textContent = window.INIT_POINT;

  // Clean up on unload
  window.addEventListener("beforeunload", () => {
    try { stopMic(); } catch {}
  });
})();
