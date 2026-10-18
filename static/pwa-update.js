/* Updates are explicit and never reload over unsaved forms or active requests. */
(() => {
  const dirty = new Set();
  let pending = 0, submitting = false, registration, available = false;
  let requested = false, reloaded = false;
  const controlledAtLoad = !!(navigator.serviceWorker && navigator.serviceWorker.controller);
  const safe = () => !dirty.size && !pending && !submitting;
  const render = () => {
    const banner = document.getElementById('pwa-update');
    if (!banner) return;
    banner.hidden = !available;
    const button = document.getElementById('pwa-update-button');
    button.disabled = !safe();
    document.getElementById('pwa-update-message').textContent = safe()
      ? 'Nova versão disponível. Você pode atualizar agora.'
      : 'Nova versão disponível. Conclua ou desfaça as alterações e aguarde as operações para atualizar.';
  };
  const reload = () => {
    if (!safe() || reloaded) return;
    reloaded = true;
    window.location.reload();
  };
  // Preserve native fetch behavior and errors while tracking in-flight requests.
  if (window.fetch) {
    const original = window.fetch.bind(window);
    window.fetch = (...args) => {
      pending++; render();
      return Promise.resolve().then(() => original(...args)).finally(() => { pending--; render(); });
    };
  }
  if (window.XMLHttpRequest) {
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function (...args) {
      pending++; render();
      const done = () => { pending--; render(); };
      this.addEventListener('loadend', done, {once: true});
      try { return originalSend.apply(this, args); }
      catch (error) { this.removeEventListener('loadend', done); done(); throw error; }
    };
  }
  function track(event) {
    const target = event.target;
    const form = target.closest && target.closest('form');
    if (form || (target.matches && target.matches('.adjusted-quantity'))) {
      const owner = form || target;
      const changed = Array.from(owner.elements || [owner]).some(el => {
        if (el.type === 'checkbox' || el.type === 'radio') return el.checked !== el.defaultChecked;
        if (el.options) {
          const options = Array.from(el.options);
          const hasDefault = options.some(opt => opt.defaultSelected);
          return options.some((opt, index) => opt.selected !== (opt.defaultSelected || (!hasDefault && index === 0)));
        }
        return el.defaultValue !== undefined && el.value !== el.defaultValue;
      });
      if (changed) dirty.add(owner); else dirty.delete(owner);
      render();
    }
  }
  document.addEventListener('input', track, true);
  document.addEventListener('change', track, true);
  document.addEventListener('reset', event => {
    queueMicrotask(() => { if (!event.defaultPrevented) dirty.delete(event.target); render(); });
  }, true);
  document.addEventListener('submit', event => {
    queueMicrotask(() => { if (!event.defaultPrevented) { submitting = true; render(); } });
  }, true);
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('pwa-update-button').addEventListener('click', () => {
      if (!safe()) return;
      requested = true;
      if (registration && registration.waiting) registration.waiting.postMessage({type: 'SKIP_WAITING'});
      else reload();
    });
    render();
  });
  if (window.addEventListener) window.addEventListener('pageshow', () => { submitting = false; render(); });
  if (navigator.serviceWorker) navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!controlledAtLoad && !requested) return;
    available = true;
    if (requested && safe()) reload();
    else render();
  });
  window.gpctaPwaUpdates = {watch(reg) {
    registration = reg;
    const check = () => { if (reg.waiting && navigator.serviceWorker.controller) { available = true; render(); } };
    check();
    reg.addEventListener('updatefound', () => {
      if (reg.installing) reg.installing.addEventListener('statechange', check);
    });
  }};
})();
