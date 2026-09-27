// The TV screen. It never decides on its own what comes next: it plays what
// the server says is playing and tells it when the song ends or fails.
(() => {
  const BAR_START_S = 10;   // bar visible for the first 10 s
  const BAR_END_S = 15;     // and the last 15 s
  const BAR_ON_CHANGE_MS = 8000;  // when the queue changes
  const NEXT_NOTICE_S = 3;  // "up next" notice

  const $ = (id) => document.getElementById(id);
  const video = $("video");
  const bar = $("bar");

  let state = null;
  let playingId = null;        // id of the request loaded in the <video>
  let queueSignature = "";
  let barForcedUntil = 0;
  let pendingReport = null;    // `ended`/`error` that could not be sent
  let qrPinned = null;         // null until the first state arrives

  const ws = connectWS(onMessage, (up) => {
    $("connection").classList.toggle("ok", up);
    if (up && pendingReport) sendReport(pendingReport);
  });

  // --- reports to the server ---

  function sendReport(msg) {
    pendingReport = ws.send(msg) ? null : msg;
  }

  video.addEventListener("ended", () => {
    if (playingId != null) sendReport({ type: "ended", id: playingId });
  });

  const MEDIA_ERRORS = {
    1: "MEDIA_ERR_ABORTED",
    2: "MEDIA_ERR_NETWORK",
    3: "MEDIA_ERR_DECODE: the browser could not decode the video",
    4: "MEDIA_ERR_SRC_NOT_SUPPORTED: unplayable format or missing file",
  };

  video.addEventListener("error", () => {
    if (playingId == null || !video.getAttribute("src")) return;
    const err = video.error;
    const text = err ? (MEDIA_ERRORS[err.code] || "error " + err.code) + (err.message ? " (" + err.message + ")" : "") : "unknown error";
    sendReport({ type: "error", id: playingId, error: text });
  });

  // --- server messages ---

  function onMessage(msg) {
    if (msg.type === "state") {
      state = msg;
      applyPinnedQr();
      apply();
    } else if (msg.type === "progress" && state) {
      for (const item of state.queue) {
        if (item.video_id === msg.video_id) item.progress = msg.pct;
      }
      if (!state.current) renderWaiting();
    }
  }

  function apply() {
    const cur = state.current;
    if (!cur) {
      stopVideo();
      showMode("waiting");
      renderWaiting();
      return;
    }

    if (cur.id !== playingId) {
      startVideo(cur);
    } else {
      // Same song (e.g. after reconnecting): leave the video alone, only the queue.
      const sig = signature();
      if (sig !== queueSignature) {
        queueSignature = sig;
        barForcedUntil = Date.now() + BAR_ON_CHANGE_MS;
      }
    }
    showMode("playing");
    renderBar();
    applyPause();
    updateOverlays();
  }

  function signature() {
    return state.up_next.map((i) => i.id + ":" + i.state).join(",") + "|" + state.queue.length;
  }

  // --- video ---

  function startVideo(item) {
    playingId = item.id;
    pendingReport = null;
    queueSignature = signature();
    barForcedUntil = 0;
    $("notice").classList.add("hidden");
    video.src = "/media/" + encodeURIComponent(item.video_id) + ".mp4";
    video.load();
    play();
  }

  function play() {
    const p = video.play();
    if (p && p.catch) {
      p.then(() => $("blocked").classList.add("hidden")).catch((err) => {
        if (err && err.name === "NotAllowedError") {
          // Autoplay blocked: Chromium is missing the flag. Playing muted is
          // worse than saying so, so show a notice and wait for a tap.
          $("blocked").classList.remove("hidden");
        }
      });
    }
  }

  $("blocked").addEventListener("click", () => play());

  function stopVideo() {
    if (playingId == null && !video.getAttribute("src")) return;
    playingId = null;
    video.pause();
    video.removeAttribute("src");
    video.load();
    bar.classList.remove("visible");
    $("notice").classList.add("hidden");
    $("paused").classList.add("hidden");
  }

  function applyPause() {
    $("paused").classList.toggle("hidden", !state.paused);
    if (state.paused && !video.paused) video.pause();
    else if (!state.paused && video.paused && !video.ended && video.getAttribute("src")) play();
  }

  // --- pinned QR: admin button on the phone, or the Q key here ---

  let flashTimer = null;
  function flash(text) {
    const el = $("flash");
    el.textContent = text;
    el.classList.remove("hidden");
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => el.classList.add("hidden"), 2500);
  }

  function applyPinnedQr() {
    const pinned = !!state.qr_pinned;
    if (qrPinned !== null && pinned !== qrPinned) {
      flash(pinned ? "QR code pinned (Q to hide it)" : "QR code unpinned (Q to pin it)");
    }
    qrPinned = pinned;
    $("playing").classList.toggle("qr-pinned", pinned);
    // While paused, the full-screen QR codes are already there.
    $("pinned-qr").classList.toggle("hidden", !pinned || state.paused);
  }

  document.addEventListener("keydown", (e) => {
    if (e.repeat || e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === "q" || e.key === "Q") ws.send({ type: "toggle-qr" });
  });

  // --- layers ---

  function showMode(mode) {
    $("waiting").classList.toggle("hidden", mode !== "waiting");
    $("playing").classList.toggle("hidden", mode !== "playing");
  }

  function renderWaiting() {
    const el = $("downloading");
    const items = state.queue.slice(0, 3);
    if (!items.length) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = `<div class="heading">Downloading…</div><ul>${items.map((i) => {
      const pct = i.state === "downloading" && i.progress != null ? ` <span class="pct">${Math.round(i.progress)}%</span>` : "";
      return `<li>${escapeHtml(i.title)} · ${escapeHtml(i.requested_by)}${pct}</li>`;
    }).join("")}</ul>`;
  }

  function renderBar() {
    const cur = state.current;
    $("bar-title").textContent = cur.title;
    $("bar-who").textContent = "requested by " + cur.requested_by;
    const next = state.up_next;
    $("bar-next").innerHTML = next.length
      ? next.map((i) => `<li>${escapeHtml(i.title)} <span class="who">· ${escapeHtml(i.requested_by)}</span>${
          i.state === "downloading" ? ' <span class="status">downloading</span>' : ""}</li>`).join("")
      : '<li class="who">Nothing in the queue. Add some songs!</li>';
  }

  function updateOverlays() {
    if (!state || !state.current) return;
    const t = video.currentTime || 0;
    const d = video.duration;
    const left = isFinite(d) && d > 0 ? d - t : Infinity;

    const showBar = t < BAR_START_S || left <= BAR_END_S || Date.now() < barForcedUntil;
    bar.classList.toggle("visible", showBar);

    const next = state.next_ready || state.up_next[0];
    const showNotice = next && left <= NEXT_NOTICE_S && !video.ended;
    if (showNotice) {
      $("notice-name").textContent = next.requested_by;
      $("notice-song").textContent = next.title;
    }
    $("notice").classList.toggle("hidden", !showNotice);
  }

  video.addEventListener("timeupdate", updateOverlays);
  // timeupdate doesn't fire while paused; this covers the end of the "forced" bar after a queue change.
  setInterval(updateOverlays, 500);
})();
