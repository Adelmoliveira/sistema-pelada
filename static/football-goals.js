(function () {
  function configureGoalType(form) {
    var goalType = form.querySelector('select[name="goal_type"]');
    var assist = form.querySelector('select[name="assist_player_id"]');
    if (!goalType || !assist) return;

    var action = form.querySelector('input[name="action"]');
    if (action && action.value === 'goal') {
      var orderedFields = [
        'match_id',
        'benefited_team',
        'author_player_id',
        'assist_player_id',
        'goal_type'
      ];
      var orderedColumns = orderedFields.map(function (fieldName) {
        var field = form.querySelector('[name="' + fieldName + '"]');
        var column = field && field.closest('[class*="col-"]');
        if (column) column.className = 'col-md-2';
        return column;
      });
      var buttonColumn = form.querySelector('button[type="submit"], button:not([type])')?.closest('[class*="col-"]');
      orderedColumns.concat(buttonColumn).forEach(function (column) {
        if (column) form.appendChild(column);
      });
    }

    function refreshAssist() {
      var allowsAssist = goalType.value === 'NORMAL';
      assist.disabled = !allowsAssist;
      assist.title = allowsAssist
        ? 'Selecione quem deu a assistência'
        : 'Assistência disponível somente para o tipo Gol';
      if (!allowsAssist) assist.value = '';
    }

    goalType.addEventListener('change', refreshAssist);
    refreshAssist();
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('form').forEach(configureGoalType);
  });
})();
