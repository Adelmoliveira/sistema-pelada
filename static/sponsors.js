(() => {
  'use strict';
  const list = document.getElementById('sponsor-order');
  if (!list) return;
  const status = document.getElementById('sponsor-order-status');
  const save = document.getElementById('sponsor-save-order');
  let dragged = null;
  list.addEventListener('dragstart', event => {
    dragged = event.target.closest('[data-id]');
    if (dragged) { event.dataTransfer.setData('text/plain', dragged.dataset.id); event.dataTransfer.effectAllowed = 'move'; }
  });
  list.addEventListener('dragover', event => { if (dragged) event.preventDefault(); });
  list.addEventListener('drop', event => {
    const target = event.target.closest('[data-id]');
    if (!dragged || !target || dragged === target) return;
    event.preventDefault();
    const after = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
    list.insertBefore(dragged, after ? target.nextSibling : target);
    dragged = null;
  });
  list.addEventListener('dragend', () => { dragged = null; });
  list.addEventListener('click', event => {
    const button = event.target.closest('[data-move]');
    if (!button) return;
    const item = button.closest('[data-id]');
    if (button.dataset.move === 'up' && item.previousElementSibling) list.insertBefore(item, item.previousElementSibling);
    if (button.dataset.move === 'down' && item.nextElementSibling) list.insertBefore(item.nextElementSibling, item);
    button.focus();
  });
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const response = await fetch(list.dataset.url, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json',
          'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify({revision: Number(list.dataset.revision), ids: [...list.children].map(item => Number(item.dataset.id))})});
      if (!response.ok || response.redirected) throw new Error('Não foi possível salvar. Recarregue a página para conferir alterações ou sua sessão.');
      const data = await response.json();
      if (!data.ok) throw new Error('Resposta inválida. Recarregue a página.');
      // Refresh all revision-bearing forms and the authoritative persisted order.
      window.location.reload();
    } catch (error) { status.textContent = error.message; save.disabled = false; }
  });
})();
