(() => {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js", {scope: "/"})
        .then(registration => registration.update())
        .catch(error => console.warn("PWA indisponível:", error));
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
