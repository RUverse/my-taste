(() => {
  const results = document.querySelector("[data-game-results]");
  let loadController;
  const loadResults = async () => {
    if (!results) return;
    loadController?.abort();
    const pending = new AbortController();
    loadController = pending;
    results.setAttribute("aria-busy", "true");
    results.querySelector("[data-game-retry]")?.setAttribute("aria-disabled", "true");
    try {
      const response = await fetch(results.dataset.gameLoadUrl || window.location.href, {
        headers: { "X-MyTaste-Fragment": "games-page" },
        cache: "no-store",
        signal: pending.signal,
      });
      const payload = await response.json();
      if (typeof payload.html !== "string") throw new Error("Game Pass games could not be loaded.");
      if (pending.signal.aborted) return;
      results.innerHTML = payload.html;
      const genre = document.querySelector("#game-genre");
      if (genre) {
        const selected = genre.value;
        const options = Array.from(new Set([...(payload.genres || []), selected].filter(Boolean)));
        genre.replaceChildren(new Option("All genres", ""), ...options.map((value) => new Option(value, value)));
        genre.value = selected;
        genre.removeAttribute("aria-busy");
      }
      document.dispatchEvent(new Event("mytaste:results-ready"));
    } catch (error) {
      if (error.name === "AbortError") return;
      const alert = document.createElement("div");
      alert.className = "alert";
      alert.setAttribute("role", "alert");
      alert.textContent = "Game Pass games could not be loaded. ";
      const retry = document.createElement("button");
      retry.className = "button button-secondary";
      retry.type = "button";
      retry.dataset.gameRetry = "";
      retry.textContent = "Try again";
      alert.append(retry);
      results.replaceChildren(alert);
    } finally {
      if (!pending.signal.aborted) results.setAttribute("aria-busy", "false");
    }
  };
  results?.addEventListener("click", (event) => {
    const retry = event.target.closest("[data-game-retry]");
    if (!retry) return;
    event.preventDefault();
    if (results.getAttribute("aria-busy") !== "true") loadResults();
  });
  window.addEventListener("pagehide", () => loadController?.abort());
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && results?.getAttribute("aria-busy") === "true") loadResults();
  });
  if (results?.dataset.gameLoadUrl) loadResults();

  const dialog = document.querySelector("#game-dialog");
  if (!dialog) return;
  const title = dialog.querySelector("[data-game-title]");
  const status = dialog.querySelector("[data-game-status]");
  const cover = dialog.querySelector("[data-game-cover]");
  const store = dialog.querySelector("[data-game-store]");
  const shots = dialog.querySelector("[data-game-screenshots]");
  let origin;
  let controller;
  let revision = 0;
  const httpsURL = (value) => {
    try {
      const url = new URL(value);
      return url.protocol === "https:" ? url.href : null;
    } catch { return null; }
  };
  document.addEventListener("click", async (event) => {
    const link = event.target.closest("[data-game-open]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    origin = link;
    controller?.abort();
    controller = new AbortController();
    const current = ++revision;
    title.textContent = link.closest(".game-card").querySelector("h2").textContent;
    status.textContent = "Loading details…";
    dialog.querySelector("[data-game-meta]").textContent = "";
    dialog.querySelector("[data-game-credits]").textContent = "";
    dialog.querySelector("[data-game-overview]").textContent = "";
    cover.hidden = true;
    cover.removeAttribute("src");
    store.hidden = true;
    store.removeAttribute("href");
    shots.replaceChildren();
    if (!dialog.open) dialog.showModal();
    document.body.classList.add("dialog-open");
    try {
      const response = await fetch(`/api/games/${encodeURIComponent(link.dataset.gameOpen)}/details`, {signal: controller.signal, cache: "no-store"});
      const game = await response.json();
      if (!response.ok) throw new Error(game.error || "Game details are unavailable.");
      if (current !== revision || !dialog.open) return;
      title.textContent = game.title;
      const parts = [game.release_date?.slice(0, 4), ...(game.genres || [])].filter(Boolean);
      if (game.rating !== null) parts.push(`Store ${Number(game.rating).toFixed(1)}/5`);
      dialog.querySelector("[data-game-meta]").textContent = parts.join(" · ");
      dialog.querySelector("[data-game-credits]").textContent = [...(game.developers || []), game.publisher ? `Published by ${game.publisher}` : ""].filter(Boolean).join(" · ");
      dialog.querySelector("[data-game-overview]").textContent = game.overview || "No description is available.";
      if (httpsURL(game.poster_url)) {
        cover.src = httpsURL(game.poster_url);
        cover.hidden = false;
      }
      if (httpsURL(game.store_url)) {
        store.href = httpsURL(game.store_url);
        store.hidden = false;
      }
      (game.screenshots || []).forEach((url, index) => {
        if (!httpsURL(url)) return;
        const image = document.createElement("img");
        image.src = httpsURL(url);
        image.alt = `Screenshot ${index + 1} from ${game.title}`;
        image.loading = "lazy";
        shots.append(image);
      });
      status.textContent = "";
    } catch (error) {
      if (error.name !== "AbortError" && current === revision) status.textContent = error.message || "Game details could not be loaded. Close and try again.";
    }
  });
  dialog.querySelector("[data-game-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    ++revision;
    controller?.abort();
    document.body.classList.remove("dialog-open");
    origin?.focus();
  });
})();
