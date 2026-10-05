document.addEventListener('click', async event => {
  const button = event.target.closest('.cancel-pix-purchase');
  if (!button || !confirm('Cancelar esta compra sem pagamento confirmado?')) return;
  button.disabled = true;
  try {
    const csrf = document.getElementById('bar-installment-config').dataset.csrf;
    const response = await fetch(button.dataset.url, {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf }
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível solicitar o cancelamento.');
    alert(data.message);
    location.reload();
  } catch (error) {
    button.disabled = false;
    alert(error.message);
  }
});
