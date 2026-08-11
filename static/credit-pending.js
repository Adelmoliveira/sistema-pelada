(() => {
  const CREDIT_PENDING_POLL_INTERVAL_MS = 60000;
  const badge = document.querySelector('#credit-pending-badge');
  if (!badge) return;

  const endpoint = badge.dataset.pendingUrl;
  const state = window.__gpctaCreditPendingPolling || { timer: null, requestInFlight: false };
  if (state.timer !== null) window.clearInterval(state.timer);
  state.timer = null;
  window.__gpctaCreditPendingPolling = state;

  const render = (count) => {
    const total = Number.isFinite(count) ? Math.max(0, count) : 0;
    badge.hidden = total === 0;
    badge.textContent = total > 9 ? '9+' : String(total);
    badge.setAttribute('aria-label', `${total} recarga${total === 1 ? '' : 's'} Pix aguardando confirmação`);
  };

  const refresh = async () => {
    if (document.visibilityState !== 'visible' || state.requestInFlight || !endpoint) return;
    state.requestInFlight = true;
    try {
      const response = await fetch(endpoint, {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return;
      const payload = await response.json();
      render(Number(payload.count || 0));
    } catch (_) {
      // A temporary network failure must not hide the last known indicator.
    } finally {
      state.requestInFlight = false;
    }
  };

  const stopPolling = () => {
    if (state.timer === null) return;
    window.clearInterval(state.timer);
    state.timer = null;
  };

  const startPolling = () => {
    stopPolling();
    if (document.visibilityState !== 'visible') return;
    state.timer = window.setInterval(refresh, CREDIT_PENDING_POLL_INTERVAL_MS);
  };

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') {
      stopPolling();
      return;
    }
    refresh();
    startPolling();
  });

  if (document.visibilityState === 'visible') {
    refresh();
    startPolling();
  }
})();
