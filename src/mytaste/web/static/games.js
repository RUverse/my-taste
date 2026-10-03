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
  const title = dialog.querySelector("[data-detail-title]");
  const status = dialog.querySelector("[data-detail-status]");
  const poster = dialog.querySelector("[data-detail-poster]");
  const backdrop = dialog.querySelector("[data-detail-backdrop]");
  const store = dialog.querySelector("[data-game-store]");
  const playGroup = dialog.querySelector("[data-play-group]");
  const genres = dialog.querySelector("[data-detail-genres]");
  const kind = dialog.querySelector("[data-detail-kind]");
  const rating = dialog.querySelector("[data-detail-rating]");
  const credit = dialog.querySelector("[data-detail-credit]");
  const publisher = dialog.querySelector("[data-game-publisher]");
  const overview = dialog.querySelector("[data-detail-overview]");
  const shots = dialog.querySelector("[data-game-screenshots]");
  const neighbors = Array.from(dialog.querySelectorAll("[data-detail-neighbor]"));
  let origin;
  let controller;
  let revision = 0;
  let closing = false;
  const httpsURL = (value) => {
    try {
      const url = new URL(value);
      return url.protocol === "https:" ? url.href : null;
    } catch { return null; }
  };
  const coverFrom = (card) => {
    const visual = card.querySelector(".poster img, .poster-placeholder")?.cloneNode(true);
    if (visual) {
      visual.removeAttribute("loading");
      return visual;
    }
    const placeholder = document.createElement("div");
    placeholder.className = "poster-placeholder";
    placeholder.textContent = card.querySelector("h2").textContent.trim().charAt(0);
    return placeholder;
  };
  const renderGenres = (values) => {
    genres.replaceChildren(...values.filter(Boolean).map((value) => {
      const chip = document.createElement("span");
      chip.textContent = value.trim();
      return chip;
    }));
  };
  const setDeveloper = (names) => {
    credit.hidden = !names;
    credit.title = names ? `Developer: ${names}` : "";
    credit.querySelector("[data-detail-credit-label]").textContent = "Developer:";
    credit.querySelector("[data-detail-credit-names]").textContent = names;
  };
  const neighboringCard = (direction) => {
    const cards = Array.from(document.querySelectorAll(".game-card"));
    const index = cards.indexOf(origin?.closest(".game-card"));
    return index < 0 ? null : cards[index + direction];
  };
  const renderNeighbors = () => neighbors.forEach((button) => {
    const direction = Number(button.dataset.detailNeighbor);
    const card = neighboringCard(direction);
    button.hidden = !card;
    if (!card) return;
    button.replaceChildren(coverFrom(card));
    button.setAttribute("aria-label", `${direction < 0 ? "Previous" : "Next"} game: ${card.querySelector("h2").textContent}`);
  });
  const openGame = async (link) => {
    if (closing) return;
    const card = link.closest(".game-card");
    if (!card) return;
    origin = link;
    controller?.abort();
    controller = new AbortController();
    const current = ++revision;
    title.textContent = card.querySelector("h2").textContent;
    poster.replaceChildren(coverFrom(card));
    kind.textContent = [card.querySelector(".meta-year")?.textContent.replace("—", "").trim(), "Game"].filter(Boolean).join(" · ");
    rating.textContent = card.querySelector(".meta-rating") ? `Store ${card.querySelector(".meta-rating").textContent.trim()}` : "";
    renderGenres((card.querySelector(".meta-genres")?.textContent || "").split("·"));
    setDeveloper(card.querySelector(".meta-people")?.textContent || "");
    status.textContent = "Loading details…";
    overview.textContent = "";
    publisher.textContent = "";
    publisher.hidden = true;
    backdrop.onload = null;
    backdrop.removeAttribute("src");
    backdrop.classList.remove("has-image");
    playGroup.hidden = true;
    store.removeAttribute("href");
    shots.replaceChildren();
    dialog.querySelector("[data-detail-content]").hidden = false;
    renderNeighbors();
    dialog.scrollTop = 0;
    if (!dialog.open) dialog.showModal();
    document.body.classList.add("media-details-open");
    requestAnimationFrame(() => {
      if (dialog.open && current === revision) dialog.classList.add("is-visible");
    });
    try {
      const response = await fetch(`/api/games/${encodeURIComponent(link.dataset.gameOpen)}/details`, {signal: controller.signal, cache: "no-store"});
      const game = await response.json();
      if (!response.ok) throw new Error(game.error || "Game details are unavailable.");
      if (current !== revision || !dialog.open) return;
      title.textContent = game.title;
      kind.textContent = [game.release_date?.slice(0, 4), "Game"].filter(Boolean).join(" · ");
      rating.textContent = game.rating !== null ? `Store ${Number(game.rating).toFixed(1)}/5 ★` : "";
      rating.setAttribute("aria-label", game.rating !== null ? `Microsoft Store rating ${game.rating} out of 5` : "No Store rating");
      renderGenres(game.genres || []);
      setDeveloper((game.developers || []).join(", "));
      publisher.textContent = game.publisher ? `Published by ${game.publisher}` : "";
      publisher.hidden = !game.publisher;
      overview.textContent = game.overview || "No description is available.";
      if (httpsURL(game.poster_url)) {
        const image = document.createElement("img");
        image.src = httpsURL(game.poster_url);
        image.alt = "";
        poster.replaceChildren(image);
      }
      const background = (game.screenshots || []).find(httpsURL);
      if (background) {
        backdrop.onload = () => {
          if (current === revision && dialog.open) backdrop.classList.add("has-image");
        };
        backdrop.src = httpsURL(background);
      }
      if (httpsURL(game.store_url)) {
        store.href = httpsURL(game.store_url);
        playGroup.hidden = false;
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
      if (error.name !== "AbortError" && current === revision) {
        status.textContent = `${error.message || "Game details could not be loaded."} `;
        const retry = document.createElement("button");
        retry.className = "detail-link";
        retry.type = "button";
        retry.textContent = "Try again";
        retry.addEventListener("click", () => openGame(origin));
        status.append(retry);
      }
    }
  };
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-game-open]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    openGame(link);
  });
  neighbors.forEach((button) => button.addEventListener("click", () => {
    const card = neighboringCard(Number(button.dataset.detailNeighbor));
    if (card) openGame(card.querySelector("[data-game-open]"));
  }));
  dialog.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.target.closest("input, textarea, select, [contenteditable=true]")) return;
    const direction = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
    const button = neighbors.find((neighbor) => Number(neighbor.dataset.detailNeighbor) === direction);
    if (button && !button.hidden) {
      event.preventDefault();
      button.click();
    }
  });
  const closeGame = () => {
    if (closing || !dialog.open) return;
    closing = true;
    ++revision;
    controller?.abort();
    dialog.classList.add("is-closing");
    dialog.classList.remove("is-visible");
    window.setTimeout(() => dialog.close(), window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 340);
  };
  dialog.querySelector("[data-game-close]").addEventListener("click", closeGame);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeGame(); });
  dialog.addEventListener("close", () => {
    ++revision;
    controller?.abort();
    backdrop.onload = null;
    closing = false;
    dialog.classList.remove("is-visible", "is-closing");
    document.body.classList.remove("media-details-open");
    origin?.focus();
  });
})();
