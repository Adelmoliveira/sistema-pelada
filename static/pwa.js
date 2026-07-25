(() => {
  let serviceWorkerRegistration;
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js", {scope: "/"})
        .then(registration => { serviceWorkerRegistration = registration; return registration.update(); })
        .catch(error => console.warn("PWA indisponível:", error));
    });
  }

  window.enablePushNotifications = async () => {
    if (!serviceWorkerRegistration || !("PushManager" in window) || !("Notification" in window)) throw new Error("Este dispositivo não oferece notificações push.");
    const keyResponse = await fetch("/notifications/push/public-key");
    const keyData = await keyResponse.json();
    if (!keyData.publicKey) throw new Error("Notificações ainda não configuradas no servidor.");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("Permissão de notificação não concedida.");
    const normalizedKey = keyData.publicKey.replace(/-/g, "+").replace(/_/g, "/") + "===";
    const subscription = await serviceWorkerRegistration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(atob(normalizedKey.slice(0, normalizedKey.length - normalizedKey.length % 4)), c => c.charCodeAt(0))});
    const response = await fetch("/notifications/push/subscribe", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(subscription)});
    if (!response.ok) throw new Error("Não foi possível registrar este dispositivo.");
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
