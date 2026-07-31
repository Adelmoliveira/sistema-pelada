(() => {
  const badge = document.querySelector('#credit-pending-badge');
  if (!badge) return;

  const endpoint = badge.dataset.pendingUrl;
  let requestInFlight = false;

  const render = (count) => {
    const total = Number.isFinite(count) ? Math.max(0, count) : 0;
    badge.hidden = total === 0;
    badge.textContent = total > 9 ? '9+' : String(total);
    badge.setAttribute('aria-label', `${total} recarga${total === 1 ? '' : 's'} Pix aguardando confirmação`);
  };

  const refresh = async () => {
    if (requestInFlight || !endpoint) return;
    requestInFlight = true;
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
      requestInFlight = false;
    }
  };

  refresh();
  window.setInterval(refresh, 15000);
})();
