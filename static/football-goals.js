(function () {
  function addGoalTypeField(form) {
    if (form.querySelector('[name="own_goal"]')) return;
    var action = form.querySelector('input[name="action"]');
    if (!action || !['goal', 'update_goal'].includes(action.value)) return;

    var isEdit = action.value === 'update_goal';
    var wrapper = document.createElement('div');
    wrapper.className = 'col-md-2 col-12';
    var label = document.createElement('label');
    label.className = 'form-label';
    label.textContent = 'Tipo de gol';
    var select = document.createElement('select');
    select.className = 'form-select';
    select.name = 'own_goal';
    select.title = 'Escolha se o gol foi normal ou contra';
    if (isEdit) {
      var keep = new Option('Manter atual', '');
      select.appendChild(keep);
    }
    select.appendChild(new Option('Gol normal', '0'));
    select.appendChild(new Option('Gol contra', '1'));
    wrapper.appendChild(label);
    wrapper.appendChild(select);

    var minute = form.querySelector('[name="minute"]');
    var minuteWrapper = minute && (minute.closest('.col-md-2') || minute.parentElement);
    if (minuteWrapper && minuteWrapper.parentElement) {
      minuteWrapper.parentElement.insertBefore(wrapper, minuteWrapper);
    } else {
      form.appendChild(wrapper);
    }

    var assist = form.querySelector('[name="assist_player_id"]');
    if (assist) {
      select.addEventListener('change', function () {
        var own = select.value === '1';
        assist.disabled = own;
        if (own) assist.value = '';
      });
      select.dispatchEvent(new Event('change'));
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('form').forEach(addGoalTypeField);
  });
})();
