// Vista del celular: pinta la cola con el estado que manda el servidor por
// WebSocket. Los permisos los verifica el servidor; aqui solo se esconden
// botones que no aplican.
(() => {
  const ME = document.body.dataset.owner;
  const IS_ADMIN = document.body.dataset.admin === "1";
  let state = null;

  const $ = (id) => document.getElementById(id);

  // --- avisos ---

  let toastTimer = null;
  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.classList.remove("opacity-0");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("opacity-0"), 3000);
  }

  async function errorText(resp) {
    try {
      const data = await resp.json();
      if (data && data.detail) return String(data.detail);
    } catch {}
    return "Error " + resp.status;
  }

  async function request(method, url, data) {
    let resp;
    try {
      resp = await fetch(url, {
        method,
        body: data ? new URLSearchParams(data) : undefined,
      });
    } catch {
      toast("Sin conexión con el karaoke");
      return null;
    }
    if (!resp.ok) {
      toast(await errorText(resp));
      return null;
    }
    return resp;
  }

  // Errores de las peticiones HTMX (busqueda y agregar).
  document.body.addEventListener("htmx:responseError", async (e) => {
    const xhr = e.detail.xhr;
    let text = "Error " + xhr.status;
    try { text = JSON.parse(xhr.responseText).detail || text; } catch {}
    toast(text);
  });
  document.body.addEventListener("htmx:sendError", () => toast("Sin conexión con el karaoke"));

  // --- pintar la cola ---

  function badge(item) {
    const pct = item.progress;
    switch (item.state) {
      case "downloading":
        return `<span class="text-sky-300">descargando${pct != null ? " " + Math.round(pct) + "%" : "…"}</span>`;
      case "queued":
        return `<span class="text-neutral-400">en espera</span>`;
      case "ready":
        return `<span class="text-emerald-300">lista</span>`;
      default:
        return "";
    }
  }

  function progressBar(item) {
    if (item.state !== "downloading") return "";
    const pct = Math.max(2, Math.round(item.progress || 0));
    return `<div class="mt-1.5 h-1 rounded-full bg-neutral-800 overflow-hidden">
              <div class="h-full bg-sky-400 transition-all" style="width:${pct}%"></div>
            </div>`;
  }

  function canRemove(item) {
    return IS_ADMIN || item.owner === ME;
  }

  function removeButton(item, label = "Quitar") {
    if (!canRemove(item)) return "";
    return `<button data-remove="${item.id}" class="shrink-0 rounded-xl bg-neutral-800 active:bg-neutral-700 px-3 py-2 text-sm">${label}</button>`;
  }

  function moveButtons(item, idx, total) {
    if (!IS_ADMIN) return "";
    const btn = (delta, label, disabled) =>
      `<button data-move="${item.id}" data-delta="${delta}" ${disabled ? "disabled" : ""}
               class="rounded-lg bg-neutral-800 active:bg-neutral-700 w-9 h-9 disabled:opacity-30">${label}</button>`;
    return `<div class="flex flex-col gap-1 shrink-0">${btn(-1, "↑", idx === 0)}${btn(1, "↓", idx === total - 1)}</div>`;
  }

  function renderCurrent() {
    const cur = state.current;
    if (!cur) {
      $("sonando").innerHTML = "";
      return;
    }
    $("sonando").innerHTML = `
      <div class="rounded-2xl bg-gradient-to-r from-fuchsia-700 to-violet-700 p-3 flex items-center gap-3 shadow-lg">
        ${cur.thumb_url ? `<img src="${escapeHtml(cur.thumb_url)}" alt="" class="w-20 aspect-video rounded-lg object-cover shrink-0">` : ""}
        <div class="min-w-0 flex-1">
          <p class="text-xs uppercase tracking-widest text-fuchsia-200">${state.paused ? "En pausa" : "Sonando"}</p>
          <p class="font-bold leading-snug line-clamp-2">${escapeHtml(cur.title)}</p>
          <p class="text-sm text-fuchsia-100/80 truncate">${escapeHtml(cur.requested_by)}</p>
        </div>
        ${removeButton(cur)}
      </div>`;
  }

  function renderQueue() {
    const items = state.queue;
    if (!items.length) {
      $("cola").innerHTML = `<li class="text-neutral-500 py-2">${
        state.current ? "No hay nada más en la cola." : "La cola está vacía. ¡Busca una canción!"
      }</li>`;
      return;
    }
    $("cola").innerHTML = items.map((item, idx) => `
      <li class="flex items-center gap-3 rounded-2xl bg-neutral-900 p-3 ${item.owner === ME ? "ring-1 ring-fuchsia-500/40" : ""}">
        <span class="w-6 text-center text-lg font-black text-neutral-500 shrink-0">${idx + 1}</span>
        <div class="min-w-0 flex-1">
          <p class="font-semibold leading-snug line-clamp-2">${escapeHtml(item.title)}</p>
          <p class="text-sm text-neutral-400 truncate">${escapeHtml(item.requested_by)} · ${badge(item)}</p>
          ${progressBar(item)}
        </div>
        ${moveButtons(item, idx, items.length)}
        ${removeButton(item)}
      </li>`).join("");
  }

  function renderFailed() {
    const mine = state.failed.filter((f) => IS_ADMIN || f.owner === ME);
    $("fallidas").innerHTML = mine.map((f) => `
      <div class="rounded-2xl bg-red-950/60 border border-red-900 p-3 flex items-center gap-3">
        <div class="min-w-0 flex-1">
          <p class="text-sm text-red-300">No se pudo ${f.owner === ME ? "poner tu canción" : "poner la de " + escapeHtml(f.requested_by)}:</p>
          <p class="font-semibold leading-snug line-clamp-2">${escapeHtml(f.title)}</p>
          <p class="text-xs text-red-300/80 break-words">${escapeHtml(f.error || "")}</p>
        </div>
        ${removeButton(f, "OK")}
      </div>`).join("");
  }

  function renderAdmin() {
    const btn = $("btn-pausa");
    if (btn) btn.textContent = state.paused ? "▶ Reanudar" : "⏸ Pausar";
  }

  function render() {
    if (!state) return;
    renderCurrent();
    renderQueue();
    renderFailed();
    renderAdmin();
  }

  function applyProgress(videoId, pct) {
    if (!state) return;
    let hit = false;
    for (const item of state.queue) {
      if (item.video_id === videoId) {
        item.progress = pct;
        hit = true;
      }
    }
    if (hit) renderQueue();
  }

  connectWS(
    (msg) => {
      if (msg.type === "state") {
        state = msg;
        render();
      } else if (msg.type === "progress") {
        applyProgress(msg.video_id, msg.pct);
      }
    },
    (up) => {
      const dot = $("conexion");
      dot.classList.toggle("bg-emerald-500", up);
      dot.classList.toggle("bg-red-500", !up);
      dot.classList.remove("bg-neutral-600");
    },
  );

  // --- acciones ---

  document.addEventListener("click", async (e) => {
    const rm = e.target.closest("[data-remove]");
    if (rm) {
      rm.disabled = true;
      if (!(await request("DELETE", "/queue/" + rm.dataset.remove))) rm.disabled = false;
      return;
    }

    const mv = e.target.closest("[data-move]");
    if (mv) {
      await request("POST", "/admin/reorder", { id: mv.dataset.move, delta: mv.dataset.delta });
      return;
    }

    const act = e.target.closest("[data-admin-action]");
    if (!act) return;
    switch (act.dataset.adminAction) {
      case "skip":
        await request("POST", "/admin/skip");
        break;
      case "pause":
        await request("POST", "/admin/pause", { paused: state && state.paused ? "0" : "1" });
        break;
      case "clear":
        if (confirm("¿Vaciar toda la cola? La canción que suena sigue.")) {
          await request("POST", "/admin/clear");
        }
        break;
      case "vol-down":
      case "vol-up": {
        const delta = act.dataset.adminAction === "vol-up" ? 5 : -5;
        const resp = await request("POST", "/admin/volume", { delta });
        if (resp) $("volumen").textContent = (delta > 0 ? "+" : "−") + "5%";
        break;
      }
    }
  });

  const login = $("admin-login");
  if (login) {
    login.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = login.querySelector("button");
      btn.disabled = true;
      const resp = await request("POST", "/admin/login", { password: login.password.value });
      btn.disabled = false;
      if (resp) location.reload();
      else login.password.select();
    });
  }
})();
