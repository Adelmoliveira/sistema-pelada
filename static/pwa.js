(() => {
  const updateNotificationBell = count => {
    const bell = document.querySelector("#notification-bell");
    const badge = document.querySelector("#notification-bell-count");
    if (!bell || !badge) return;
    const total = Math.max(0, Number(count) || 0);
    bell.classList.toggle("has-notifications", total > 0);
    bell.setAttribute("aria-label", total > 0 ? `Avisos: ${total} não lido(s)` : "Avisos");
    badge.textContent = total > 99 ? "99+" : String(total || "");
    badge.hidden = total === 0;
  };

  const syncAppBadge = async () => {
    try {
      const response = await fetch("/notifications/push/unread-count", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const data = await response.json();
      updateNotificationBell(data.count);
      if (Number(data.count) > 0 && "setAppBadge" in navigator) await navigator.setAppBadge(Number(data.count));
      else if ("clearAppBadge" in navigator) await navigator.clearAppBadge();
    } catch (_) {}
  };
  window.syncAppBadge = syncAppBadge;
  syncAppBadge();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) syncAppBadge(); });

  const updatePendingDelivery = count => {
    const total = Math.max(0, Number(count) || 0);
    const bell = document.querySelector("#pending-delivery-bell");
    const badge = document.querySelector("#pending-delivery-count");
    const menu = document.querySelector("#sidebar-pending-delivery-link");
    const menuBadge = document.querySelector("#sidebar-pending-delivery-count");
    [bell, menu].forEach(element => { if (element) element.hidden = total === 0; });
    [badge, menuBadge].forEach(element => {
      if (!element) return;
      element.hidden = total === 0;
      element.textContent = total > 99 ? "99+" : String(total || "");
    });
    if (bell) {
      bell.classList.toggle("has-pending-delivery", total > 0);
      bell.setAttribute("aria-label", total > 0 ? `Aguardando retirada: ${total} item(ns)` : "Aguardando retirada");
      bell.title = total > 0 ? `${total} item(ns) aguardando retirada` : "Aguardando retirada";
    }
  };

  const DELIVERY_POLL_INTERVAL_MS = 60000;
  const deliveryPollingState = window.__gpctaDeliveryPolling || { timer: null, requestInFlight: false };
  if (deliveryPollingState.timer !== null) window.clearInterval(deliveryPollingState.timer);
  deliveryPollingState.timer = null;
  window.__gpctaDeliveryPolling = deliveryPollingState;
  const hasPendingDeliveryIndicator = () => Boolean(
    document.querySelector("#pending-delivery-bell") || document.querySelector("#sidebar-pending-delivery-link")
  );

  const syncPendingDelivery = async () => {
    if (document.visibilityState !== "visible" || deliveryPollingState.requestInFlight) return;
    if (!hasPendingDeliveryIndicator()) return;
    deliveryPollingState.requestInFlight = true;
    try {
      const response = await fetch("/minhas-compras/pending-count", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const data = await response.json();
      updatePendingDelivery(data.count);
    } catch (_) {
      // Preserve the last known counter until the next scheduled refresh.
    } finally {
      deliveryPollingState.requestInFlight = false;
    }
  };

  const stopDeliveryPolling = () => {
    if (deliveryPollingState.timer === null) return;
    window.clearInterval(deliveryPollingState.timer);
    deliveryPollingState.timer = null;
  };
  const startDeliveryPolling = () => {
    stopDeliveryPolling();
    if (document.visibilityState !== "visible" || !hasPendingDeliveryIndicator()) return;
    deliveryPollingState.timer = window.setInterval(syncPendingDelivery, DELIVERY_POLL_INTERVAL_MS);
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") {
      stopDeliveryPolling();
      return;
    }
    syncPendingDelivery();
    startDeliveryPolling();
  });
  if (document.visibilityState === "visible") {
    syncPendingDelivery();
    startDeliveryPolling();
  }
  let serviceWorkerRegistration;
  let registrationPromise;
  if ("serviceWorker" in navigator) {
    registrationPromise = navigator.serviceWorker.register("/service-worker.js", {scope: "/"})
      .then(registration => { serviceWorkerRegistration = registration;
        if (window.gpctaPwaUpdates) window.gpctaPwaUpdates.watch(registration);
        return registration.update().catch(() => undefined).then(() => registration); })
      .catch(error => { console.warn("PWA indisponível:", error); throw error; });
    navigator.serviceWorker.addEventListener("message", event => {
      if (event.data && event.data.type === "notification-count") {
        updateNotificationBell(event.data.count);
        syncAppBadge();
      }
    });
  }

  const publicKeyBytes = key => {
    const normalized = key.replace(/-/g, "+").replace(/_/g, "/") + "===";
    return Uint8Array.from(atob(normalized.slice(0, normalized.length - normalized.length % 4)), c => c.charCodeAt(0));
  };

  const sameKey = (subscription, expected) => {
    const current = subscription && subscription.options && subscription.options.applicationServerKey;
    if (!current) return true;
    const bytes = new Uint8Array(current);
    return bytes.length === expected.length && bytes.every((value, index) => value === expected[index]);
  };

  const managerPush = document.body.dataset.pushRole === "manager";
  let managerPushEpoch=0;
  if (managerPush) document.addEventListener('submit',event=>{
    if (event.target.getAttribute && event.target.getAttribute('action') === '/logout') managerPushEpoch++;
  },true);
  const pushRoot = managerPush ? "/notifications/manager-push" : "/notifications/push";
  const pushHeaders = () => ({"Content-Type":"application/json", "X-CSRFToken":document.querySelector('meta[name="csrf-token"]')?.content || ""});
  const registerSubscription = subscription => fetch(pushRoot + "/subscribe", {
    method: "POST",
    credentials: "same-origin",
    headers: pushHeaders(),
    body: JSON.stringify(managerPush ? {...subscription.toJSON(),consent:true} : subscription)
  });

  window.enablePushNotifications = async () => {
    if (!registrationPromise || !("PushManager" in window) || !("Notification" in window)) throw new Error("Este dispositivo não oferece notificações push.");
    const expectedEpoch=managerPushEpoch;
    const ensureCurrent=()=>{if(managerPush && expectedEpoch!==managerPushEpoch)throw new Error('Consentimento cancelado pelo logout.');};
    const registration = await registrationPromise;
    ensureCurrent();
    const permission = await Notification.requestPermission();
    ensureCurrent();
    if (permission !== "granted") throw new Error("As notificações estão bloqueadas nas configurações deste navegador.");
    const keyResponse = managerPush
      ? await fetch(pushRoot + "/prepare",{method:"POST",headers:pushHeaders(),body:"{}"})
      : await fetch(pushRoot + "/public-key");
    ensureCurrent();
    if (!keyResponse.ok || keyResponse.redirected) throw new Error("Não foi possível preparar o consentimento. Recarregue a página e tente novamente.");
    const keyData = await keyResponse.json();
    ensureCurrent();
    if (!keyData.publicKey) throw new Error("Notificações ainda não configuradas no servidor.");
    const applicationServerKey = publicKeyBytes(keyData.publicKey);
    let subscription = await registration.pushManager.getSubscription();
    ensureCurrent();
    if (subscription && (keyData.forceRenew === true || !sameKey(subscription, applicationServerKey))) {
      await subscription.unsubscribe();
      subscription = null;
    }
    subscription = subscription || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey});
    ensureCurrent();
    const response = await registerSubscription(subscription);
    ensureCurrent();
    if (!response.ok || response.redirected) throw new Error("Não foi possível registrar este dispositivo.");
    return true;
  };

  // Recupera assinaturas perdidas após migração/recriação do banco.
  // Também renova automaticamente quando a chave VAPID foi alterada.
  if (!managerPush && document.body.dataset.pushRole === "client" && registrationPromise && "Notification" in window && Notification.permission === "granted") {
    window.enablePushNotifications().catch(error => console.warn("Não foi possível sincronizar o push:", error));
  }

  window.disablePushNotifications = async () => {
    if (!registrationPromise || !("PushManager" in window)) throw new Error("Este dispositivo não oferece notificações push.");
    const registration = await registrationPromise;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription && !managerPush) return true;
    const response = await fetch(pushRoot + "/unsubscribe", {method: "POST", headers: pushHeaders(), body: JSON.stringify({endpoint: subscription?.endpoint})});
    if (!response.ok || response.redirected) throw new Error("Não foi possível desativar as notificações.");
    if (subscription) await subscription.unsubscribe();
    return true;
  };

  if (managerPush) {
    const status=document.getElementById('manager-push-status');
    const enable=document.getElementById('manager-enable-push');
    const disable=document.getElementById('manager-disable-push');
    const consent=document.getElementById('manager-push-consent');
    const consentGroup=document.getElementById('manager-push-consent-group');
    const setActive=active=>{
      if (!enable || !disable || !consent || !consentGroup) return;
      consentGroup.hidden=active;
      enable.hidden=active;
      disable.hidden=!active;
      consent.checked=false;
      consent.disabled=active;
      enable.disabled=true;
    };
    const friendlyError=error=>{
      if (error && error.name === 'TypeError') return 'Não foi possível concluir a operação. Verifique a conexão e tente novamente.';
      if (error && error.message) return error.message;
      return 'Não foi possível concluir a operação. Verifique a conexão e tente novamente.';
    };
    if (consent && enable) consent.addEventListener('change',()=>{enable.disabled=!consent.checked;});
    if (enable) enable.addEventListener('click',async()=>{
      if (!consent || !consent.checked) return;
      enable.disabled=true;
      status.textContent='Ativando notificações…';
      try {
        await window.enablePushNotifications();
        setActive(true);
        status.textContent='Notificações ativas neste dispositivo';
      } catch(error) {
        status.textContent=friendlyError(error);
        enable.disabled=!consent.checked;
      }
    });
    if (disable) disable.addEventListener('click',async()=>{
      disable.disabled=true;
      status.textContent='Desativando notificações…';
      try {
        await window.disablePushNotifications();
        setActive(false);
        consent.disabled=false;
        status.textContent='Notificações desativadas neste dispositivo.';
      } catch(error) {
        status.textContent=friendlyError(error);
      } finally { disable.disabled=false; }
    });
    (async()=>{
      if (!status || !consent || !enable || !disable) return;
      if (!registrationPromise || !("PushManager" in window) || !("Notification" in window)) {
        status.textContent='Este navegador não oferece suporte a notificações push.';
        return;
      }
      if (Notification.permission === 'denied') {
        status.textContent='As notificações estão bloqueadas nas configurações deste navegador.';
        return;
      }
      try {
        const registration=await registrationPromise;
        const subscription=await registration.pushManager.getSubscription();
        if (!subscription) {
          consent.disabled=false;
          return;
        }
        const response=await fetch(pushRoot+'/status',{
          method:'POST',credentials:'same-origin',headers:pushHeaders(),
          body:JSON.stringify({endpoint:subscription.endpoint})
        });
        if (!response.ok || response.redirected) throw new Error();
        const data=await response.json();
        if (data.active === true) {
          setActive(true);
          status.textContent='Notificações ativas neste dispositivo';
        } else consent.disabled=false;
      } catch(_) {
        status.textContent='Não foi possível verificar as notificações deste dispositivo. Verifique a conexão e recarregue a página.';
      }
    })();
  }

  const onboarding = document.querySelector("#push-onboarding");
  if (onboarding) {
    const action = onboarding.querySelector("#push-onboarding-action");
    const hideOnboarding = () => onboarding.classList.add("d-none");

    if ("Notification" in window && "PushManager" in window && registrationPromise) {
      registrationPromise.then(registration => registration.pushManager.getSubscription())
        .then(subscription => { if (!subscription) onboarding.classList.remove("d-none"); })
        .catch(() => {});
    }
    action.addEventListener("click", async () => {
      action.disabled = true;
      action.textContent = "Ativando…";
      try {
        await window.enablePushNotifications();
        action.textContent = "Avisos ativados";
        setTimeout(hideOnboarding, 1200);
      } catch (error) {
        action.disabled = false;
        action.textContent = "Ativar avisos no celular";
        alert(error.message || "Não foi possível ativar as notificações.");
      }
    });
  }

  const panel = document.querySelector("#pwa-install");
  if (!panel) return;

  const action = panel.querySelector("#pwa-install-action");
  const close = panel.querySelector("#pwa-install-close");
  const message = panel.querySelector("#pwa-install-message");
  const standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  const isiOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  let dismissedAt = 0;
  try { dismissedAt = Number(localStorage.getItem("pwa-install-dismissed") || 0); } catch (_) {}
  const recentlyDismissed = Date.now() - dismissedAt < 7 * 24 * 60 * 60 * 1000;
  let installPrompt;

  if (standalone || recentlyDismissed) return;

  const showPanel = () => { panel.hidden = false; };
  const hidePanel = () => { panel.hidden = true; };

  close.addEventListener("click", () => {
    try { localStorage.setItem("pwa-install-dismissed", String(Date.now())); } catch (_) {}
    hidePanel();
  });

  if (isiOS) {
    action.textContent = "Como instalar";
    action.addEventListener("click", () => {
      message.textContent = "No Safari, toque em Compartilhar e depois em Adicionar à Tela de Início.";
      action.hidden = true;
    });
    showPanel();
  }

  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    installPrompt = event;
    action.textContent = "Instalar";
    showPanel();
  });

  action.addEventListener("click", async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    const choice = await installPrompt.userChoice;
    installPrompt = null;
    if (choice.outcome === "accepted") hidePanel();
  });

  window.addEventListener("appinstalled", hidePanel);
})();
