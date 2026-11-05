(() => {
  "use strict";

  if (!document.body.classList.contains("gpcta-login")) return;
  if (!document.getElementById("christmas-animations-enabled")) return;

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const root = document.body;
  const assetUrl = "/static/christmas/santa-sleigh-animated.gif";

  let snow = null;
  let sleigh = null;
  let timer = null;

  function random(min, max) {
    return min + Math.random() * (max - min);
  }

  function clearFlight() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function stop() {
    clearFlight();
    if (snow) snow.remove();
    if (sleigh) sleigh.remove();
    snow = null;
    sleigh = null;
  }

  function createSnow() {
    snow = document.createElement("div");
    snow.className = "christmas-snow";
    snow.setAttribute("aria-hidden", "true");

    const count = window.innerWidth < 576 ? 18 : 32;
    const fragment = document.createDocumentFragment();

    for (let i = 0; i < count; i++) {
      const flake = document.createElement("span");
      flake.className = "christmas-flake";

      flake.style.setProperty("--flake-x", `${random(0, 100)}%`);
      flake.style.setProperty("--flake-size", `${random(2, 7)}px`);
      flake.style.setProperty("--flake-duration", `${random(9, 20)}s`);
      flake.style.setProperty("--flake-delay", `${random(-20, 0)}s`);
      flake.style.setProperty("--flake-drift", `${random(-35, 35)}px`);

      fragment.appendChild(flake);
    }

    snow.appendChild(fragment);
    root.prepend(snow);
  }

  function scheduleFlight() {
    clearFlight();
    timer = setTimeout(fly, random(15000, 30000));
  }

  function fly() {
    timer = null;

    if (!sleigh || motion.matches || document.hidden) {
      scheduleFlight();
      return;
    }

    const mobile = window.innerWidth < 576;
    const width = mobile
      ? Math.min(260, window.innerWidth * 0.72)
      : Math.min(430, window.innerWidth * 0.72);

    const height = width * 0.38;
    const viewportHeight = window.innerHeight;

    const startY = random(20, Math.max(25, viewportHeight * 0.32));

    const trajectories = [
      -Math.min(110, viewportHeight * 0.13),
      Math.min(110, viewportHeight * 0.13),
      random(-20, 20)
    ];

    const deltaY = trajectories[Math.floor(Math.random() * trajectories.length)];
    const endY = Math.max(0, Math.min(
      viewportHeight - height,
      startY + deltaY
    ));

    const duration = random(8, 12);

    sleigh.style.setProperty("--flight-start-x", `${window.innerWidth + 20}px`);
    sleigh.style.setProperty("--flight-end-x", `${-width - 30}px`);
    sleigh.style.setProperty("--flight-start-y", `${startY}px`);
    sleigh.style.setProperty("--flight-end-y", `${endY}px`);
    sleigh.style.setProperty("--flight-duration", `${duration}s`);

    sleigh.classList.remove("is-flying");
    void sleigh.offsetWidth;
    sleigh.classList.add("is-flying");

    clearFlight();
    timer = setTimeout(() => {
      if (sleigh) sleigh.classList.remove("is-flying");
      scheduleFlight();
    }, duration * 1000);
  }

  function start() {
    if (motion.matches || snow || sleigh) return;

    createSnow();

    sleigh = document.createElement("img");
    sleigh.className = "christmas-sleigh";
    sleigh.src = assetUrl;
    sleigh.alt = "";
    sleigh.setAttribute("aria-hidden", "true");
    sleigh.decoding = "async";

    root.appendChild(sleigh);

    timer = setTimeout(fly, 3000);
  }

  function update() {
    if (motion.matches) stop();
    else start();
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearFlight();
      if (sleigh) sleigh.classList.remove("is-flying");
    } else if (sleigh && !motion.matches) {
      timer = setTimeout(fly, 1500);
    }
  });

  update();

  if (motion.addEventListener) {
    motion.addEventListener("change", update);
  } else if (motion.addListener) {
    motion.addListener(update);
  }
})();