(function () {
  const indicator = document.getElementById("ws-indicator");
  const REFRESH_ON_PATHS = ["/", "/risk", "/monitor"];

  function shouldRefresh() {
    const path = window.location.pathname;
    return REFRESH_ON_PATHS.includes(path) || path.startsWith("/stock/");
  }

  function connect() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onopen = () => indicator && indicator.classList.add("connected");
    ws.onclose = () => {
      indicator && indicator.classList.remove("connected");
      // Reconnect after a short delay if the server restarts or the
      // connection drops.
      setTimeout(connect, 3000);
    };
    ws.onerror = () => ws.close();

    ws.onmessage = () => {
      // Every event_bus message (a new signal, order, kill-switch change,
      // agent run) is a cue to refresh the current page's live data. This
      // is a deliberately simple v1: a full reload of pages backed by live
      // state, rather than hand-patching DOM per event type.
      if (shouldRefresh()) {
        window.location.reload();
      }
    };
  }

  connect();
})();
