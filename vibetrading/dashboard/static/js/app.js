(function () {
  const indicator = document.getElementById("ws-indicator");
  const REFRESH_ON_PATHS = ["/", "/risk", "/monitor"];

  // Auto-reload is suppressed for this long after any htmx request the user
  // triggered. Without it, clicking Analyze published agent_output events
  // that came straight back over the WebSocket and reloaded the page --
  // discarding the very analysis fragment that had just been swapped in, so
  // the button looked like it did nothing (or briefly flashed a result).
  const SELF_ACTION_QUIET_MS = 10000;
  const RELOAD_DEBOUNCE_MS = 750;

  let lastOwnRequestAt = 0;
  let pendingReload = null;

  document.body.addEventListener("htmx:beforeRequest", () => {
    lastOwnRequestAt = Date.now();
  });
  document.body.addEventListener("htmx:afterRequest", () => {
    lastOwnRequestAt = Date.now();
  });

  // htmx drops the response body of any non-2xx by default, which is why a
  // failed Analyze or Backtest used to leave the panel unchanged with no
  // explanation. These endpoints answer errors with a rendered state card,
  // so swap those in; anything that isn't one of our fragments gets a
  // generic card rather than a raw server error page.
  document.body.addEventListener("htmx:beforeSwap", (evt) => {
    const xhr = evt.detail.xhr;
    if (!xhr || xhr.status < 400) return;

    evt.detail.shouldSwap = true;
    evt.detail.isError = false;

    const body = xhr.responseText || "";
    if (!body.includes("state-card")) {
      evt.detail.serverResponse =
        '<section class="card state-card state-error">' +
        '<div class="card-header"><h2 class="card-title">Request failed</h2>' +
        '<span class="status-pill status-error">HTTP ' +
        xhr.status +
        "</span></div><p>The server could not complete this request. " +
        "Check the application logs for the full error.</p></section>";
    }
  });

  function shouldRefresh() {
    const path = window.location.pathname;
    return REFRESH_ON_PATHS.includes(path) || path.startsWith("/stock/");
  }

  function scheduleReload() {
    if (!shouldRefresh()) return;
    if (Date.now() - lastOwnRequestAt < SELF_ACTION_QUIET_MS) return;
    if (pendingReload) return;
    pendingReload = setTimeout(() => {
      pendingReload = null;
      // Re-check: the user may have started interacting during the debounce.
      if (Date.now() - lastOwnRequestAt >= SELF_ACTION_QUIET_MS) {
        window.location.reload();
      }
    }, RELOAD_DEBOUNCE_MS);
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
      // agent run) is a cue to refresh the current page's live data -- but
      // never on top of what the user is doing right now.
      scheduleReload();
    };
  }

  connect();
})();
