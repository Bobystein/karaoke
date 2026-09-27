// WebSocket connection shared by the screen and the phones.
// If it drops, it retries every 2 seconds without reloading the page (so the
// video on the screen isn't interrupted).
function connectWS(onMessage, onStatus) {
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  let ws = null;
  let timer = null;

  function open() {
    timer = null;
    ws = new WebSocket(url);
    ws.onopen = () => onStatus && onStatus(true);
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      onMessage(msg);
    };
    ws.onclose = () => {
      onStatus && onStatus(false);
      if (!timer) timer = setTimeout(open, 2000);
    };
    ws.onerror = () => ws.close();
  }

  open();
  return {
    send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
        return true;
      }
      return false;
    },
  };
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function mmss(seconds) {
  if (!seconds && seconds !== 0) return "";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return m + ":" + String(s).padStart(2, "0");
}
