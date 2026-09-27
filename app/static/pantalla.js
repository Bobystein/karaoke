// La pantalla de la tele. Nunca decide sola que sigue: reproduce lo que el
// servidor dice que suena y le avisa cuando termina o falla.
(() => {
  const BAR_START_S = 10;   // barra visible los primeros 10 s
  const BAR_END_S = 15;     // y los ultimos 15 s
  const BAR_ON_CHANGE_MS = 8000;  // cuando cambia la cola
  const NEXT_NOTICE_S = 3;  // aviso de quien sigue

  const $ = (id) => document.getElementById(id);
  const video = $("video");
  const bar = $("barra");

  let state = null;
  let playingId = null;        // id de la peticion cargada en el <video>
  let queueSignature = "";
  let barForcedUntil = 0;
  let pendingReport = null;    // `ended`/`error` que no se pudo mandar

  const ws = connectWS(onMessage, (up) => {
    $("conexion").classList.toggle("ok", up);
    if (up && pendingReport) sendReport(pendingReport);
  });

  // --- avisos al servidor ---

  function sendReport(msg) {
    pendingReport = ws.send(msg) ? null : msg;
  }

  video.addEventListener("ended", () => {
    if (playingId != null) sendReport({ type: "ended", id: playingId });
  });

  const MEDIA_ERRORS = {
    1: "MEDIA_ERR_ABORTED",
    2: "MEDIA_ERR_NETWORK",
    3: "MEDIA_ERR_DECODE: el navegador no pudo decodificar el video",
    4: "MEDIA_ERR_SRC_NOT_SUPPORTED: formato no reproducible o archivo faltante",
  };

  video.addEventListener("error", () => {
    if (playingId == null || !video.getAttribute("src")) return;
    const err = video.error;
    const text = err ? (MEDIA_ERRORS[err.code] || "error " + err.code) + (err.message ? " (" + err.message + ")" : "") : "error desconocido";
    sendReport({ type: "error", id: playingId, error: text });
  });

  // --- mensajes del servidor ---

  function onMessage(msg) {
    if (msg.type === "state") {
      state = msg;
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
      showMode("espera");
      renderWaiting();
      return;
    }

    if (cur.id !== playingId) {
      startVideo(cur);
    } else {
      // Misma cancion (p. ej. tras reconectar): no tocar el video, solo la cola.
      const sig = signature();
      if (sig !== queueSignature) {
        queueSignature = sig;
        barForcedUntil = Date.now() + BAR_ON_CHANGE_MS;
      }
    }
    showMode("reproduccion");
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
    $("aviso").classList.add("oculta");
    video.src = "/media/" + encodeURIComponent(item.video_id) + ".mp4";
    video.load();
    play();
  }

  function play() {
    const p = video.play();
    if (p && p.catch) {
      p.then(() => $("bloqueado").classList.add("oculta")).catch((err) => {
        if (err && err.name === "NotAllowedError") {
          // Autoplay bloqueado: sin la bandera de Chromium. Sonar mudo es
          // peor que avisar, asi que se avisa y se espera un toque.
          $("bloqueado").classList.remove("oculta");
        }
      });
    }
  }

  $("bloqueado").addEventListener("click", () => play());

  function stopVideo() {
    if (playingId == null && !video.getAttribute("src")) return;
    playingId = null;
    video.pause();
    video.removeAttribute("src");
    video.load();
    bar.classList.remove("visible");
    $("aviso").classList.add("oculta");
    $("pausa").classList.add("oculta");
  }

  function applyPause() {
    $("pausa").classList.toggle("oculta", !state.paused);
    if (state.paused && !video.paused) video.pause();
    else if (!state.paused && video.paused && !video.ended && video.getAttribute("src")) play();
  }

  // --- capas ---

  function showMode(mode) {
    $("espera").classList.toggle("oculta", mode !== "espera");
    $("reproduccion").classList.toggle("oculta", mode !== "reproduccion");
  }

  function renderWaiting() {
    const el = $("descargando");
    const items = state.queue.slice(0, 3);
    if (!items.length) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = `<div class="titulo">Descargando…</div><ul>${items.map((i) => {
      const pct = i.state === "downloading" && i.progress != null ? ` <span class="pct">${Math.round(i.progress)}%</span>` : "";
      return `<li>${escapeHtml(i.title)} · ${escapeHtml(i.requested_by)}${pct}</li>`;
    }).join("")}</ul>`;
  }

  function renderBar() {
    const cur = state.current;
    $("barra-titulo").textContent = cur.title;
    $("barra-quien").textContent = "la pidió " + cur.requested_by;
    const next = state.up_next;
    $("barra-siguientes").innerHTML = next.length
      ? next.map((i) => `<li>${escapeHtml(i.title)} <span class="quien">· ${escapeHtml(i.requested_by)}</span>${
          i.state === "downloading" ? ' <span class="estado">descargando</span>' : ""}</li>`).join("")
      : '<li class="quien">Nada en la cola. ¡Agreguen canciones!</li>';
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
      $("aviso-nombre").textContent = next.requested_by;
      $("aviso-cancion").textContent = next.title;
    }
    $("aviso").classList.toggle("oculta", !showNotice);
  }

  video.addEventListener("timeupdate", updateOverlays);
  // timeupdate no corre en pausa; esto cubre el fin del "forzado" por cambio de cola.
  setInterval(updateOverlays, 500);
})();
