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

  const syncPendingDelivery = async () => {
    if (!document.querySelector("#pending-delivery-bell") && !document.querySelector("#sidebar-pending-delivery-link")) return;
    try {
      const response = await fetch("/minhas-compras/pending-count", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const data = await response.json();
      updatePendingDelivery(data.count);
    } catch (_) {}
  };
  syncPendingDelivery();
  window.setInterval(syncPendingDelivery, 20000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) syncPendingDelivery(); });
  let serviceWorkerRegistration;
  let registrationPromise;
  if ("serviceWorker" in navigator) {
    registrationPromise = navigator.serviceWorker.register("/service-worker.js", {scope: "/"})
      .then(registration => { serviceWorkerRegistration = registration; return registration.update().then(() => registration); })
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

  const registerSubscription = subscription => fetch("/notifications/push/subscribe", {
    method: "POST",
    credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(subscription)
  });

  window.enablePushNotifications = async () => {
    if (!registrationPromise || !("PushManager" in window) || !("Notification" in window)) throw new Error("Este dispositivo não oferece notificações push.");
    const registration = await registrationPromise;
    const keyResponse = await fetch("/notifications/push/public-key");
    const keyData = await keyResponse.json();
    if (!keyData.publicKey) throw new Error("Notificações ainda não configuradas no servidor.");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("Permissão de notificação não concedida.");
    const applicationServerKey = publicKeyBytes(keyData.publicKey);
    let subscription = await registration.pushManager.getSubscription();
    if (subscription && !sameKey(subscription, applicationServerKey)) {
      await subscription.unsubscribe();
      subscription = null;
    }
    subscription = subscription || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey});
    const response = await registerSubscription(subscription);
    if (!response.ok) throw new Error("Não foi possível registrar este dispositivo.");
    return true;
  };

  // Recupera assinaturas perdidas após migração/recriação do banco.
  // Também renova automaticamente quando a chave VAPID foi alterada.
  if (registrationPromise && "Notification" in window && Notification.permission === "granted") {
    window.enablePushNotifications().catch(error => console.warn("Não foi possível sincronizar o push:", error));
  }

  window.disablePushNotifications = async () => {
    if (!registrationPromise || !("PushManager" in window)) throw new Error("Este dispositivo não oferece notificações push.");
    const registration = await registrationPromise;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) return true;
    const response = await fetch("/notifications/push/unsubscribe", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({endpoint: subscription.endpoint})});
    if (!response.ok) throw new Error("Não foi possível desativar as notificações.");
    await subscription.unsubscribe();
    return true;
  };

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
  const dismissedAt = Number(localStorage.getItem("pwa-install-dismissed") || 0);
  const recentlyDismissed = Date.now() - dismissedAt < 7 * 24 * 60 * 60 * 1000;
  let installPrompt;

  if (standalone || recentlyDismissed) return;

  const showPanel = () => { panel.hidden = false; };
  const hidePanel = () => { panel.hidden = true; };

  close.addEventListener("click", () => {
    localStorage.setItem("pwa-install-dismissed", String(Date.now()));
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
