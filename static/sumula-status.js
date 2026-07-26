/* Aplica cores às situações na lista de súmulas. Arquivo externo para respeitar CSP. */
(function () {
  var colors = {
    'Rascunho': ['#f4b400', '#172b3a'],
    'Aberta': ['#0d6efd', '#ffffff'],
    'Em andamento': ['#f08c00', '#ffffff'],
    'Finalizada': ['#198754', '#ffffff'],
    'Cancelada': ['#dc3545', '#ffffff'],
    'Encerrada definitivamente': ['#dc3545', '#ffffff']
  };

  document.querySelectorAll('table tbody tr td:nth-child(6) .badge').forEach(function (badge) {
    var color = colors[badge.textContent.trim()];
    if (!color) return;
    badge.classList.add('sumula-status');
    badge.style.setProperty('background-color', color[0], 'important');
    badge.style.setProperty('color', color[1], 'important');
  });
})();
