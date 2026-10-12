// Phone view: draws the queue from the state the server sends over the
// WebSocket. The server checks permissions; here we only hide buttons that
// don't apply.
(() => {
  const ME = document.body.dataset.owner;
  const IS_ADMIN = document.body.dataset.admin === "1";
  let state = null;

  const $ = (id) => document.getElementById(id);

  // --- notices ---

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
      toast("Can't reach the karaoke");
      return null;
    }
    if (!resp.ok) {
      toast(await errorText(resp));
      return null;
    }
    return resp;
  }

  // Errors from HTMX requests (search and add).
  document.body.addEventListener("htmx:responseError", async (e) => {
    const xhr = e.detail.xhr;
    let text = "Error " + xhr.status;
    try { text = JSON.parse(xhr.responseText).detail || text; } catch {}
    toast(text);
  });
  document.body.addEventListener("htmx:sendError", () => toast("Can't reach the karaoke"));

  // --- drawing the queue ---

  function badge(item) {
    const pct = item.progress;
    switch (item.state) {
      case "downloading":
        return `<span class="text-sky-300">downloading${pct != null ? " " + Math.round(pct) + "%" : "…"}</span>`;
      case "queued":
        // Its download failed and the server will try again on its own.
        if (item.retry_at) return `<span class="text-amber-300">retrying at ${clock(item.retry_at)}</span>`;
        return `<span class="text-neutral-400">waiting</span>`;
      case "ready":
        return `<span class="text-emerald-300">ready</span>`;
      default:
        return "";
    }
  }

  // Unix seconds to the phone's local HH:MM.
  function clock(unix) {
    return new Date(unix * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // Why a song is waiting to be retried. Only for whoever can do something about it.
  function retryNote(item) {
    if (item.state !== "queued" || !item.retry_at || !item.error || !canRemove(item)) return "";
    return `<p class="mt-1 text-xs text-amber-200/80 break-words">${escapeHtml(item.error)}</p>`;
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

  function removeButton(item, label = "Remove") {
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
      $("now-playing").innerHTML = "";
      return;
    }
    $("now-playing").innerHTML = `
      <div class="rounded-2xl bg-gradient-to-r from-fuchsia-700 to-violet-700 p-3 flex items-center gap-3 shadow-lg">
        ${cur.thumb_url ? `<img src="${escapeHtml(cur.thumb_url)}" alt="" class="w-20 aspect-video rounded-lg object-cover shrink-0">` : ""}
        <div class="min-w-0 flex-1">
          <p class="text-xs uppercase tracking-widest text-fuchsia-200">${state.paused ? "Paused" : "Now playing"}</p>
          <p class="font-bold leading-snug line-clamp-2">${escapeHtml(cur.title)}</p>
          <p class="text-sm text-fuchsia-100/80 truncate">${escapeHtml(cur.requested_by)}</p>
        </div>
        ${removeButton(cur)}
      </div>`;
  }

  function renderQueue() {
    const items = state.queue;
    if (!items.length) {
      $("queue").innerHTML = `<li class="text-neutral-500 py-2">${
        state.current ? "Nothing else in the queue." : "The queue is empty. Search for a song!"
      }</li>`;
      return;
    }
    $("queue").innerHTML = items.map((item, idx) => `
      <li class="flex items-center gap-3 rounded-2xl bg-neutral-900 p-3 ${item.owner === ME ? "ring-1 ring-fuchsia-500/40" : ""}">
        <span class="w-6 text-center text-lg font-black text-neutral-500 shrink-0">${idx + 1}</span>
        <div class="min-w-0 flex-1">
          <p class="font-semibold leading-snug line-clamp-2">${escapeHtml(item.title)}</p>
          <p class="text-sm text-neutral-400 truncate">${escapeHtml(item.requested_by)} · ${badge(item)}</p>
          ${progressBar(item)}
          ${retryNote(item)}
        </div>
        ${moveButtons(item, idx, items.length)}
        ${removeButton(item)}
      </li>`).join("");
  }

  // A song that is already on disk: asking for it again needs no search.
  function addButton(videoId, label = "Add") {
    return `<button data-add="${escapeHtml(videoId)}" class="shrink-0 rounded-xl bg-fuchsia-600 active:bg-fuchsia-700 px-4 py-2.5 font-bold">${label}</button>`;
  }

  function renderRecent() {
    const items = state.recent || [];
    $("recent").hidden = !items.length;
    $("recent-list").innerHTML = items.map((item) => `
      <li class="flex items-center gap-3 rounded-2xl bg-neutral-900 p-3">
        <div class="min-w-0 flex-1">
          <p class="font-semibold leading-snug line-clamp-2">${escapeHtml(item.title)}</p>
          <p class="text-sm text-neutral-400 truncate">${escapeHtml(item.requested_by)}</p>
        </div>
        ${addButton(item.video_id, "Again")}
      </li>`).join("");
  }

  function renderFailed() {
    const mine = state.failed.filter((f) => IS_ADMIN || f.owner === ME);
    $("failed").innerHTML = mine.map((f) => `
      <div class="rounded-2xl bg-red-950/60 border border-red-900 p-3">
        <div class="flex items-center gap-3">
          <div class="min-w-0 flex-1">
            <p class="text-sm text-red-300">Couldn't play ${f.owner === ME ? "your song" : escapeHtml(f.requested_by) + "'s song"}:</p>
            <p class="font-semibold leading-snug line-clamp-2">${escapeHtml(f.title)}</p>
            <p class="text-sm text-red-200/90 break-words">${escapeHtml(f.error || "")}</p>
          </div>
          ${removeButton(f, "OK")}
        </div>
        ${f.owner === ME ? `<button data-retry="${f.id}"
            class="mt-2 w-full rounded-xl bg-fuchsia-600 active:bg-fuchsia-700 py-2.5 font-bold">Try another version</button>` : ""}
      </div>`).join("");
  }

  function renderAdmin() {
    const btn = $("btn-pause");
    if (btn) btn.textContent = state.paused ? "▶ Resume" : "⏸ Pause";
    const qr = $("btn-qr");
    if (qr) {
      qr.textContent = state.qr_pinned ? "📌 QR pinned on screen · tap to unpin" : "📌 Pin QR on screen";
      qr.classList.toggle("ring-2", !!state.qr_pinned);
      qr.classList.toggle("ring-amber-400", !!state.qr_pinned);
    }
  }

  function render() {
    if (!state) return;
    renderCurrent();
    renderQueue();
    renderFailed();
    renderRecent();
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
      const dot = $("connection");
      dot.classList.toggle("bg-emerald-500", up);
      dot.classList.toggle("bg-red-500", !up);
      dot.classList.remove("bg-neutral-600");
    },
  );

  // --- downloaded songs ---
  // The whole list comes down once, when the section is opened, and is
  // filtered and grouped here: it has to work with no internet and be instant.

  const lib = { songs: null, view: "artist", open: new Set() };
  const OTHER = "Other";  // catalog.UNKNOWN_ARTIST

  // Same idea as catalog.key(): no accents, no case.
  function norm(text) {
    return (text || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
  }

  function mmss(seconds) {
    if (!seconds) return "";
    return Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
  }

  function songRow(song, showArtist) {
    const details = [showArtist ? song.artist : "", mmss(song.duration_s)].filter(Boolean).join(" · ");
    const edit = IS_ADMIN
      ? `<button data-artist="${escapeHtml(song.video_id)}" title="Change the artist" aria-label="Change the artist"
                 class="shrink-0 rounded-xl bg-neutral-800 active:bg-neutral-700 w-10 h-10">✎</button>`
      : "";
    return `
      <div class="flex items-center gap-2 rounded-2xl bg-neutral-900 p-2 pl-3">
        <div class="min-w-0 flex-1">
          <p class="font-semibold leading-snug line-clamp-2">${escapeHtml(song.title)}</p>
          ${details ? `<p class="text-sm text-neutral-400 truncate">${escapeHtml(details)}</p>` : ""}
        </div>
        ${edit}
        ${addButton(song.video_id)}
      </div>`;
  }

  function renderLibrary() {
    const list = $("library-list");
    if (lib.songs === null) return;
    const q = norm($("library-filter").value);
    const songs = q
      ? lib.songs.filter((s) => norm(s.title + " " + s.artist).includes(q))
      : lib.songs;
    $("library-count").textContent = q
      ? `${songs.length} of ${lib.songs.length}`
      : `${lib.songs.length} song${lib.songs.length === 1 ? "" : "s"}`;
    if (!lib.songs.length) {
      list.innerHTML = `<p class="text-neutral-500">Nothing downloaded yet. Songs stay here after they play once.</p>`;
      return;
    }
    if (!songs.length) {
      list.innerHTML = `<p class="text-neutral-500">Nothing downloaded matches. Try the search at the top.</p>`;
      return;
    }
    if (lib.view === "title") {  // the server already sorted them by title
      list.innerHTML = songs.map((s) => songRow(s, true)).join("");
      return;
    }
    const groups = new Map();
    for (const s of songs) {
      if (!groups.has(s.artist)) groups.set(s.artist, []);
      groups.get(s.artist).push(s);
    }
    const artists = [...groups.keys()].sort((a, b) =>
      (a === OTHER) - (b === OTHER) || norm(a).localeCompare(norm(b)));
    // While filtering every group is open: the matches are what you came for.
    list.innerHTML = artists.map((artist) => `
      <details data-artist-group="${escapeHtml(artist)}" ${q || lib.open.has(artist) ? "open" : ""}
               class="rounded-2xl border border-neutral-800">
        <summary class="cursor-pointer select-none px-3 py-2.5 flex items-center gap-2">
          <span class="min-w-0 flex-1 truncate font-semibold">${escapeHtml(artist)}</span>
          <span class="text-sm text-neutral-500">${groups.get(artist).length}</span>
        </summary>
        <div class="flex flex-col gap-2 p-2 pt-0">${groups.get(artist).map((s) => songRow(s, false)).join("")}</div>
      </details>`).join("");
  }

  async function loadLibrary() {
    const resp = await request("GET", "/library");
    if (!resp) {
      if (lib.songs === null) $("library-list").innerHTML = `<p class="text-neutral-500">Couldn't load the list.</p>`;
      return;
    }
    lib.songs = (await resp.json()).songs;
    renderLibrary();
  }

  $("library").addEventListener("toggle", (e) => {
    if (e.target.id === "library") {
      if (e.target.open) loadLibrary();  // every time: songs downloaded since are there
      return;
    }
    // An artist opened or closed by hand; remembered across redraws. Not
    // while filtering, when they are all open anyway.
    const artist = e.target.dataset.artistGroup;
    if (artist === undefined || norm($("library-filter").value)) return;
    if (e.target.open) lib.open.add(artist);
    else lib.open.delete(artist);
  }, true);  // toggle doesn't bubble

  $("library-filter").addEventListener("input", renderLibrary);
  $("library").addEventListener("change", (e) => {
    if (e.target.name !== "library-view") return;
    lib.view = e.target.value;
    renderLibrary();
  });

  async function changeArtist(videoId) {
    const song = (lib.songs || []).find((s) => s.video_id === videoId);
    if (!song) return;
    const artist = prompt(`Artist of “${song.title}”\n(leave empty to guess it from the title)`, song.artist === OTHER ? "" : song.artist);
    if (artist === null) return;
    if (await request("POST", `/admin/songs/${encodeURIComponent(videoId)}/artist`, { artist })) loadLibrary();
  }

  // --- search mode and "try another version" ---

  const form = $("search");
  const MODE_KEY = "karaoke.mode";

  function setMode(mode, remember) {
    const radio = form.querySelector(`input[name="mode"][value="${CSS.escape(mode || "")}"]`);
    if (!radio) return;
    radio.checked = true;
    if (remember) try { sessionStorage.setItem(MODE_KEY, mode); } catch {}
  }

  try { setMode(sessionStorage.getItem(MODE_KEY)); } catch {}

  function search() {
    if (form.q.value.trim()) htmx.trigger(form, "submit");
  }

  form.addEventListener("change", (e) => {
    if (e.target.name !== "mode") return;
    setMode(e.target.value, true);
    search();
  });

  // Typing a new query forgets which result had failed.
  form.q.addEventListener("input", () => { form.failed.value = ""; });

  function retry(item) {
    form.q.value = item.query || item.title;
    if (item.search_mode) setMode(item.search_mode, true);
    form.failed.value = item.video_id;
    window.scrollTo({ top: 0, behavior: "smooth" });
    search();
    // The notice has done its job; it goes away like tapping "OK".
    request("DELETE", "/queue/" + item.id);
  }

  // --- actions ---

  document.addEventListener("click", async (e) => {
    const again = e.target.closest("[data-retry]");
    if (again) {
      const item = state && state.failed.find((f) => String(f.id) === again.dataset.retry);
      if (item) retry(item);
      return;
    }

    const add = e.target.closest("[data-add]");
    if (add) {
      add.disabled = true;
      const label = add.textContent;
      if (await request("POST", "/queue", { video_id: add.dataset.add })) {
        add.textContent = "Queued ✓";
        setTimeout(() => { add.textContent = label; add.disabled = false; }, 2000);
      } else {
        add.disabled = false;
      }
      return;
    }

    const artist = e.target.closest("[data-artist]");
    if (artist) {
      await changeArtist(artist.dataset.artist);
      return;
    }

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
      case "qr":
        await request("POST", "/admin/qr", { pinned: state && state.qr_pinned ? "0" : "1" });
        break;
      case "clear":
        if (confirm("Clear the whole queue? The current song keeps playing.")) {
          await request("POST", "/admin/clear");
        }
        break;
      case "seek-back":
      case "seek-forward":
        await request("POST", "/admin/seek", { delta: act.dataset.adminAction === "seek-forward" ? 5 : -5 });
        break;
      case "vol-down":
      case "vol-up": {
        const delta = act.dataset.adminAction === "vol-up" ? 5 : -5;
        const resp = await request("POST", "/admin/volume", { delta });
        if (resp) $("volume").textContent = (delta > 0 ? "+" : "−") + "5%";
        break;
      }
    }
  });

  // --- change your name ---

  const rename = $("rename");
  const nameText = $("my-name-text");

  function toggleRename(open) {
    rename.hidden = !open;
    if (open) {
      rename.name.value = nameText.textContent;
      rename.name.focus();
      rename.name.select();
    }
  }

  $("my-name").addEventListener("click", () => toggleRename(rename.hidden));
  rename.querySelector("[data-rename-cancel]").addEventListener("click", () => toggleRename(false));

  rename.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = rename.querySelector("button");
    btn.disabled = true;
    const resp = await request("POST", "/session/name", { name: rename.name.value });
    btn.disabled = false;
    if (!resp) return;
    const { name } = await resp.json();
    nameText.textContent = name;
    toggleRename(false);
    toast("You're now " + name);
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
