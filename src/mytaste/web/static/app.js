(() => {
  "use strict";

  const searchForm = document.querySelector(".search-form");
  const searchToggle = document.querySelector(".search-toggle");
  const searchInput = document.querySelector(".search-input");

  if (searchForm && searchToggle && searchInput) {
    searchToggle.addEventListener("click", () => {
      const willOpen = !searchForm.classList.contains("is-open");
      searchForm.classList.toggle("is-open", willOpen);
      searchToggle.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) {
        searchInput.focus();
      } else if (!searchInput.value) {
        searchToggle.focus();
      }
    });

    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !searchInput.value) {
        searchForm.classList.remove("is-open");
        searchToggle.setAttribute("aria-expanded", "false");
        searchToggle.focus();
      }
    });
  }

  const sidebar = document.querySelector("#browse-sidebar");
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");

  if (sidebar && sidebarToggle) {
    const overlayQuery = window.matchMedia("(max-width: 900px)");

    const sidebarIsOpen = () =>
      overlayQuery.matches
        ? document.body.classList.contains("sidebar-overlay-open")
        : document.body.dataset.sidebar === "open";

    const syncSidebar = () => {
      const open = sidebarIsOpen();
      sidebar.inert = !open;
      sidebarToggle.setAttribute("aria-expanded", String(open));
      sidebarToggle.setAttribute("aria-label", `${open ? "Hide" : "Show"} filters and display options`);
    };

    const setSidebarOpen = (open) => {
      const hadFocus = sidebar.contains(document.activeElement);
      if (overlayQuery.matches) {
        document.body.classList.toggle("sidebar-overlay-open", open);
      } else {
        document.body.dataset.sidebar = open ? "open" : "closed";
        fetch("/api/preferences/display", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ sidebar_open: open }),
        }).catch(() => {});
      }
      syncSidebar();
      if (open && overlayQuery.matches) {
        sidebar.querySelector("[data-sidebar-close]")?.focus();
      } else if (!open && hadFocus) {
        sidebarToggle.focus();
      }
    };

    sidebarToggle.addEventListener("click", () => setSidebarOpen(!sidebarIsOpen()));
    document.querySelectorAll("[data-sidebar-close]").forEach((button) => {
      button.addEventListener("click", () => setSidebarOpen(false));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && overlayQuery.matches && sidebarIsOpen()) {
        setSidebarOpen(false);
      }
    });
    overlayQuery.addEventListener("change", () => {
      document.body.classList.remove("sidebar-overlay-open");
      syncSidebar();
    });
    syncSidebar();
  }

  const markNavigating = () => document.body.classList.add("is-navigating");
  window.addEventListener("pageshow", () => document.body.classList.remove("is-navigating"));
  document.querySelectorAll(".browse-sidebar a, .active-filters a, .category-tabs a").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (!event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) {
        markNavigating();
      }
    });
  });

  const filterForm = document.querySelector("[data-filter-form]");

  if (filterForm) {
    const sourceBoxes = Array.from(filterForm.querySelectorAll('.source-toggle input[type="checkbox"]'));

    filterForm.addEventListener("change", (event) => {
      if (sourceBoxes.includes(event.target) && !sourceBoxes.some((box) => box.checked)) {
        event.target.checked = true;
        return;
      }
      filterForm.requestSubmit();
    });

    filterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const params = new URLSearchParams();
      new FormData(filterForm).forEach((value, key) => {
        if (key !== "providers" && key !== "libraries" && value !== "") {
          params.append(key, value);
        }
      });
      ["providers", "libraries"].forEach((key) => {
        const boxes = sourceBoxes.filter((box) => box.name === key);
        const checked = boxes.filter((box) => box.checked).map((box) => box.value);
        if (boxes.length && checked.length !== boxes.length) {
          params.set(key, checked.length ? checked.join(",") : "none");
        }
      });
      markNavigating();
      window.location.assign(`/?${params}`);
    });
  }

  const detailDialog = document.querySelector("#media-details");
  const detailPoster = detailDialog?.querySelector("[data-detail-poster]");
  const detailTitle = detailDialog?.querySelector("[data-detail-title]");
  const detailKind = detailDialog?.querySelector("[data-detail-kind]");
  const detailYear = detailDialog?.querySelector("[data-detail-year]");
  const detailRuntime = detailDialog?.querySelector("[data-detail-runtime]");
  const detailRating = detailDialog?.querySelector("[data-detail-rating]");
  const detailStatus = detailDialog?.querySelector("[data-detail-status]");
  const detailContent = detailDialog?.querySelector("[data-detail-content]");
  const detailGenres = detailDialog?.querySelector("[data-detail-genres]");
  const detailOverview = detailDialog?.querySelector("[data-detail-overview]");
  const detailCast = detailDialog?.querySelector("[data-detail-cast]");
  const detailCastSection = detailDialog?.querySelector(".media-detail-cast-section");
  const detailBackdrop = detailDialog?.querySelector("[data-detail-backdrop]");
  const detailVideo = detailDialog?.querySelector("[data-detail-video]");
  const detailSound = detailDialog?.querySelector("[data-detail-sound]");
  const detailTrailer = detailDialog?.querySelector("[data-detail-trailer]");
  const detailPlay = detailDialog?.querySelector("[data-detail-play]");
  const detailPlayLabel = detailDialog?.querySelector("[data-detail-play-label]");
  const detailWatch = detailDialog?.querySelector("[data-watch]");
  const detailWatchPrimary = detailDialog?.querySelector("[data-watch-primary]");
  const detailWatchProvider = detailDialog?.querySelector("[data-watch-provider]");
  const detailWatchAlternatives = detailDialog?.querySelector("[data-watch-alternatives]");
  const detailEpisodes = detailDialog?.querySelector("[data-detail-episodes]");
  const detailEpisodesSummary = detailDialog?.querySelector("[data-episodes-summary]");
  const detailEpisodesStatus = detailDialog?.querySelector("[data-episodes-status]");
  const detailSeasons = detailDialog?.querySelector("[data-episodes-seasons]");
  const detailCache = new Map();
  let watchUrl = "";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let activeDetailCard;
  let detailRevision = 0;
  let posterMotionRevision = 0;

  const setTrailerMuted = (muted, sendCommand = false) => {
    detailSound.dataset.muted = String(muted);
    detailSound.setAttribute("aria-pressed", String(!muted));
    detailSound.setAttribute("aria-label", muted ? "Unmute trailer" : "Mute trailer");
    detailSound.title = muted ? "Unmute trailer" : "Mute trailer";
    if (sendCommand) {
      detailVideo.querySelector("iframe")?.contentWindow?.postMessage(
        JSON.stringify({ event: "command", func: muted ? "mute" : "unMute", args: [] }),
        "https://www.youtube-nocookie.com",
      );
    }
  };

  const posterVisual = (element) =>
    element?.querySelector("img, .poster-placeholder")?.cloneNode(true);

  const setDetailPoster = (source, fallbackTitle) => {
    const visual = posterVisual(source);
    detailPoster.replaceChildren();
    if (visual) {
      visual.removeAttribute("loading");
      detailPoster.append(visual);
      return;
    }
    const placeholder = document.createElement("div");
    placeholder.className = "poster-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    const initial = document.createElement("span");
    initial.textContent = fallbackTitle.charAt(0) || "?";
    placeholder.append(initial);
    detailPoster.append(placeholder);
  };

  const renderGenres = (genres) => {
    detailGenres.replaceChildren();
    genres.forEach((genre) => {
      const chip = document.createElement("span");
      chip.textContent = genre;
      detailGenres.append(chip);
    });
  };

  const posterShadows = {
    card: "0 14px 34px rgba(0, 0, 0, 0.34)",
    detail: "0 28px 70px rgba(0, 0, 0, 0.62)",
  };

  // FLIP the dialog's own poster between its resting place and a card's poster, so the cover
  // reads as one object moving rather than a copy cross-fading with the original.
  const flyPoster = (cardRect, { reverse = false } = {}) => {
    detailPoster.getAnimations().forEach((animation) => animation.cancel());
    const restRect = detailPoster.getBoundingClientRect();
    if (!cardRect?.width || !restRect.width || reducedMotion.matches || !("animate" in Element.prototype)) {
      return null;
    }
    const scaleX = cardRect.width / restRect.width;
    const scaleY = cardRect.height / restRect.height;
    const atCard = {
      transform: `translate(${cardRect.left - restRect.left}px, ${cardRect.top - restRect.top}px) scale(${scaleX}, ${scaleY})`,
      borderRadius: `${11 / scaleX}px / ${11 / scaleY}px`,
      boxShadow: posterShadows.card,
    };
    const atRest = {
      transform: "translate(0px, 0px) scale(1, 1)",
      borderRadius: "12px / 12px",
      boxShadow: posterShadows.detail,
    };
    return detailPoster.animate(reverse ? [atRest, atCard] : [atCard, atRest], {
      duration: reverse ? 420 : 560,
      easing: reverse ? "cubic-bezier(0.32, 0, 0.18, 1)" : "cubic-bezier(0.16, 1, 0.3, 1)",
      fill: "both",
    });
  };

  const resetDetailContent = (card) => {
    const sourcePoster = card.querySelector(".poster");
    setDetailPoster(sourcePoster, card.dataset.title || "");
    detailTitle.textContent = card.dataset.title || "Loading…";
    detailKind.textContent = card.dataset.mediaLabel || "";
    detailYear.textContent = card.dataset.year === "—" ? "" : card.dataset.year || "";
    detailRuntime.textContent = "";
    const rating = Number(card.dataset.rating || 0);
    detailRating.textContent = rating ? `★ ${rating}` : "";
    detailStatus.textContent = "Loading trailer and cast…";
    let genres = [];
    try {
      const parsedGenres = JSON.parse(card.dataset.genres || "[]");
      genres = Array.isArray(parsedGenres) ? parsedGenres : [];
    } catch {
      genres = [];
    }
    renderGenres(genres);
    detailOverview.textContent = card.dataset.overview || "No description is available yet.";
    detailContent.hidden = false;
    detailCast.replaceChildren();
    detailCastSection.hidden = true;
    detailBackdrop.onload = null;
    detailBackdrop.removeAttribute("src");
    detailBackdrop.classList.remove("has-image");
    detailVideo.replaceChildren();
    detailVideo.classList.remove("is-active");
    detailSound.hidden = true;
    setTrailerMuted(true);
    detailTrailer.hidden = true;
    detailTrailer.removeAttribute("href");
    detailTrailer.classList.add("detail-action-primary");
    setDetailPlay(card.dataset.playUrl, card.dataset.resume ? "Resume" : "Play");
    detailTrailer.classList.toggle("detail-action-primary", detailPlay.hidden);
    detailWatch.hidden = true;
    detailWatchPrimary.classList.toggle("detail-action-primary", detailPlay.hidden);
    detailWatchPrimary.removeAttribute("href");
    detailWatchProvider.replaceChildren();
    detailWatchAlternatives.replaceChildren();
    watchUrl = "";
    const isSeries = card.dataset.detailUrl?.startsWith("/api/items/tv/") ?? false;
    detailDialog.classList.toggle("is-series", isSeries);
    detailEpisodes.hidden = !isSeries;
    detailEpisodesSummary.textContent = "";
    detailEpisodesStatus.textContent = isSeries ? "Loading episodes…" : "";
    detailSeasons.replaceChildren();
  };

  // Local titles play in MyTaste's own player; streaming links stay available beside it.
  const setDetailPlay = (url, label) => {
    detailPlay.hidden = !url;
    if (url) {
      detailPlay.href = url;
      detailPlayLabel.textContent = label;
      detailPlay.setAttribute("aria-label", `${label} from your library`);
    } else {
      detailPlay.removeAttribute("href");
    }
  };

  const providerLogo = (option, className) => {
    const logo = document.createElement("span");
    logo.className = className;
    if (option.logo_url) {
      const image = document.createElement("img");
      image.src = option.logo_url;
      image.alt = "";
      logo.append(image);
    } else {
      logo.textContent = option.name?.charAt(0) || "?";
    }
    return logo;
  };

  const watchTitle = (option) =>
    option.direct ? `Watch on ${option.name}` : `Find ${option.name} offers on TMDB`;

  const renderWatchOptions = (options) => {
    const [primary, ...others] = options.filter((option) => option.url?.startsWith("https://"));
    if (!primary) {
      return;
    }
    watchUrl = primary.url;
    detailWatchPrimary.href = primary.url;
    detailWatchPrimary.title = watchTitle(primary);
    detailWatchPrimary.setAttribute("aria-label", watchTitle(primary));
    detailWatchProvider.replaceChildren(providerLogo(primary, "watch-provider-logo"));
    others.forEach((option) => {
      const link = document.createElement("a");
      link.className = "watch-alternative";
      link.href = option.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = watchTitle(option);
      link.setAttribute("aria-label", watchTitle(option));
      link.append(providerLogo(option, "watch-provider-logo"));
      detailWatchAlternatives.append(link);
    });
    detailWatch.hidden = false;
    detailWatchPrimary.classList.toggle("detail-action-primary", detailPlay.hidden);
    detailTrailer.classList.remove("detail-action-primary");
    detailSeasons.querySelectorAll("[data-episode-link]").forEach(linkEpisode);
  };

  const linkEpisode = (card) => {
    if (!watchUrl || card.dataset.upcoming === "true" || card.dataset.local === "true") {
      return;
    }
    card.href = watchUrl;
    card.target = "_blank";
    card.rel = "noopener noreferrer";
    card.setAttribute("aria-label", card.dataset.label);
  };

  const airDateFormat = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });

  const formatAirDate = (value) => {
    const parsed = /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? new Date(`${value}T00:00:00`) : null;
    return parsed && !Number.isNaN(parsed.getTime()) ? airDateFormat.format(parsed) : "";
  };

  const renderEpisode = (episode, seasonNumber, today) => {
    const item = document.createElement("li");
    const card = document.createElement("a");
    card.className = "episode-card";
    card.dataset.episodeLink = "";
    // Undated regular episodes are announced but not out yet; undated specials are just old.
    const upcoming = episode.air_date ? episode.air_date > today : seasonNumber > 0;
    card.dataset.upcoming = String(upcoming);

    const still = document.createElement("div");
    still.className = "episode-still";
    if (episode.still_url) {
      const image = document.createElement("img");
      image.src = episode.still_url;
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      still.append(image);
    }
    const number = document.createElement("span");
    number.className = "episode-number";
    number.textContent = String(episode.episode_number);
    still.append(number);
    if (episode.watched) {
      const watched = document.createElement("span");
      watched.className = "episode-watched";
      watched.title = "Watched";
      watched.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m6 12.5 4 4 8-9"></path></svg>';
      still.append(watched);
    } else if (episode.progress > 0) {
      const progress = document.createElement("span");
      progress.className = "card-progress";
      const bar = document.createElement("span");
      bar.style.width = `${Math.round(episode.progress * 1000) / 10}%`;
      progress.append(bar);
      still.append(progress);
    }
    if (episode.in_library) {
      const local = document.createElement("span");
      local.className = "episode-local";
      local.title = "In your local library";
      local.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l2 2H19a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 19 18H5a1.5 1.5 0 0 1-1.5-1.5v-9Z"></path></svg>';
      still.append(local);
    }
    const play = document.createElement("span");
    play.className = "episode-play";
    play.setAttribute("aria-hidden", "true");
    play.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5.5v13l10.5-6.5L8 5.5Z"></path></svg>';
    still.append(play);

    const copy = document.createElement("div");
    copy.className = "episode-copy";
    const name = document.createElement("strong");
    name.textContent = episode.name || `Episode ${episode.episode_number}`;
    const meta = document.createElement("small");
    const airDate = formatAirDate(episode.air_date);
    meta.textContent = upcoming
      ? airDate ? `Coming ${airDate}` : "Not yet aired"
      : [runtimeLabel(episode.runtime_minutes), airDate].filter(Boolean).join(" · ");
    copy.append(name, meta);
    if (episode.overview) {
      card.title = episode.overview;
    }
    const label = seasonNumber === 0 ? "Special" : `Season ${seasonNumber}, episode`;
    const context = [
      `${label} ${episode.episode_number}: ${name.textContent}`,
      meta.textContent,
      episode.in_library ? "in your local library" : "",
      episode.watched ? "watched" : "",
    ].filter(Boolean);
    card.dataset.label = context.join(", ");
    card.append(still, copy);
    if (episode.play_url) {
      card.dataset.local = "true";
      card.href = episode.play_url;
      card.setAttribute("aria-label", `Play ${card.dataset.label}`);
    }
    linkEpisode(card);
    item.append(card);
    return item;
  };

  const renderSeasons = (seasons) => {
    const today = new Date().toISOString().slice(0, 10);
    const regular = seasons.filter((season) => season.season_number > 0);
    const episodeCount = regular.reduce((total, season) => total + season.episodes.length, 0);
    const plural = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;
    detailEpisodesSummary.textContent = regular.length
      ? `${plural(regular.length, "season")} · ${plural(episodeCount, "episode")}`
      : "";
    detailSeasons.replaceChildren(
      ...seasons.map((season) => {
        const row = document.createElement("section");
        row.className = "season-row";
        const head = document.createElement("header");
        head.className = "season-row-head";
        const title = document.createElement("h4");
        title.id = `season-${season.season_number}-title`;
        title.textContent = season.name;
        row.setAttribute("aria-labelledby", title.id);
        const meta = document.createElement("span");
        const local = season.episodes.filter((episode) => episode.in_library).length;
        meta.textContent = [
          plural(season.episodes.length, "episode"),
          local ? `${local} in library` : "",
        ].filter(Boolean).join(" · ");
        head.append(title, meta);
        const strip = document.createElement("ol");
        strip.className = "episode-strip";
        strip.append(...season.episodes.map((episode) => renderEpisode(episode, season.season_number, today)));
        row.append(head, strip);
        return row;
      }),
    );
    detailEpisodesStatus.textContent = seasons.length ? "" : "No episode list is available yet.";
  };

  const runtimeLabel = (minutes) => {
    if (!Number.isInteger(minutes) || minutes <= 0) {
      return "";
    }
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return hours ? `${hours}h${remainder ? ` ${remainder}m` : ""}` : `${minutes}m`;
  };

  const renderCast = (people) => {
    detailCast.replaceChildren();
    people.forEach((person) => {
      const item = document.createElement("article");
      item.className = "cast-person";
      item.setAttribute("role", "listitem");
      const portrait = document.createElement("div");
      portrait.className = "cast-person-image";
      if (person.profile_url) {
        const image = document.createElement("img");
        image.src = person.profile_url;
        image.alt = "";
        image.loading = "lazy";
        portrait.append(image);
      } else {
        portrait.textContent = person.name?.charAt(0) || "?";
      }
      const name = document.createElement("strong");
      name.textContent = person.name || "";
      item.append(portrait, name);
      if (person.character) {
        const character = document.createElement("small");
        character.textContent = person.character;
        item.append(character);
      }
      detailCast.append(item);
    });
    detailCastSection.hidden = people.length === 0;
  };

  const renderDetails = (payload) => {
    detailTitle.textContent = payload.title || detailTitle.textContent;
    detailKind.textContent = payload.media_label || detailKind.textContent;
    detailYear.textContent = payload.year === "—" ? "" : payload.year || "";
    detailRuntime.textContent = runtimeLabel(payload.runtime_minutes);
    detailRating.textContent = payload.rating ? `★ ${payload.rating}` : "";
    // The card's cover is usually the same image; swapping it would flash mid-flight.
    if (payload.poster_url && detailPoster.querySelector("img")?.src !== payload.poster_url) {
      const poster = document.createElement("img");
      poster.src = payload.poster_url;
      poster.alt = "";
      detailPoster.replaceChildren(poster);
    }

    renderGenres(Array.isArray(payload.genres) ? payload.genres : []);
    detailOverview.textContent = payload.overview || "No description is available yet.";
    renderCast(Array.isArray(payload.cast) ? payload.cast : []);

    if (payload.backdrop_url) {
      detailBackdrop.onload = () => detailBackdrop.classList.add("has-image");
      detailBackdrop.src = payload.backdrop_url;
      if (detailBackdrop.complete) {
        detailBackdrop.classList.add("has-image");
      }
    }

    const autoplayTrailer = document.body.dataset.autoplayTrailer !== "false";
    if (payload.trailer_key && autoplayTrailer && !reducedMotion.matches) {
      const key = encodeURIComponent(payload.trailer_key);
      const playerParams = new URLSearchParams({
        autoplay: "1",
        mute: "1",
        controls: "0",
        loop: "1",
        playlist: payload.trailer_key,
        playsinline: "1",
        rel: "0",
        modestbranding: "1",
        enablejsapi: "1",
        origin: window.location.origin,
      });
      const iframe = document.createElement("iframe");
      iframe.src = `https://www.youtube-nocookie.com/embed/${key}?${playerParams}`;
      iframe.title = `${payload.title || "Media"} trailer`;
      iframe.tabIndex = -1;
      iframe.loading = "eager";
      iframe.allow = "autoplay; encrypted-media; picture-in-picture";
      iframe.referrerPolicy = "strict-origin-when-cross-origin";
      iframe.addEventListener("load", () => detailVideo.classList.add("is-active"));
      detailVideo.replaceChildren(iframe);
      setTrailerMuted(true);
      detailSound.hidden = false;
      detailTrailer.href = payload.trailer_url;
      detailTrailer.hidden = false;
    } else if (payload.trailer_url) {
      detailTrailer.href = payload.trailer_url;
      detailTrailer.hidden = false;
    }
    detailStatus.textContent = "";
    detailContent.hidden = false;
  };

  const fetchDetails = (url) => {
    if (!detailCache.has(url)) {
      const request = fetch(url, { headers: { Accept: "application/json" } })
        .then(async (response) => {
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || "Details unavailable");
          }
          return payload;
        })
        .catch((error) => {
          detailCache.delete(url);
          throw error;
        });
      detailCache.set(url, request);
    }
    return detailCache.get(url);
  };

  const openMediaDetails = async (card) => {
    if (!detailDialog || detailDialog.open) {
      return;
    }
    activeDetailCard = card;
    const revision = ++detailRevision;
    const motionRevision = ++posterMotionRevision;
    const sourceRect = card.querySelector(".poster").getBoundingClientRect();
    resetDetailContent(card);
    detailDialog.classList.remove("is-closing", "is-visible", "is-poster-fading");
    detailDialog.showModal();
    detailDialog.scrollTop = 0;
    document.body.classList.add("media-details-open");
    card.classList.add("is-detail-source");
    // flyPoster measures layout, so the hidden starting styles are applied before is-visible.
    const flight = flyPoster(sourceRect);
    detailDialog.classList.add("is-visible");
    flight?.finished
      .then(() => {
        if (motionRevision === posterMotionRevision) {
          flight.cancel();
        }
      })
      .catch(() => {
        // A close that starts mid-flight cancels this animation.
      });

    if (!card.dataset.detailUrl) {
      detailStatus.textContent = "This title is not matched on TMDB yet, so extra details are unavailable.";
      return;
    }

    const itemUrl = card.dataset.detailUrl.replace(/\/details$/, "");
    const isCurrent = () => revision === detailRevision && detailDialog.open;
    fetchDetails(`${itemUrl}/watch`)
      .then((payload) => {
        if (isCurrent() && Array.isArray(payload.options)) {
          renderWatchOptions(payload.options);
        }
      })
      .catch(() => {
        // Watch links are optional; the details stay usable without them.
      });
    if (detailDialog.classList.contains("is-series")) {
      fetchDetails(`${itemUrl}/episodes`)
        .then((payload) => {
          if (isCurrent()) {
            renderSeasons(Array.isArray(payload.seasons) ? payload.seasons : []);
            if (payload.next_up?.url) {
              setDetailPlay(
                payload.next_up.url,
                `${payload.next_up.resume ? "Resume" : "Play"} ${payload.next_up.label.replace(" · ", " ")}`,
              );
            }
          }
        })
        .catch(() => {
          if (isCurrent()) {
            detailEpisodesStatus.textContent = "Episodes are unavailable right now.";
          }
        });
    }

    try {
      const payload = await fetchDetails(card.dataset.detailUrl);
      if (revision === detailRevision && detailDialog.open) {
        renderDetails(payload);
      }
    } catch {
      if (revision === detailRevision && detailDialog.open) {
        detailStatus.textContent = "Extra details are unavailable right now.";
      }
    }
  };

  const closeMediaDetails = async () => {
    if (!detailDialog?.open || detailDialog.classList.contains("is-closing")) {
      return;
    }
    ++detailRevision;
    const motionRevision = ++posterMotionRevision;
    detailVideo.replaceChildren();
    detailVideo.classList.remove("is-active");
    detailSound.hidden = true;
    const card = activeDetailCard;
    const destinationRect = card?.querySelector(".poster")?.getBoundingClientRect();
    const destinationVisible =
      destinationRect && destinationRect.bottom > 0 && destinationRect.top < window.innerHeight;
    detailDialog.classList.add("is-closing");
    detailDialog.classList.remove("is-visible");
    const flight = destinationVisible ? flyPoster(destinationRect, { reverse: true }) : null;
    if (flight) {
      try {
        await flight.finished;
      } catch {
        // Superseded by another open or close.
      }
    } else {
      detailDialog.classList.add("is-poster-fading");
      await new Promise((resolve) => window.setTimeout(resolve, 340));
    }
    if (motionRevision !== posterMotionRevision) {
      return;
    }
    detailDialog.close();
    flight?.cancel();
    detailDialog.classList.remove("is-closing", "is-poster-fading");
    card?.classList.remove("is-detail-source");
    document.body.classList.remove("media-details-open");
    card?.querySelector("[data-open-media-details]")?.focus();
  };

  if (detailDialog) {
    document.querySelectorAll("[data-open-media-details]").forEach((button) => {
      button.addEventListener("click", () => openMediaDetails(button.closest(".media-card")));
    });
    detailDialog.querySelector("[data-close-media-details]")?.addEventListener("click", closeMediaDetails);
    detailSound?.addEventListener("click", () => {
      setTrailerMuted(detailSound.dataset.muted !== "true", true);
    });
    detailDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeMediaDetails();
    });
    detailDialog.addEventListener("click", (event) => {
      if (event.target === detailDialog) {
        closeMediaDetails();
      }
    });
  }

  const peopleCards = Array.from(document.querySelectorAll("[data-people-url]"));
  let peopleObserver;

  const loadPeople = async (element) => {
    if (element.dataset.loaded === "true" || element.dataset.loading === "true") {
      return;
    }
    element.dataset.loading = "true";
    try {
      const response = await fetch(element.dataset.peopleUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error("Credits unavailable");
      }
      const payload = await response.json();
      if (Array.isArray(payload.names) && payload.names.length) {
        element.textContent = payload.names.join(", ");
      } else {
        element.textContent = "";
      }
      element.dataset.loaded = "true";
    } catch {
      element.textContent = "";
    } finally {
      delete element.dataset.loading;
    }
  };

  const observePeople = () => {
    if (document.body.dataset.showPeople !== "true") {
      peopleObserver?.disconnect();
      return;
    }
    if (!("IntersectionObserver" in window)) {
      peopleCards.forEach(loadPeople);
      return;
    }
    if (!peopleObserver) {
      peopleObserver = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              loadPeople(entry.target);
              peopleObserver.unobserve(entry.target);
            }
          });
        },
        { rootMargin: "180px" },
      );
    }
    peopleCards
      .filter((element) => element.dataset.loaded !== "true")
      .forEach((element) => peopleObserver.observe(element));
  };

  const providerStrips = Array.from(document.querySelectorAll("[data-providers-key]"));
  const providerQueue = new Set();
  let providerTimer;
  let providerObserver;

  const renderProviders = (strip, providers) => {
    const local = strip.querySelector(".source-local");
    const icons = providers.map((provider) => {
      if (provider.logo_url) {
        const logo = document.createElement("img");
        logo.src = provider.logo_url;
        logo.alt = "";
        logo.width = 24;
        logo.height = 24;
        logo.decoding = "async";
        return logo;
      }
      const letter = document.createElement("span");
      letter.textContent = provider.name.slice(0, 1);
      return letter;
    });
    strip.replaceChildren(...icons, ...(local ? [local] : []));
    const names = providers.map((provider) => provider.name).join(", ");
    const summary = [names && `On ${names}`, local && "In your local library"].filter(Boolean);
    strip.title = summary.join(" · ");
    const text = strip.closest(".media-card")?.querySelector("[data-providers-text]");
    if (text) {
      text.textContent = summary.join(". ");
    }
  };

  const flushProviders = async () => {
    providerTimer = undefined;
    const batch = Array.from(providerQueue);
    providerQueue.clear();
    const keys = Array.from(new Set(batch.map((strip) => strip.dataset.providersKey)));
    try {
      const params = new URLSearchParams({ items: keys.join(",") });
      const response = await fetch(`/api/items/providers?${params}`, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error("Services unavailable");
      }
      const payload = await response.json();
      batch.forEach((strip) => {
        const providers = payload.providers?.[strip.dataset.providersKey];
        if (providers) {
          renderProviders(strip, providers);
          strip.dataset.loaded = "true";
        }
      });
    } catch {
      // Leave the strips empty; they are retried the next time the option is enabled.
    } finally {
      batch.forEach((strip) => delete strip.dataset.loading);
    }
  };

  const queueProviders = (strip) => {
    if (strip.dataset.loaded === "true" || strip.dataset.loading === "true") {
      return;
    }
    strip.dataset.loading = "true";
    providerQueue.add(strip);
    providerTimer ??= window.setTimeout(flushProviders, 40);
  };

  const observeProviders = () => {
    if (document.body.dataset.showProviders !== "true") {
      providerObserver?.disconnect();
      return;
    }
    if (!("IntersectionObserver" in window)) {
      providerStrips.forEach(queueProviders);
      return;
    }
    providerObserver ??= new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            queueProviders(entry.target);
            providerObserver.unobserve(entry.target);
          }
        });
      },
      { rootMargin: "240px" },
    );
    providerStrips
      .filter((strip) => strip.dataset.loaded !== "true")
      .forEach((strip) => providerObserver.observe(strip));
  };

  const displayForm = document.querySelector("#display-form");
  const displayStatus = document.querySelector("#display-save-status");
  let displaySaveQueue = Promise.resolve();
  let displaySaveRevision = 0;
  const displayKeys = [
    "show_year",
    "show_rating",
    "show_media_type",
    "show_genres",
    "show_people",
    "show_providers",
    "autoplay_trailer",
  ];

  const displayPayload = () => {
    const data = new FormData(displayForm);
    return {
      ...Object.fromEntries(
        displayKeys.filter((key) => displayForm.elements[key]).map((key) => [key, data.has(key)]),
      ),
      card_size: data.get("card_size") || "comfortable",
    };
  };

  const applyDisplay = (payload) => {
    displayKeys.filter((key) => key in payload).forEach((key) => {
      const dataKey = key.replace(/_([a-z])/g, (_, value) => value.toUpperCase());
      document.body.dataset[dataKey] = String(payload[key]);
    });
    document.body.dataset.cardSize = payload.card_size;
    if (!payload.autoplay_trailer) {
      detailVideo?.replaceChildren();
      detailVideo?.classList.remove("is-active");
      if (detailSound) {
        detailSound.hidden = true;
      }
    }
    observePeople();
    observeProviders();
  };

  const saveDisplay = (payload) => {
    const revision = ++displaySaveRevision;
    if (displayStatus) {
      displayStatus.textContent = "Saving…";
    }
    displaySaveQueue = displaySaveQueue.then(async () => {
      try {
        const response = await fetch("/api/preferences/display", {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          throw new Error("Could not save display settings");
        }
        if (displayStatus && revision === displaySaveRevision) {
          displayStatus.textContent = "Saved automatically";
        }
      } catch {
        if (displayStatus && revision === displaySaveRevision) {
          displayStatus.textContent = "Could not save. Try changing the option again.";
        }
      }
    });
  };

  if (displayForm) {
    displayForm.addEventListener("change", () => {
      const payload = displayPayload();
      applyDisplay(payload);
      saveDisplay(payload);
    });
  }

  observePeople();
  observeProviders();

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    });
  });

  const libraryRows = Array.from(document.querySelectorAll("[data-library-id]"));
  let libraryPollTimer;

  const applyLibraryStatus = (payload) => {
    const byId = new Map(payload.map((library) => [String(library.id), library]));
    let scanning = false;
    libraryRows.forEach((row) => {
      const library = byId.get(row.dataset.libraryId);
      if (!library) {
        return;
      }
      row.dataset.state = library.state;
      const status = row.querySelector("[data-library-status]");
      if (status && status.textContent !== library.text) {
        status.textContent = library.text;
      }
      const rescan = row.querySelector("[data-library-rescan]");
      if (rescan) {
        rescan.disabled = library.state === "scanning";
      }
      scanning = scanning || library.state === "scanning";
    });
    return scanning;
  };

  const pollLibraries = async () => {
    try {
      const response = await fetch("/api/libraries/status", { headers: { Accept: "application/json" } });
      if (!response.ok) {
        throw new Error("Status unavailable");
      }
      const payload = await response.json();
      if (applyLibraryStatus(payload.libraries || [])) {
        libraryPollTimer = window.setTimeout(pollLibraries, 1500);
      }
    } catch {
      libraryPollTimer = window.setTimeout(pollLibraries, 5000);
    }
  };

  if (libraryRows.some((row) => row.dataset.state === "scanning")) {
    pollLibraries();
  }
  document.querySelectorAll("[data-library-rescan]").forEach((button) => {
    button.addEventListener("click", () => window.clearTimeout(libraryPollTimer));
  });

  // Library names are edited in place: the card swaps its title and actions for a small form.
  document.querySelectorAll("[data-library-rename]").forEach((button) => {
    const card = button.closest("[data-library-id]");
    const form = card.querySelector("[data-library-rename-form]");
    const input = form.querySelector("input");
    const saved = input.value;

    const setEditing = (editing) => {
      card.classList.toggle("is-renaming", editing);
      form.hidden = !editing;
      if (editing) {
        input.focus();
        input.select();
      } else {
        input.value = saved;
        button.focus();
      }
    };

    button.addEventListener("click", () => setEditing(true));
    form.querySelector("[data-library-rename-cancel]").addEventListener("click", () => setEditing(false));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setEditing(false);
      }
    });
  });

  const addDialog = document.querySelector("#add-source");
  const libraryForm = document.querySelector("[data-library-form]");
  const nameInput = document.querySelector("#library-name");
  const folderPicker = document.querySelector("[data-folder-picker]");
  let openFolderPicker = () => {};
  let firstPathInput = () => null;
  let setLibraryTarget = () => {};

  if (libraryForm && folderPicker) {
    const folderRows = libraryForm.querySelector("[data-folder-rows]");
    const rowTemplate = libraryForm.querySelector("[data-folder-row-template]");
    const addRowButton = libraryForm.querySelector("[data-folder-add-row]");
    const nameField = libraryForm.querySelector("[data-library-name-field]");
    const librarySubmit = libraryForm.querySelector("[data-library-submit]");
    const localPanel = libraryForm.closest("[data-add-panel]");
    const folderEntries = folderPicker.querySelector("[data-folder-entries]");
    const folderCurrent = folderPicker.querySelector("[data-folder-current]");
    const folderStatus = folderPicker.querySelector("[data-folder-status]");
    const folderUp = folderPicker.querySelector("[data-folder-up]");
    const folderChoose = folderPicker.querySelector("[data-folder-choose]");
    let currentListing = { path: "", parent: null, entries: [] };
    let activeRow = null;
    let suggestedName = nameInput?.value || "";
    let libraryTarget = libraryForm.getAttribute("action").match(/libraries\/(\d+)\/folders/)?.[1] || "";
    let startNear = "";

    const rows = () => Array.from(folderRows.querySelectorAll("[data-folder-row]"));
    const pathOf = (row) => row.querySelector("[data-folder-path]");
    const lastSegment = (path) => path.split("/").filter(Boolean).pop() || "";
    firstPathInput = () => pathOf(rows().find((row) => !pathOf(row).value.trim()) || rows()[0]);

    // A single folder names the library after itself; several folders fall back to "Local library".
    const updateSuggestedName = () => {
      if (!nameInput) {
        return;
      }
      const all = rows();
      const next = all.length === 1 ? lastSegment(pathOf(all[0]).value.trim()) : "";
      if (!nameInput.value || nameInput.value === suggestedName) {
        nameInput.value = next;
      }
      suggestedName = next;
      nameInput.placeholder = all.length === 1 ? "Uses the folder name" : "Local library";
    };

    // Radio groups are numbered by position so the server can pair each path with its type.
    const renumber = () => {
      const all = rows();
      all.forEach((row, index) => {
        row.querySelectorAll('input[type="radio"]').forEach((radio) => {
          radio.name = `media_type_${index}`;
        });
        pathOf(row).id = `library-path-${index}`;
        row.querySelector("[data-folder-remove-row]").hidden = all.length === 1;
      });
      updateSuggestedName();
    };

    const guessType = (row, path) => {
      if ("typeChosen" in row.dataset) {
        return;
      }
      const tv = /\b(tv|shows?|series)\b/i.test(lastSegment(path));
      row.querySelector(`input[type="radio"][value="${tv ? "tv" : "movie"}"]`).checked = true;
    };

    const usePath = (path) => {
      if (!activeRow) {
        return;
      }
      pathOf(activeRow).value = path;
      guessType(activeRow, path);
      updateSuggestedName();
    };

    const renderListing = (listing) => {
      currentListing = listing;
      folderCurrent.textContent = listing.path || "Allowed folders";
      folderUp.disabled = listing.parent === null;
      folderChoose.disabled = !listing.path;
      folderEntries.replaceChildren();
      listing.entries.forEach((entry) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.setAttribute("viewBox", "0 0 24 24");
        icon.setAttribute("aria-hidden", "true");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute(
          "d",
          "M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l2 2h8.8A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z",
        );
        icon.append(path);
        const label = document.createElement("span");
        label.textContent = entry.name;
        button.append(icon, label);
        button.addEventListener("click", () => loadFolder(entry.path, true));
        item.append(button);
        folderEntries.append(item);
      });
      folderStatus.textContent = listing.entries.length ? "" : "No subfolders here.";
    };

    const loadFolder = async (path, fill) => {
      folderStatus.textContent = "Loading…";
      try {
        const params = new URLSearchParams();
        if (path) {
          params.set("path", path);
        }
        const response = await fetch(`/api/libraries/folders?${params}`, {
          headers: { Accept: "application/json" },
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Folder unavailable");
        }
        renderListing(payload);
        if (fill && payload.path && payload.path !== "/") {
          usePath(payload.path);
        }
      } catch (error) {
        folderStatus.textContent = error.message || "Could not open that folder.";
      }
    };

    const closePicker = () => {
      folderPicker.hidden = true;
      folderRows.querySelectorAll("[data-folder-browse]").forEach((button) => {
        button.setAttribute("aria-expanded", "false");
      });
    };

    // The one folder picker moves under whichever row is being browsed. A new, empty row starts
    // next to the folder chosen in the row above it.
    const openPickerFor = (row) => {
      closePicker();
      activeRow = row;
      row.append(folderPicker);
      folderPicker.hidden = false;
      row.querySelector("[data-folder-browse]").setAttribute("aria-expanded", "true");
      let start = pathOf(row).value.trim();
      if (!start) {
        const above = rows()
          .slice(0, rows().indexOf(row))
          .map((candidate) => pathOf(candidate).value.trim())
          .filter(Boolean)
          .pop();
        const near = above || startNear;
        start = near ? near.replace(/\/[^/]+\/?$/, "") : "";
      }
      loadFolder(start, false);
    };

    openFolderPicker = () => {
      const all = rows();
      if (folderPicker.hidden && all.length === 1 && !pathOf(all[0]).value.trim()) {
        openPickerFor(all[0]);
      }
    };

    const addRow = () => {
      const fragment = rowTemplate.content.cloneNode(true);
      const row = fragment.querySelector("[data-folder-row]");
      folderRows.append(fragment);
      renumber();
      openPickerFor(row);
      pathOf(row).focus();
    };

    const removeRow = (row) => {
      const all = rows();
      const index = all.indexOf(row);
      if (row.contains(folderPicker)) {
        closePicker();
        folderRows.after(folderPicker);
        activeRow = null;
      }
      row.remove();
      renumber();
      const remaining = rows();
      pathOf(remaining[Math.min(index, remaining.length - 1)]).focus();
    };

    const resetRows = () => {
      closePicker();
      folderRows.after(folderPicker);
      activeRow = null;
      rows()
        .slice(1)
        .forEach((row) => row.remove());
      const [first] = rows();
      pathOf(first).value = "";
      delete first.dataset.typeChosen;
      first.querySelector('input[type="radio"][value="movie"]').checked = true;
      renumber();
    };

    // Opening the dialog for a different library (or a new one) starts from a single empty row.
    setLibraryTarget = (id, name, near) => {
      if (id !== libraryTarget) {
        libraryTarget = id;
        resetRows();
      }
      startNear = near || "";
      libraryForm.action = id ? `/settings/libraries/${id}/folders` : "/settings/libraries";
      nameField.hidden = Boolean(id);
      nameInput.disabled = Boolean(id);
      if (id) {
        nameInput.value = "";
        suggestedName = "";
      } else {
        updateSuggestedName();
      }
      localPanel.dataset.title = id ? `Add folders to ${name}` : "Add a local library";
      librarySubmit.textContent = id ? "Add and rescan" : "Add and scan";
    };

    folderRows.addEventListener("click", (event) => {
      const row = event.target.closest("[data-folder-row]");
      if (!row) {
        return;
      }
      if (event.target.closest("[data-folder-browse]")) {
        if (activeRow === row && !folderPicker.hidden) {
          closePicker();
        } else {
          openPickerFor(row);
        }
      } else if (event.target.closest("[data-folder-remove-row]")) {
        removeRow(row);
      }
    });
    folderRows.addEventListener("change", (event) => {
      if (event.target.matches('input[type="radio"]')) {
        event.target.closest("[data-folder-row]").dataset.typeChosen = "";
      }
    });
    folderRows.addEventListener("input", (event) => {
      if (event.target.matches("[data-folder-path]")) {
        updateSuggestedName();
      }
    });
    addRowButton.addEventListener("click", addRow);
    folderUp.addEventListener("click", () => {
      if (currentListing.parent !== null) {
        loadFolder(currentListing.parent, true);
      }
    });
    folderChoose.addEventListener("click", () => {
      const row = activeRow;
      if (currentListing.path) {
        usePath(currentListing.path);
      }
      closePicker();
      if (row) {
        pathOf(row).focus();
      }
    });
  }

  const providerForm = document.querySelector("[data-provider-form]");

  if (providerForm) {
    const providerSearch = providerForm.querySelector("[data-provider-search]");
    const providerChoices = Array.from(providerForm.querySelectorAll(".provider-choice"));
    const providerEmpty = providerForm.querySelector("[data-provider-empty]");
    const providerCount = providerForm.querySelector("[data-provider-count]");
    const providerSubmit = providerForm.querySelector("[data-provider-submit]");

    const updateProviderCount = () => {
      if (!providerChoices.length) {
        return;
      }
      const selected = providerChoices.filter((choice) => choice.querySelector("input").checked).length;
      providerSubmit.disabled = selected === 0;
      providerSubmit.textContent = selected ? `Add ${selected} service${selected === 1 ? "" : "s"}` : "Add services";
      providerCount.textContent = selected ? `${selected} selected` : "Select one or more";
    };

    providerSearch?.addEventListener("input", () => {
      const term = providerSearch.value.trim().toLowerCase();
      let visible = 0;
      providerChoices.forEach((choice) => {
        const match =
          !term || choice.dataset.providerName.includes(term) || choice.querySelector("input").checked;
        choice.hidden = !match;
        visible += match ? 1 : 0;
      });
      if (providerEmpty) {
        providerEmpty.hidden = visible > 0;
      }
    });
    providerSearch?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
      }
    });
    providerForm.addEventListener("change", updateProviderCount);
    updateProviderCount();
  }

  if (addDialog) {
    const addPanels = Array.from(addDialog.querySelectorAll("[data-add-panel]"));
    const addTitle = addDialog.querySelector("[data-add-title]");
    const addBack = addDialog.querySelector("[data-add-back]");
    const finePointer = window.matchMedia("(pointer: fine)");
    let addOpener;

    const focusStep = (step, panel) => {
      let target = panel?.querySelector(".source-choice");
      if (step === "streaming") {
        target = finePointer.matches ? panel.querySelector("[data-provider-search]") : null;
      } else if (step === "local") {
        target = finePointer.matches ? firstPathInput() : null;
      }
      (target || addTitle).focus();
    };

    const showStep = (step) => {
      const panel = addPanels.find((candidate) => candidate.dataset.addPanel === step) || addPanels[0];
      addPanels.forEach((candidate) => {
        candidate.hidden = candidate !== panel;
      });
      addTitle.textContent = panel.dataset.title;
      addBack.hidden = panel.dataset.addPanel === "choose";
      addDialog.dataset.step = panel.dataset.addPanel;
      if (panel.dataset.addPanel === "local") {
        openFolderPicker();
      }
      focusStep(panel.dataset.addPanel, panel);
    };

    const openAdd = (step, opener) => {
      addOpener = opener;
      if (!addDialog.open) {
        addDialog.showModal();
        document.body.classList.add("dialog-open");
      }
      showStep(step);
    };

    addTitle.tabIndex = -1;
    document.querySelectorAll("[data-open-add]").forEach((button) => {
      button.addEventListener("click", () => {
        setLibraryTarget(
          button.dataset.libraryTarget || "",
          button.dataset.libraryName || "",
          button.dataset.libraryNear || "",
        );
        openAdd(button.dataset.openAdd, button);
      });
    });
    addDialog.querySelectorAll("[data-add-go]").forEach((button) => {
      button.addEventListener("click", () => {
        setLibraryTarget("", "", "");
        showStep(button.dataset.addGo);
      });
    });
    addBack.addEventListener("click", () => showStep("choose"));
    addDialog.querySelector("[data-close-add]")?.addEventListener("click", () => addDialog.close());
    addDialog.addEventListener("click", (event) => {
      if (event.target === addDialog) {
        addDialog.close();
      }
    });
    addDialog.addEventListener("close", () => {
      document.body.classList.remove("dialog-open");
      addOpener?.focus();
    });

    if (addDialog.dataset.openStep) {
      openAdd(addDialog.dataset.openStep, document.querySelector("[data-open-add]"));
    }
  }

  const regionForm = document.querySelector("[data-region-form]");
  const regionSelect = regionForm?.querySelector("select");

  if (regionForm && regionSelect) {
    const savedRegion = regionSelect.value;
    regionSelect.addEventListener("change", () => {
      const enabled = document.querySelectorAll(".source-card:not(.source-card-local)").length;
      const name = regionSelect.selectedOptions[0]?.textContent.trim() || regionSelect.value;
      if (
        enabled &&
        !window.confirm(`Switch the streaming region to ${name}? Services that aren’t offered there are removed.`)
      ) {
        regionSelect.value = savedRegion;
        return;
      }
      regionForm.requestSubmit();
    });
  }
})();
