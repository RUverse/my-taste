(() => {
  "use strict";

  // Covers come through MyTaste's cache; one that can't be fetched shows its letter instead.
  document.addEventListener(
    "error",
    (event) => {
      const image = event.target;
      if (!(image instanceof HTMLImageElement) || !image.matches("img[data-cover]")) return;
      const placeholder = document.createElement("div");
      placeholder.className = "poster-placeholder album-placeholder";
      placeholder.setAttribute("aria-hidden", "true");
      const letter = document.createElement("span");
      letter.textContent = (image.closest("[data-title]")?.dataset.title || "?").charAt(0);
      placeholder.append(letter);
      image.replaceWith(placeholder);
    },
    true,
  );

  // In a collection that mixes albums with films, an album's panel takes its cover's average
  // colour. Covers are served by MyTaste, so the browser may read them.
  const tint = (image) => {
    const card = image.closest(".album-card-mixed");
    if (!card || card.style.getPropertyValue("--album-color")) return;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 8;
      canvas.height = 8;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.drawImage(image, 0, 0, 8, 8);
      const data = context.getImageData(0, 0, 8, 8).data;
      const total = [0, 0, 0];
      for (let index = 0; index < data.length; index += 4) {
        total[0] += data[index];
        total[1] += data[index + 1];
        total[2] += data[index + 2];
      }
      const pixels = data.length / 4;
      card.style.setProperty("--album-color", `rgb(${total.map((sum) => Math.round(sum / pixels)).join(", ")})`);
    } catch {
      // An image the browser may not read leaves the panel grey.
    }
  };
  document.querySelectorAll(".album-card-mixed img[data-cover]").forEach((image) => {
    if (image.complete && image.naturalWidth) tint(image);
  });
  document.addEventListener(
    "load",
    (event) => {
      if (event.target instanceof HTMLImageElement && event.target.matches(".album-card-mixed img[data-cover]")) tint(event.target);
    },
    true,
  );

  // Spotify's player loads only when asked for, so opening an album sends nothing to Spotify.
  const showSpotify = (container, id) => {
    if (!container || !/^[0-9A-Za-z]{22}$/.test(id)) return;
    const frame = document.createElement("iframe");
    frame.src = `https://open.spotify.com/embed/album/${id}?utm_source=mytaste`;
    frame.title = "Spotify player";
    frame.loading = "lazy";
    frame.allow = "autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture";
    container.replaceChildren(frame);
    container.hidden = false;
  };
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-album-spotify]");
    if (!button) return;
    const root = button.closest(".album-details");
    showSpotify(root?.querySelector("[data-album-embed]"), button.dataset.albumSpotify);
  });

  // Results load after the page, and again while chart entries are still being matched.
  const results = document.querySelector("[data-music-results]");
  let loadController;
  let polls = 0;
  const loadResults = async () => {
    if (!results) return;
    loadController?.abort();
    const pending = new AbortController();
    loadController = pending;
    results.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(results.dataset.musicLoadUrl || window.location.href, {
        headers: { "X-MyTaste-Fragment": "music-page" },
        cache: "no-store",
        signal: pending.signal,
      });
      const payload = await response.json();
      if (typeof payload.html !== "string") throw new Error("Albums could not be loaded.");
      if (pending.signal.aborted) return;
      results.innerHTML = payload.html;
      document.dispatchEvent(new Event("mytaste:results-ready"));
      if (payload.pending > 0 && polls < 20) {
        polls += 1;
        window.setTimeout(() => {
          // Someone browsing further down keeps their place; the chart fills in on the next visit.
          if (window.scrollY < 200 && !document.querySelector("#album-dialog[open]")) loadResults();
        }, 5000);
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      const alert = document.createElement("div");
      alert.className = "alert";
      alert.setAttribute("role", "alert");
      alert.textContent = "Albums could not be loaded. ";
      const retry = document.createElement("button");
      retry.className = "button button-secondary";
      retry.type = "button";
      retry.dataset.musicRetry = "";
      retry.textContent = "Try again";
      alert.append(retry);
      results.replaceChildren(alert);
    } finally {
      if (!pending.signal.aborted) results.setAttribute("aria-busy", "false");
    }
  };
  results?.addEventListener("click", (event) => {
    const retry = event.target.closest("[data-music-retry]");
    if (!retry) return;
    event.preventDefault();
    if (results.getAttribute("aria-busy") !== "true") loadResults();
  });
  window.addEventListener("pagehide", () => loadController?.abort());
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && results?.getAttribute("aria-busy") === "true") loadResults();
  });
  if (results?.dataset.musicLoadUrl) loadResults();

  const dialog = document.querySelector("#album-dialog");
  if (!dialog) return;
  const title = dialog.querySelector("[data-detail-title]");
  const status = dialog.querySelector("[data-detail-status]");
  const poster = dialog.querySelector("[data-detail-poster]");
  const backdrop = dialog.querySelector("[data-detail-backdrop]");
  const kind = dialog.querySelector("[data-detail-kind]");
  const artists = dialog.querySelector("[data-album-artists]");
  const rating = dialog.querySelector("[data-detail-rating]");
  const genres = dialog.querySelector("[data-detail-genres]");
  const release = dialog.querySelector("[data-album-release]");
  const length = dialog.querySelector("[data-album-length]");
  const tracks = dialog.querySelector("[data-album-tracks]");
  const playGroup = dialog.querySelector("[data-play-group]");
  const primary = dialog.querySelector("[data-album-primary]");
  const services = dialog.querySelector("[data-album-services]");
  const embed = dialog.querySelector("[data-album-embed]");
  const noLinks = dialog.querySelector("[data-album-no-links]");
  const addLink = dialog.querySelector("[data-album-add-link]");
  const musicbrainz = dialog.querySelector("[data-album-musicbrainz]");
  const logos = dialog.querySelector("template[data-service-logos]")?.content;
  const neighbors = Array.from(dialog.querySelectorAll("[data-detail-neighbor]"));
  const motion = window.MyTaste?.posterMotion(dialog, poster, neighbors);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const saveButton = dialog.querySelector("[data-detail-save]");
  const saveMenu = saveButton && window.MyTaste?.createSaveMenu(dialog.querySelector("#album-save-popover"), {
    onRender: (collections) => {
      const saved = collections.filter((collection) => collection.saved);
      saveButton.classList.toggle("is-saved", saved.length > 0);
      window.MyTaste.renderSavedIcons(saveButton.querySelector("[data-saved-icons]"), collections);
      saveButton.querySelector("[data-detail-save-label]").textContent = saved.length ? "Saved" : "Save";
      saveButton.setAttribute("aria-label", saved.length ? `Saved in ${saved.map((collection) => collection.name).join(", ")}` : "Save to a collection");
    },
    beforeNew: () => window.MyTaste.closePopover(saveButton),
  });
  let activeCard;
  let controller;
  let revision = 0;
  let motionRevision = 0;
  let closing = false;

  const httpsURL = (value) => {
    try {
      const url = new URL(value);
      return url.protocol === "https:" ? url.href : null;
    } catch {
      return null;
    }
  };
  const sameOrigin = (value) => typeof value === "string" && value.startsWith("/music/covers/");
  // Mixed collections draw the cover inside a taller sleeve; the flight starts from the cover.
  const coverBox = (card) => card.querySelector(".album-sleeve") || card.querySelector(".poster");
  const coverFrom = (card) => {
    const visual = coverBox(card)?.querySelector("img, .poster-placeholder")?.cloneNode(true);
    if (visual) {
      visual.removeAttribute("loading");
      visual.removeAttribute("srcset");
      return visual;
    }
    const placeholder = document.createElement("div");
    placeholder.className = "poster-placeholder";
    placeholder.textContent = (card.dataset.title || "?").charAt(0);
    return placeholder;
  };
  const logo = (service) => logos?.querySelector(`[data-service="${CSS.escape(service)}"] svg`)?.cloneNode(true);
  const cards = () => Array.from(document.querySelectorAll(".album-card"));
  const neighboringCard = (direction) => {
    const all = cards();
    const index = all.indexOf(activeCard);
    return index < 0 ? null : all[index + direction];
  };
  const renderNeighbors = () =>
    neighbors.forEach((button) => {
      const direction = Number(button.dataset.detailNeighbor);
      const card = neighboringCard(direction);
      button.hidden = !card;
      if (!card) return;
      button.replaceChildren(coverFrom(card));
      button.setAttribute("aria-label", `${direction < 0 ? "Previous" : "Next"} album: ${card.dataset.title}`);
    });
  const renderGenres = (values) =>
    genres.replaceChildren(
      ...values.filter(Boolean).map((value) => {
        const chip = document.createElement("span");
        chip.textContent = value.trim();
        return chip;
      }),
    );
  const renderArtists = (credits) => {
    artists.replaceChildren();
    credits.forEach((credit) => {
      if (credit.url) {
        const link = document.createElement("a");
        link.href = credit.url;
        link.textContent = credit.name;
        artists.append(link);
      } else {
        artists.append(credit.name);
      }
      if (credit.join) artists.append(credit.join);
    });
  };
  const renderTracks = (media) => {
    tracks.replaceChildren();
    media.forEach((medium) => {
      if (media.length > 1 || medium.label) {
        const heading = document.createElement("h3");
        heading.className = "album-medium";
        heading.textContent = medium.label || `Disc ${medium.position}`;
        tracks.append(heading);
      }
      const list = document.createElement("ol");
      list.className = "track-list";
      medium.tracks.forEach((track) => {
        const row = document.createElement("li");
        const number = document.createElement("span");
        number.className = "track-number";
        number.textContent = track.position;
        const name = document.createElement("span");
        name.className = "track-title";
        name.textContent = track.title;
        if (track.artist) {
          const by = document.createElement("small");
          by.textContent = track.artist;
          name.append(by);
        }
        const time = document.createElement("span");
        time.className = "track-length";
        time.textContent = track.duration;
        row.append(number, name, time);
        list.append(row);
      });
      tracks.append(list);
    });
  };
  const serviceLink = (link) => {
    const anchor = document.createElement("a");
    anchor.className = "watch-alternative";
    anchor.href = link.url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchor.title = `Listen on ${link.label}`;
    anchor.setAttribute("aria-label", `Listen on ${link.label}`);
    const box = document.createElement("span");
    box.className = "watch-provider-logo";
    const mark = logo(link.service);
    if (mark) box.append(mark);
    anchor.append(box);
    return anchor;
  };
  // Spotify plays in its own player here; any other service opens the album there.
  const renderPlay = (album) => {
    const links = (album.links || []).filter((link) => httpsURL(link.url));
    const spotify = album.spotify_id;
    const first = spotify ? null : links[0];
    primary.replaceChildren();
    primary.removeAttribute("data-album-spotify");
    primary.onclick = null;
    if (spotify || first) {
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = '<path d="M8 5.5v13l10.5-6.5L8 5.5Z"></path>';
      const label = document.createElement("span");
      label.textContent = spotify ? "Play" : `Listen on ${first.label}`;
      const source = document.createElement("span");
      source.className = "play-source";
      source.setAttribute("aria-hidden", "true");
      const box = document.createElement("span");
      box.className = "watch-provider-logo";
      const mark = logo(spotify ? "spotify" : first.service);
      if (mark) box.append(mark);
      source.append(box);
      primary.append(icon, label, source);
      if (spotify) {
        primary.dataset.albumSpotify = spotify;
        primary.setAttribute("aria-label", "Play on Spotify");
      } else {
        primary.onclick = () => window.open(first.url, "_blank", "noopener,noreferrer");
        primary.setAttribute("aria-label", `Listen on ${first.label}`);
      }
      primary.hidden = false;
    } else {
      primary.hidden = true;
    }
    services.replaceChildren(
      ...links.filter((link) => !(spotify && link.service === "spotify") && link !== first).map(serviceLink),
    );
    playGroup.hidden = !(spotify || first);
    noLinks.hidden = links.length > 0;
  };

  const openAlbum = async (card, { direction = 0, play = false } = {}) => {
    if (closing || !card) return;
    const mbid = card.dataset.detailUrl?.match(/\/album\/([0-9a-f-]{36})\//)?.[1];
    if (!mbid) return;
    const stepping = dialog.open && direction !== 0;
    const arriving = stepping ? neighbors.find((button) => Number(button.dataset.detailNeighbor) === direction) : null;
    const fromRect = (arriving || coverBox(card)).getBoundingClientRect();
    const leavingRect = poster.getBoundingClientRect();
    activeCard?.classList.remove("is-detail-source");
    activeCard = card;
    card.classList.add("is-detail-source");
    if (stepping) card.scrollIntoView({ block: "nearest", behavior: "instant" });
    controller?.abort();
    controller = new AbortController();
    const current = ++revision;
    title.textContent = card.dataset.title;
    poster.replaceChildren(coverFrom(card));
    kind.textContent = [card.querySelector(".meta-year")?.textContent.replace("—", "").trim(), card.querySelector(".meta-type")?.textContent.trim()].filter(Boolean).join(" · ");
    artists.textContent = card.querySelector(".album-artist")?.textContent || "";
    rating.textContent = "";
    renderGenres((card.querySelector(".meta-genres")?.textContent || "").split("·"));
    release.hidden = true;
    length.textContent = "";
    tracks.replaceChildren();
    embed.replaceChildren();
    embed.hidden = true;
    playGroup.hidden = true;
    noLinks.hidden = true;
    musicbrainz.href = `https://musicbrainz.org/release-group/${mbid}`;
    addLink.href = musicbrainz.href;
    backdrop.onload = null;
    backdrop.removeAttribute("src");
    backdrop.classList.remove("has-image");
    status.textContent = "Loading tracks…";
    dialog.querySelector("[data-detail-content]").hidden = false;
    if (saveMenu) {
      window.MyTaste.closePopover(saveButton);
      saveMenu.load(`album/${mbid}`);
    }
    renderNeighbors();
    if (!dialog.open) {
      dialog.classList.remove("is-closing", "is-visible", "is-poster-fading");
      dialog.showModal();
    }
    dialog.scrollTop = 0;
    document.body.classList.add("media-details-open");
    const flightRevision = ++motionRevision;
    const flight = motion?.fly(fromRect);
    dialog.classList.add("is-visible");
    motion?.settle(flight, () => flightRevision === motionRevision);
    if (stepping) motion?.step(direction, arriving, leavingRect);
    try {
      const response = await fetch(`/api/items/album/${mbid}/details`, { signal: controller.signal, cache: "no-store" });
      const album = await response.json();
      if (!response.ok) throw new Error(album.error || "Album details are unavailable.");
      if (current !== revision || !dialog.open) return;
      title.textContent = album.title;
      kind.textContent = [album.year, album.type_label].filter(Boolean).join(" · ");
      renderArtists(album.artist_links || []);
      rating.textContent = album.listens ? `${album.listens_label} listens on ListenBrainz` : "";
      renderGenres(album.genres || []);
      release.textContent = album.label || "";
      release.hidden = !album.label;
      length.textContent = [album.track_count ? `${album.track_count} tracks` : "", album.length_label].filter(Boolean).join(" · ");
      renderTracks(album.media || []);
      renderPlay(album);
      if (sameOrigin(album.cover)) {
        backdrop.onload = () => {
          if (current === revision && dialog.open) backdrop.classList.add("has-image");
        };
        backdrop.src = album.cover;
        // A sharper cover replaces the card's once it has loaded.
        const sharper = new Image();
        sharper.onload = () => {
          if (current !== revision) return;
          sharper.alt = "";
          poster.replaceChildren(sharper);
        };
        sharper.src = album.cover;
      }
      status.textContent = album.media?.length ? "" : "MusicBrainz has no tracklist for this album yet.";
      if (play && album.spotify_id) showSpotify(embed, album.spotify_id);
      else if (play && album.links?.length) embed.scrollIntoView?.({ block: "nearest" });
    } catch (error) {
      if (error.name === "AbortError" || current !== revision) return;
      status.textContent = `${error.message || "Album details could not be loaded."} `;
      const retry = document.createElement("button");
      retry.className = "detail-link";
      retry.type = "button";
      retry.textContent = "Try again";
      retry.addEventListener("click", () => openAlbum(activeCard));
      status.append(retry);
    }
  };

  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-album-open], [data-album-play]");
    if (!target || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const card = target.closest(".album-card");
    if (!card) return;
    event.preventDefault();
    openAlbum(card, { play: target.matches("[data-album-play]") });
  });
  neighbors.forEach((button) =>
    button.addEventListener("click", () => {
      const direction = Number(button.dataset.detailNeighbor);
      openAlbum(neighboringCard(direction), { direction });
    }),
  );
  dialog.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.target.closest("input, textarea, select, [contenteditable=true]")) return;
    const direction = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
    const button = neighbors.find((neighbor) => Number(neighbor.dataset.detailNeighbor) === direction);
    if (button && !button.hidden) {
      event.preventDefault();
      button.click();
    }
  });
  const closeAlbum = async () => {
    if (closing || !dialog.open) return;
    closing = true;
    ++revision;
    controller?.abort();
    // Closing stops Spotify's player with the dialog.
    embed.replaceChildren();
    embed.hidden = true;
    const flightRevision = ++motionRevision;
    const destination = activeCard && coverBox(activeCard)?.getBoundingClientRect();
    const visible = destination && destination.bottom > 0 && destination.top < window.innerHeight;
    dialog.classList.add("is-closing");
    dialog.classList.remove("is-visible");
    const flight = visible ? motion?.fly(destination, { reverse: true }) : null;
    if (flight) {
      try {
        await flight.finished;
      } catch {
        // Superseded by another flight.
      }
    } else {
      dialog.classList.add("is-poster-fading");
      await new Promise((resolve) => window.setTimeout(resolve, reducedMotion.matches ? 0 : 340));
    }
    if (flightRevision !== motionRevision) return;
    dialog.close();
    flight?.cancel();
  };
  dialog.querySelector("[data-album-close]")?.addEventListener("click", closeAlbum);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeAlbum();
  });
  dialog.addEventListener("close", () => {
    ++revision;
    controller?.abort();
    backdrop.onload = null;
    closing = false;
    embed.replaceChildren();
    dialog.classList.remove("is-visible", "is-closing", "is-poster-fading");
    activeCard?.classList.remove("is-detail-source");
    const card = activeCard;
    activeCard = null;
    document.body.classList.remove("media-details-open");
    card?.querySelector("[data-album-open]")?.focus();
    window.MyTaste?.reloadIfCollectionChanged();
  });
})();
