(() => {
  let serviceWorkerRegistration;
  let registrationPromise;
  if ("serviceWorker" in navigator) {
    registrationPromise = navigator.serviceWorker.register("/service-worker.js", {scope: "/"})
      .then(registration => { serviceWorkerRegistration = registration; return registration.update().then(() => registration); })
      .catch(error => { console.warn("PWA indisponível:", error); throw error; });
  }

  window.enablePushNotifications = async () => {
    if (!registrationPromise || !("PushManager" in window) || !("Notification" in window)) throw new Error("Este dispositivo não oferece notificações push.");
    const registration = await registrationPromise;
    const keyResponse = await fetch("/notifications/push/public-key");
    const keyData = await keyResponse.json();
    if (!keyData.publicKey) throw new Error("Notificações ainda não configuradas no servidor.");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("Permissão de notificação não concedida.");
    const normalizedKey = keyData.publicKey.replace(/-/g, "+").replace(/_/g, "/") + "===";
    const existing = await registration.pushManager.getSubscription();
    const subscription = existing || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(atob(normalizedKey.slice(0, normalizedKey.length - normalizedKey.length % 4)), c => c.charCodeAt(0))});
    const response = await fetch("/notifications/push/subscribe", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(subscription)});
    if (!response.ok) throw new Error("Não foi possível registrar este dispositivo.");
    return true;
  };

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
