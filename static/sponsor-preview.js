(() => {
  'use strict';
  document.querySelectorAll('[data-sponsor-editor]').forEach(form => {
    const file = form.querySelector('[name="logo"]');
    const remove = form.querySelector('[name="remove_background"]');
    const confirmation = form.querySelector('[name="confirm_background"]');
    const image = form.querySelector('[data-logo-preview]');
    const button = form.querySelector('[data-preview-logo]');
    const status = form.querySelector('[data-preview-status]');
    const original = image.getAttribute('src');
    let generation = 0, objectURL = null, reviewed = false;
    confirmation.disabled = true;
    function show(blob) {
      if (objectURL) URL.revokeObjectURL(objectURL);
      objectURL = URL.createObjectURL(blob);
      image.src = objectURL;
      image.hidden = false;
    }
    function reset() {
      generation++;
      reviewed = false;
      confirmation.checked = false;
      confirmation.disabled = true;
      button.disabled = false;
      if (objectURL) { URL.revokeObjectURL(objectURL); objectURL = null; }
      const selected = file.files[0];
      if (selected && selected.size <= 2 * 1024 * 1024 && ['image/png', 'image/jpeg', 'image/webp'].includes(selected.type)) {
        show(selected);
        status.textContent = 'Imagem selecionada. Gere a prévia para conferir o resultado otimizado.';
      } else {
        image.hidden = !original;
        if (original) image.src = original;
        status.textContent = selected ? 'Envie PNG, JPEG ou WebP de até 2 MB.' : '';
      }
    }
    file.addEventListener('change', reset);
    remove.addEventListener('change', reset);
    button.addEventListener('click', async () => {
      if (remove.checked && !window.confirm('Gerar prévia removendo apenas o fundo uniforme? Confira se nenhuma parte da marca foi removida antes de salvar.')) return;
      const token = ++generation;
      reviewed = false;
      confirmation.checked = false;
      confirmation.disabled = true;
      button.disabled = true;
      status.textContent = 'Preparando prévia…';
      try {
        const data = new FormData(form);
        if (remove.checked) data.set('confirm_background', '1');
        const response = await fetch(form.dataset.previewUrl, {method: 'POST', credentials: 'same-origin', body: data,
          headers: {'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content}});
        if (!response.ok || response.redirected || !response.headers.get('Content-Type')?.startsWith('image/webp')) {
          let message = 'Não foi possível preparar a prévia. Recarregue a página e confira sua sessão.';
          if (response.headers.get('Content-Type')?.includes('application/json')) message = (await response.json()).error || message;
          throw new Error(message);
        }
        const blob = await response.blob();
        if (token !== generation) return;
        show(blob);
        reviewed = true;
        confirmation.disabled = !remove.checked;
        status.textContent = remove.checked ? 'Confira a identidade da marca e marque a confirmação antes de salvar.' : 'Prévia otimizada. A imagem só será gravada ao salvar.';
      } catch (error) {
        if (token === generation) status.textContent = error.message;
      } finally {
        if (token === generation) button.disabled = false;
      }
    });
    form.addEventListener('submit', event => {
      if (remove.checked && (!reviewed || !confirmation.checked)) {
        event.preventDefault();
        status.textContent = 'Gere e revise a prévia; depois confirme a remoção do fundo.';
        button.focus();
      }
    });
  });
})();
