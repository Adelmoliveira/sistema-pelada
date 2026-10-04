(() => {
  const config = document.querySelector('#sports-installment-config');
  if (!config) return;
  const money = cents => new Intl.NumberFormat('pt-BR', {style:'currency',currency:'BRL'}).format(cents/100);
  const choice = document.querySelector('#sports-pix-choice');
  const mode = document.querySelector('#sports-pix-mode');
  const preview = document.querySelector('#sports-installment-preview');
  const pixButton = document.querySelector('#generate-pix');
  let sequence = 0, timer, busy = false, previewTimer, savedCheckout = null, restoredMode = false;
  const headers = {Accept:'application/json','Content-Type':'application/json','X-CSRFToken':config.dataset.csrf};
  async function request(url, body) {
    const response = await fetch(url, {method:body===undefined?'GET':'POST',cache:'no-store',headers,body:body===undefined?undefined:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível processar a parcela.');
    return data;
  }
  function items() {
    return [...document.querySelectorAll('.sale-cart-row')].map(row => ({product_id:row.querySelector('.product').value,variant_id:row.querySelector('.variant')?.value,quantity:row.querySelector('.quantity').value,order_mode:row.querySelector('.order-mode')?.value,custom_name:row.querySelector('.custom-name')?.value||'',custom_number:row.querySelector('.custom-number')?.value||''}));
  }
  async function updatePreview() {
    if (!choice) return;
    const current = ++sequence;
    const cart = items();
    const eligible = document.querySelector('#sale-department').value==='sports' && document.querySelector('#payment-method').value==='Pix' && cart.length && cart.every(item=>item.order_mode==='ready');
    choice.classList.toggle('d-none', !eligible);
    if (!eligible) {mode.value='cash';return;}
    mode.querySelector('[value="three"]').disabled=true;
    preview.textContent='Consultando valores e disponibilidade...';
    try {
      const data=await request(config.dataset.preview,{items:cart});
      if (current!==sequence) return;
      mode.querySelector('[value="three"]').disabled=false;
      if(!restoredMode){
        restoredMode=true;
        try {const saved=JSON.parse(localStorage.getItem('sports-3x-checkout')||'null');if(saved?.signature===JSON.stringify(cart))mode.value='three';}catch(_error){}
      }
      preview.textContent=`Total: ${money(data.total_cents)} · `+data.installments.map(i=>`${i.installment_number}/3: ${money(i.amount_cents)} · ${i.due_date.split('-').reverse().join('/')}`).join(' | ');
    } catch (error) {if(current===sequence){mode.value='cash';preview.textContent=error.message;}}
  }
  function schedulePreview() {clearTimeout(previewTimer);previewTimer=setTimeout(updatePreview,200);}
  if (choice) {
    const cart=document.querySelector('#items');
    new MutationObserver(schedulePreview).observe(cart,{childList:true,subtree:true});
    document.querySelector('#sale-form').addEventListener('input',schedulePreview);
    document.querySelector('#sale-form').addEventListener('change',schedulePreview);
    document.querySelector('#show-bar-products').addEventListener('click',schedulePreview);
    document.querySelector('#show-sports-products').addEventListener('click',schedulePreview);
    schedulePreview();
  }
  function showState(data) {
    const status=document.querySelector('#pix-status');
    const plan=data.plan;
    status.className='alert '+(data.paid?'alert-success':'alert-info');
    status.textContent=data.paid ? `Pagamento confirmado. ${plan.status==='paid'?'Quitado. ':''}${plan.withdrawal_allowed?'Material liberado para retirada. ':'Retirada bloqueada até pagamento da primeira parcela. '}Saldo restante: ${money(plan.balance_cents)}` : 'Aguardando pagamento. A primeira parcela libera a retirada; as demais permanecem devidas.';
    if (data.paid) {
      ['#pix-image','#pix-payload','#pix-payload-label','#copy-pix'].forEach(id=>document.querySelector(id).classList.add('d-none'));
      if(choice){if(typeof clearSavedCart==='function')clearSavedCart();try{localStorage.removeItem('sports-3x-checkout');}catch(_error){}savedCheckout=null;}
      timer=setTimeout(()=>location.assign(config.dataset.purchases),4000);
    }
  }
  async function poll(url) {
    try {
      const data=await request(url);
      showState(data);
      if(!data.paid)timer=setTimeout(()=>poll(url),5000);
    } catch(error) {document.querySelector('#pix-error').textContent=error.message;document.querySelector('#pix-error').classList.remove('d-none');timer=setTimeout(()=>poll(url),7000);}
  }
  async function pay(url, body, button) {
    if(busy)return;
    busy=true;button.disabled=true;clearTimeout(timer);
    const modal=bootstrap.Modal.getOrCreateInstance(document.querySelector('#pix-modal'));
    modal.show();
    document.querySelector('#pix-error').classList.add('d-none');
    document.querySelector('#pix-status').className='alert alert-info';
    document.querySelector('#pix-status').textContent='Gerando cobrança da parcela...';
    ['#pix-image','#pix-payload','#pix-payload-label','#copy-pix'].forEach(id=>document.querySelector(id).classList.add('d-none'));
    try {
      const data=await request(url,body);
      if(choice&&typeof lockCheckout==='function')lockCheckout(true);
      showState(data);
      document.querySelector('#pix-summary').textContent=`Parcela: ${data.amount} · Pix 3x`;
      if(!data.paid){
        if(data.image){document.querySelector('#pix-image').src=data.image;document.querySelector('#pix-image').classList.remove('d-none');}
        if(data.payload){document.querySelector('#pix-payload').value=data.payload;['#pix-payload','#pix-payload-label','#copy-pix'].forEach(id=>document.querySelector(id).classList.remove('d-none'));}
        else document.querySelector('#pix-status').textContent='Cobrança em processamento. Acompanhe em Minhas Compras; não gere outra compra.';
        poll(data.status_url);
      }
    } catch(error){document.querySelector('#pix-error').textContent=error.message;document.querySelector('#pix-error').classList.remove('d-none');}
    finally {busy=false;button.disabled=false;}
  }
  pixButton?.addEventListener('click',event=>{
    if(!mode||mode.value!=='three'||choice.classList.contains('d-none'))return;
    event.preventDefault();event.stopImmediatePropagation();
    const cart=items();let saved=savedCheckout;
    try {saved=JSON.parse(localStorage.getItem('sports-3x-checkout')||'null');}catch(_error){}
    const signature=JSON.stringify(cart);
    if(!saved||saved.signature!==signature)saved={signature,key:crypto.randomUUID()};
    savedCheckout=saved;
    try{localStorage.setItem('sports-3x-checkout',JSON.stringify(saved));}catch(_error){}
    pay(config.dataset.checkout,{items:cart,idempotency_key:saved.key,notes:document.querySelector('[name="notes"]').value},pixButton);
  },true);
  document.querySelectorAll('.installment-pay').forEach(button=>button.addEventListener('click',()=>pay(button.dataset.url,{},button)));
  if(!choice)document.querySelector('#copy-pix').addEventListener('click',async()=>{
    const field=document.querySelector('#pix-payload');
    try {if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(field.value);else{field.select();document.execCommand('copy');}}
    catch(_error){field.focus();field.select();}
  });
  document.querySelector('#pix-modal').addEventListener('hidden.bs.modal',()=>clearTimeout(timer));
})();
