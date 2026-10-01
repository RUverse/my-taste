(() => {
  "use strict";

  const configElement = document.getElementById("player-config");
  const root = document.querySelector("[data-player]");
  if (!configElement || !root) {
    return;
  }
  const config = JSON.parse(configElement.textContent);
  const video = root.querySelector("[data-video]");
  const find = (selector) => root.querySelector(selector);
  const findAll = (selector) => Array.from(root.querySelectorAll(selector));

  const elements = {
    bigPlay: find("[data-big-play]"),
    flash: find("[data-flash]"),
    message: find("[data-message]"),
    messageTitle: find("[data-message-title]"),
    messageText: find("[data-message-text]"),
    retry: find("[data-retry]"),
    resume: find("[data-resume]"),
    resumeLabel: find("[data-resume-label]"),
    resumePlay: find("[data-resume-play]"),
    resumeRestart: find("[data-resume-restart]"),
    upNext: find("[data-upnext]"),
    upNextStill: find("[data-upnext-still]"),
    upNextTitle: find("[data-upnext-title]"),
    upNextCount: find("[data-upnext-count]"),
    upNextPlay: find("[data-upnext-play]"),
    upNextCancel: find("[data-upnext-cancel]"),
    seek: find("[data-seek]"),
    played: find("[data-played]"),
    buffered: find("[data-buffered]"),
    chapters: find("[data-chapters]"),
    tooltip: find("[data-tooltip]"),
    timeline: find("[data-timeline]"),
    play: find("[data-play]"),
    mute: find("[data-mute]"),
    volume: find("[data-volume]"),
    current: find("[data-current]"),
    duration: find("[data-duration]"),
    previous: find("[data-previous]"),
    next: find("[data-next]"),
    pip: find("[data-pip]"),
    fullscreen: find("[data-fullscreen]"),
    subtitleOptions: find("[data-subtitle-options]"),
    audioOptions: find("[data-audio-options]"),
    qualityOptions: find("[data-quality-options]"),
    versionOptions: find("[data-version-options]"),
    speedOptions: find("[data-speed-options]"),
    mode: find("[data-playback-mode]"),
    detail: find("[data-playback-detail]"),
    watched: find("[data-toggle-watched]"),
    status: find("[data-status]"),
  };

  // Preferences live on this device, like volume in any other player.
  const preference = {
    get(key, fallback) {
      try {
        const value = window.localStorage.getItem(`mytaste.player.${key}`);
        return value === null ? fallback : JSON.parse(value);
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try {
        window.localStorage.setItem(`mytaste.player.${key}`, JSON.stringify(value));
      } catch {
        // Private browsing may refuse storage; the player still works.
      }
    },
  };

  const randomId = () => {
    const bytes = new Uint8Array(12);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  };

  const playerId = (() => {
    try {
      let id = window.sessionStorage.getItem("mytaste.player.id");
      if (!id) {
        id = randomId();
        window.sessionStorage.setItem("mytaste.player.id", id);
      }
      return id;
    } catch {
      return randomId();
    }
  })();

  const formatTime = (seconds) => {
    if (!Number.isFinite(seconds) || seconds < 0) {
      seconds = 0;
    }
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const rest = String(total % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
  };

  const announce = (text) => {
    elements.status.textContent = text;
  };

  // What this browser can decode, sent to the server so it can choose direct play, a remux,
  // or a transcode. Nothing is guessed from file names.
  const detectCapabilities = () => {
    const probe = document.createElement("video");
    const mediaSource = window.ManagedMediaSource || window.MediaSource;
    const native = (type) => probe.canPlayType(type) !== "";
    const mse = (type) => {
      try {
        return Boolean(mediaSource?.isTypeSupported?.(type));
      } catch {
        return false;
      }
    };
    const videoTypes = {
      h264: 'video/mp4; codecs="avc1.640028"',
      hevc: 'video/mp4; codecs="hvc1.1.6.L120.90"',
      hevc10: 'video/mp4; codecs="hvc1.2.4.L120.90"',
      av1: 'video/mp4; codecs="av01.0.08M.08"',
      vp9: 'video/mp4; codecs="vp09.00.40.08"',
    };
    const audioTypes = {
      aac: 'audio/mp4; codecs="mp4a.40.2"',
      mp3: "audio/mpeg",
      ac3: 'audio/mp4; codecs="ac-3"',
      eac3: 'audio/mp4; codecs="ec-3"',
      opus: 'audio/mp4; codecs="opus"',
      flac: 'audio/mp4; codecs="flac"',
    };
    const useHlsJs = Boolean(window.Hls?.isSupported?.());
    const nativeHls = native("application/vnd.apple.mpegurl");
    const hlsCheck = useHlsJs ? mse : native;
    const pick = (types, check) => Object.keys(types).filter((key) => check(types[key]));
    const containers = ["mp4"];
    if (native("video/webm")) {
      containers.push("webm");
    }
    if (native("video/x-matroska") || native("video/mkv")) {
      containers.push("mkv");
    }
    return {
      hls: useHlsJs || nativeHls,
      direct_containers: containers,
      direct_video: pick(videoTypes, native),
      direct_audio: pick(audioTypes, native),
      hls_video: useHlsJs || nativeHls ? pick(videoTypes, hlsCheck) : [],
      hls_audio: useHlsJs || nativeHls ? pick(audioTypes, hlsCheck) : [],
    };
  };

  const capabilities = detectCapabilities();
  const subtitleOptions = Array.isArray(config.subtitles) ? config.subtitles : [];
  const audioOptions = Array.isArray(config.audio) ? config.audio : [];
  const preferredAudio = preference.get("audioLanguage", "");
  const initialAudio = audioOptions.find(
    (option) => preferredAudio && option.language === preferredAudio && !option.default,
  );

  const state = {
    hls: null,
    session: null,
    mode: null,
    token: 0,
    started: false,
    excluded: new Set(),
    audio: initialAudio ? initialAudio.index : null,
    burn: null,
    maxHeight: preference.get("quality", null),
    subtitle: null,
    position: 0,
    duration: Number(config.duration) || 0,
    scrubbing: false,
    networkRestarts: 0,
    mediaRecovered: false,
    upNextDismissed: false,
    upNextTimer: null,
    watched: Boolean(config.watched),
  };
  if (!config.qualities?.includes(state.maxHeight)) {
    state.maxHeight = null;
  }
  // A browser that once failed to play this kind of file directly goes straight to streaming.
  const directKey = `noDirect.${config.format}`;
  if (config.format && preference.get(directKey, false)) {
    state.excluded.add("direct");
  }

  // Sessions -----------------------------------------------------------------------------

  const stopSession = (beacon = false) => {
    const id = state.session;
    state.session = null;
    if (!id) {
      return;
    }
    const url = `/api/playback/sessions/${encodeURIComponent(id)}/stop`;
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon(url);
    } else {
      fetch(url, { method: "POST", keepalive: true }).catch(() => {});
    }
  };

  const teardown = () => {
    if (state.hls) {
      state.hls.destroy();
      state.hls = null;
    }
    video.removeAttribute("src");
    video.load();
  };

  const setLoading = (loading) => root.classList.toggle("is-loading", loading);

  const showMessage = (title, text, { retry = true } = {}) => {
    setLoading(false);
    elements.messageTitle.textContent = title;
    elements.messageText.textContent = text || "";
    elements.retry.hidden = !retry;
    elements.message.hidden = false;
    elements.bigPlay.hidden = true;
    announce(`${title}. ${text || ""}`);
  };

  const hideMessage = () => {
    elements.message.hidden = true;
  };

  const currentPosition = () =>
    Number.isFinite(video.currentTime) && video.currentTime > 0 ? video.currentTime : state.position;

  const begin = async (position) => {
    const token = ++state.token;
    state.position = position;
    hideMessage();
    elements.resume.hidden = true;
    setLoading(true);
    stopSession();
    teardown();
    let response;
    let payload = {};
    try {
      response = await fetch("/api/playback/sessions", {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          file_id: config.file_id,
          player: playerId,
          capabilities,
          audio: state.audio,
          burn_subtitle: state.burn,
          max_height: state.maxHeight,
          exclude: Array.from(state.excluded),
          start: position,
        }),
      });
      payload = await response.json().catch(() => ({}));
    } catch {
      if (token === state.token) {
        showMessage("Can’t reach MyTaste", "Check the connection to your server and try again.");
      }
      return;
    }
    if (token !== state.token) {
      return;
    }
    if (!response.ok) {
      showMessage("Can’t play this video", payload.error || "The server could not prepare it.");
      return;
    }
    state.session = payload.session;
    state.mode = payload.mode;
    state.mediaRecovered = false;
    describePlayback(payload);
    if (payload.mode !== "direct" && window.Hls?.isSupported?.()) {
      attachHls(payload.url, position, token);
    } else {
      video.src = payload.url;
      if (position > 0) {
        video.addEventListener(
          "loadedmetadata",
          () => {
            if (token === state.token) {
              video.currentTime = position;
            }
          },
          { once: true },
        );
      }
    }
    applySubtitle();
    watchStartup(token, { direct: 10, remux: 30, transcode: 60 }[payload.mode] || 30);
    play();
  };

  // Some browsers claim they can play a file and then never load it, without an error.
  // If no picture arrives in time, fall back to the next, safer delivery method.
  let startupTimer;
  const watchStartup = (token, seconds) => {
    window.clearTimeout(startupTimer);
    startupTimer = window.setTimeout(() => {
      if (token === state.token && video.readyState < 2 && elements.message.hidden) {
        fallBack(false);
      }
    }, seconds * 1000);
  };
  video.addEventListener("loadeddata", () => window.clearTimeout(startupTimer));

  // A stream that stays stuck mid-film offers a retry from the same place.
  let stallTimer;
  const watchStall = () => {
    window.clearTimeout(stallTimer);
    stallTimer = window.setTimeout(() => {
      if (state.started && !video.paused && video.readyState < 3 && elements.message.hidden) {
        showMessage("Still loading…", "The video has not loaded for a while.");
        elements.retry.focus();
      }
    }, 20000);
  };
  video.addEventListener("waiting", watchStall);
  ["playing", "pause", "seeked", "canplaythrough"].forEach((name) => {
    video.addEventListener(name, () => window.clearTimeout(stallTimer));
  });

  const attachHls = (url, position, token) => {
    const Hls = window.Hls;
    const retry = {
      maxTimeToFirstByteMs: 90000,
      maxLoadTimeMs: 150000,
      timeoutRetry: { maxNumRetry: 1, retryDelayMs: 0, maxRetryDelayMs: 0 },
      errorRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
    };
    const hls = new Hls({
      startPosition: position,
      maxBufferLength: 30,
      maxMaxBufferLength: 90,
      backBufferLength: 60,
      fragLoadPolicy: { default: retry },
      manifestLoadPolicy: { default: retry },
      playlistLoadPolicy: { default: retry },
    });
    state.hls = hls;
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal || token !== state.token) {
        return;
      }
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !state.mediaRecovered) {
        state.mediaRecovered = true;
        hls.recoverMediaError();
        return;
      }
      const status = data.response?.code;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR && status === 404 && state.networkRestarts < 3) {
        // The server stopped an idle session; start a new one where we were.
        state.networkRestarts += 1;
        begin(currentPosition());
        return;
      }
      fallBack(data.type === Hls.ErrorTypes.NETWORK_ERROR);
    });
    hls.loadSource(url);
    hls.attachMedia(video);
  };

  // Direct play, then a remux, then a transcode: each failure tries the next, safer method.
  const fallBack = (networkError) => {
    const position = currentPosition();
    if (state.mode === "direct" && !state.excluded.has("direct")) {
      state.excluded.add("direct");
      if (config.format && !networkError) {
        preference.set(directKey, true);
      }
      begin(position);
      return;
    }
    if (state.mode === "remux" && !networkError && !state.excluded.has("remux") && config.can_transcode) {
      state.excluded.add("remux");
      begin(position);
      return;
    }
    showMessage(
      "Playback stopped",
      networkError
        ? "The video stopped loading. The drive may be busy or disconnected."
        : "This browser could not decode the video.",
    );
  };

  video.addEventListener("error", () => {
    if (!state.hls && video.getAttribute("src")) {
      fallBack(video.error?.code === 2);
    }
  });

  const play = () => {
    const attempt = video.play();
    if (attempt?.catch) {
      attempt.catch((error) => {
        if (error?.name === "NotAllowedError") {
          setLoading(false);
          elements.bigPlay.hidden = false;
          showControls();
        }
      });
    }
  };

  const togglePlay = () => {
    if (!state.started && !state.mode) {
      return;
    }
    if (video.paused || video.ended) {
      play();
    } else {
      video.pause();
    }
  };

  // Progress -----------------------------------------------------------------------------

  const progressBody = (extra = {}) =>
    JSON.stringify({
      file_id: config.file_id,
      position: currentPosition(),
      duration: state.duration,
      session: state.session,
      ...extra,
    });

  const reportProgress = (extra = {}) => {
    if (!state.started) {
      return;
    }
    fetch("/api/playback/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: progressBody(extra),
      keepalive: true,
    })
      .then((response) => response.json())
      .then((payload) => {
        if (typeof payload.watched === "boolean") {
          state.watched = payload.watched;
          renderWatched();
        }
      })
      .catch(() => {});
  };

  let lastReport = 0;
  window.setInterval(() => {
    const now = Date.now();
    const interval = video.paused ? 30000 : 10000;
    if (state.started && now - lastReport >= interval) {
      lastReport = now;
      reportProgress();
    }
  }, 2000);

  window.addEventListener("pagehide", () => {
    if (state.started && navigator.sendBeacon) {
      navigator.sendBeacon(
        "/api/playback/progress",
        new Blob([progressBody()], { type: "application/json" }),
      );
    }
    stopSession(true);
  });

  // Video events -----------------------------------------------------------------------------

  const renderTime = () => {
    const position = state.scrubbing ? Number(elements.seek.value) / 1000 * state.duration : currentPosition();
    const fraction = state.duration ? Math.min(position / state.duration, 1) : 0;
    elements.played.style.width = `${fraction * 100}%`;
    if (!state.scrubbing) {
      elements.seek.value = String(Math.round(fraction * 1000));
    }
    elements.seek.setAttribute("aria-valuetext", `${formatTime(position)} of ${formatTime(state.duration)}`);
    elements.current.textContent = formatTime(position);
    elements.duration.textContent = formatTime(state.duration);
  };

  const renderBuffered = () => {
    const time = video.currentTime;
    let end = 0;
    for (let index = 0; index < video.buffered.length; index += 1) {
      if (video.buffered.start(index) <= time + 0.5 && video.buffered.end(index) >= time) {
        end = video.buffered.end(index);
      }
    }
    elements.buffered.style.width = state.duration ? `${Math.min(end / state.duration, 1) * 100}%` : "0";
  };

  video.addEventListener("timeupdate", () => {
    if (video.currentTime > 0) {
      state.position = video.currentTime;
    }
    renderTime();
    renderBuffered();
    maybeShowUpNext();
  });
  video.addEventListener("progress", renderBuffered);
  video.addEventListener("durationchange", () => {
    if (!state.duration && Number.isFinite(video.duration)) {
      state.duration = video.duration;
    }
    renderTime();
  });
  video.addEventListener("waiting", () => setLoading(true));
  video.addEventListener("seeking", () => setLoading(true));
  video.addEventListener("seeked", () => {
    if (!video.paused) {
      return;
    }
    setLoading(false);
  });
  video.addEventListener("canplay", () => {
    if (video.paused) {
      setLoading(false);
    }
  });
  video.addEventListener("playing", () => {
    setLoading(false);
    elements.bigPlay.hidden = true;
    if (!state.started) {
      state.started = true;
      root.classList.add("has-started");
    }
    state.networkRestarts = 0;
  });
  video.addEventListener("play", () => {
    root.classList.add("is-playing");
    elements.play.setAttribute("aria-label", "Pause");
    elements.play.title = "Pause (k)";
    scheduleIdle();
  });
  video.addEventListener("pause", () => {
    root.classList.remove("is-playing");
    elements.play.setAttribute("aria-label", "Play");
    elements.play.title = "Play (k)";
    showControls();
    reportProgress();
  });
  video.addEventListener("ended", () => {
    reportProgress({ ended: true });
    state.watched = true;
    renderWatched();
    if (config.next) {
      startUpNextCountdown();
    }
  });
  video.addEventListener("volumechange", () => {
    root.classList.toggle("is-muted", video.muted || video.volume === 0);
    elements.mute.setAttribute("aria-label", video.muted ? "Unmute" : "Mute");
    elements.volume.value = String(video.muted ? 0 : video.volume);
    preference.set("volume", video.volume);
    preference.set("muted", video.muted);
  });

  // Controls -------------------------------------------------------------------------------

  let idleTimer;
  const showControls = () => {
    root.classList.remove("is-idle");
    scheduleIdle();
  };
  const scheduleIdle = () => {
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(() => {
      const menuOpen = findAll("[data-menu]").some((menu) => !menu.hidden);
      const focusInControls = root.querySelector(".player-controls")?.contains(document.activeElement)
        && document.activeElement !== elements.seek && document.activeElement?.matches(":focus-visible");
      if (!video.paused && !menuOpen && !focusInControls && !state.scrubbing) {
        root.classList.add("is-idle");
      }
    }, 3000);
  };

  root.addEventListener("pointermove", (event) => {
    if (event.pointerType === "mouse") {
      showControls();
    }
  });

  const clickArea = find("[data-click-area]");
  let lastTap = 0;
  clickArea.addEventListener("click", (event) => {
    if (closeMenus()) {
      return;
    }
    if (event.pointerType === "touch" || window.matchMedia("(hover: none)").matches) {
      // On touch screens a tap shows or hides the controls; a double tap seeks.
      const now = Date.now();
      if (now - lastTap < 300) {
        const rect = clickArea.getBoundingClientRect();
        skip(event.clientX - rect.left < rect.width / 2 ? -10 : 10);
        lastTap = 0;
        return;
      }
      lastTap = now;
      if (root.classList.contains("is-idle")) {
        showControls();
      } else if (!video.paused) {
        root.classList.add("is-idle");
      }
      return;
    }
    togglePlay();
  });
  clickArea.addEventListener("dblclick", (event) => {
    if (!window.matchMedia("(hover: none)").matches) {
      event.preventDefault();
      toggleFullscreen();
    }
  });

  elements.bigPlay.addEventListener("click", () => {
    elements.bigPlay.hidden = true;
    play();
  });
  elements.play.addEventListener("click", togglePlay);

  let flashTimer;
  const flash = (text) => {
    elements.flash.textContent = text;
    elements.flash.classList.add("is-visible");
    window.clearTimeout(flashTimer);
    flashTimer = window.setTimeout(() => elements.flash.classList.remove("is-visible"), 700);
  };

  const seekTo = (seconds) => {
    const target = Math.max(0, Math.min(seconds, Math.max(state.duration - 0.5, 0)));
    state.position = target;
    if (state.mode) {
      video.currentTime = target;
    }
    renderTime();
  };

  const skip = (seconds) => {
    seekTo(currentPosition() + seconds);
    flash(seconds > 0 ? `+${seconds} s` : `−${Math.abs(seconds)} s`);
    showControls();
  };

  findAll("[data-skip]").forEach((button) => {
    button.addEventListener("click", () => skip(Number(button.dataset.skip)));
  });

  const timeAt = (clientX) => {
    const rect = elements.seek.getBoundingClientRect();
    const fraction = Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1);
    return { fraction, seconds: fraction * state.duration };
  };

  const chapterAt = (seconds) => {
    const chapters = (config.chapters || []).filter((chapter) => chapter.start <= seconds);
    const title = chapters.length ? chapters[chapters.length - 1].title : "";
    return /^\d{1,2}:\d{2}/.test(title) || /^chapter \d+$/i.test(title) ? "" : title;
  };

  elements.timeline.addEventListener("pointermove", (event) => {
    const { fraction, seconds } = timeAt(event.clientX);
    const chapter = chapterAt(seconds);
    elements.tooltip.textContent = chapter ? `${formatTime(seconds)} · ${chapter}` : formatTime(seconds);
    elements.tooltip.style.left = `${fraction * 100}%`;
  });
  elements.seek.addEventListener("input", () => {
    state.scrubbing = true;
    renderTime();
  });
  elements.seek.addEventListener("change", () => {
    state.scrubbing = false;
    seekTo(Number(elements.seek.value) / 1000 * state.duration);
  });
  elements.seek.addEventListener("keydown", (event) => {
    // Arrow keys on the slider move by ten seconds rather than a thousandth of the film.
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      event.stopPropagation();
      skip(event.key === "ArrowLeft" ? -10 : 10);
    }
  });

  const storedVolume = Number(preference.get("volume", 1));
  video.volume = Number.isFinite(storedVolume) ? Math.min(Math.max(storedVolume, 0), 1) : 1;
  video.muted = Boolean(preference.get("muted", false));
  elements.volume.value = String(video.muted ? 0 : video.volume);
  root.classList.toggle("is-muted", video.muted);
  elements.mute.addEventListener("click", () => {
    video.muted = !video.muted;
    if (!video.muted && video.volume === 0) {
      video.volume = 0.5;
    }
  });
  elements.volume.addEventListener("input", () => {
    video.volume = Number(elements.volume.value);
    video.muted = video.volume === 0;
  });

  const changeVolume = (delta) => {
    video.muted = false;
    video.volume = Math.min(Math.max(video.volume + delta, 0), 1);
    flash(`Volume ${Math.round(video.volume * 100)}%`);
  };

  // Full screen and picture in picture.
  const fullscreenElement = () => document.fullscreenElement || document.webkitFullscreenElement;
  const toggleFullscreen = () => {
    if (fullscreenElement()) {
      (document.exitFullscreen || document.webkitExitFullscreen)?.call(document);
      return;
    }
    const request = root.requestFullscreen || root.webkitRequestFullscreen;
    if (request) {
      request.call(root).catch?.(() => {});
    } else if (video.webkitEnterFullscreen) {
      video.webkitEnterFullscreen();
    }
  };
  const syncFullscreen = () => {
    const active = Boolean(fullscreenElement());
    root.classList.toggle("is-fullscreen", active);
    elements.fullscreen.setAttribute("aria-label", active ? "Exit full screen" : "Full screen");
  };
  document.addEventListener("fullscreenchange", syncFullscreen);
  document.addEventListener("webkitfullscreenchange", syncFullscreen);
  elements.fullscreen.addEventListener("click", toggleFullscreen);

  if (document.pictureInPictureEnabled && video.requestPictureInPicture) {
    elements.pip.hidden = false;
    elements.pip.addEventListener("click", () => {
      if (document.pictureInPictureElement) {
        document.exitPictureInPicture().catch(() => {});
      } else {
        video.requestPictureInPicture().catch(() => {});
      }
    });
  }

  // Episodes.
  if (config.previous) {
    elements.previous.href = config.previous.url;
    elements.previous.title = `Previous: ${config.previous.label}`;
    elements.previous.setAttribute("aria-label", `Previous episode: ${config.previous.label}`);
    elements.previous.hidden = false;
  }
  if (config.next) {
    elements.next.href = `${config.next.url}?autoplay=1`;
    elements.next.title = `Next: ${config.next.label}`;
    elements.next.setAttribute("aria-label", `Next episode: ${config.next.label}`);
    elements.next.hidden = false;
    elements.upNextTitle.textContent = config.next.label;
    elements.upNextPlay.href = `${config.next.url}?autoplay=1`;
    if (config.next.still) {
      const still = document.createElement("img");
      still.src = config.next.still;
      still.alt = "";
      elements.upNextStill.append(still);
    }
  }

  const maybeShowUpNext = () => {
    if (!config.next || state.upNextDismissed || !state.duration) {
      return;
    }
    const remaining = state.duration - currentPosition();
    elements.upNext.hidden = !(remaining <= 25 && remaining > 0 && !video.paused);
  };

  const startUpNextCountdown = () => {
    if (state.upNextDismissed) {
      return;
    }
    let count = 8;
    elements.upNext.hidden = false;
    elements.upNextCount.textContent = `in ${count}`;
    window.clearInterval(state.upNextTimer);
    state.upNextTimer = window.setInterval(() => {
      count -= 1;
      elements.upNextCount.textContent = count > 0 ? `in ${count}` : "";
      if (count <= 0) {
        window.clearInterval(state.upNextTimer);
        window.location.assign(elements.upNextPlay.href);
      }
    }, 1000);
  };

  elements.upNextCancel.addEventListener("click", () => {
    state.upNextDismissed = true;
    window.clearInterval(state.upNextTimer);
    elements.upNext.hidden = true;
  });

  // Chapter marks on the timeline.
  if (state.duration && Array.isArray(config.chapters)) {
    config.chapters
      .filter((chapter) => chapter.start > 1 && chapter.start < state.duration - 1)
      .forEach((chapter) => {
        const mark = document.createElement("span");
        mark.style.left = `${(chapter.start / state.duration) * 100}%`;
        elements.chapters.append(mark);
      });
  }

  // Menus ------------------------------------------------------------------------------------

  const menus = findAll("[data-menu]");
  const menuButtons = findAll("[data-menu-button]");
  const closeMenus = () => {
    let closed = false;
    menus.forEach((menu) => {
      if (!menu.hidden) {
        menu.hidden = true;
        closed = true;
      }
    });
    menuButtons.forEach((button) => button.setAttribute("aria-expanded", "false"));
    return closed;
  };
  menuButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const menu = find(`[data-menu="${button.dataset.menuButton}"]`);
      const opening = menu.hidden;
      closeMenus();
      if (opening) {
        menu.hidden = false;
        button.setAttribute("aria-expanded", "true");
        (menu.querySelector('[aria-checked="true"]') || menu.querySelector("button"))?.focus();
      }
      showControls();
    });
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("[data-menu], [data-menu-button]")) {
      closeMenus();
    }
  });

  const option = (label, detail, checked, onSelect) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "player-option";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(checked));
    const name = document.createElement("span");
    name.textContent = label;
    button.append(name);
    if (detail) {
      const small = document.createElement("small");
      small.textContent = detail;
      button.append(small);
    }
    button.addEventListener("click", () => {
      button.parentElement.querySelectorAll(".player-option").forEach((item) => {
        item.setAttribute("aria-checked", String(item === button));
      });
      onSelect();
    });
    return button;
  };

  // Subtitles: text tracks are WebVTT served by MyTaste; image tracks are burned into a
  // transcode because browsers cannot draw them.
  const tracks = new Map();
  subtitleOptions.forEach((subtitle) => {
    if (subtitle.kind !== "text") {
      return;
    }
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = subtitle.label;
    if (subtitle.language) {
      track.srclang = subtitle.language;
    }
    track.src = subtitle.url;
    track.addEventListener("load", () => {
      if (state.subtitle === subtitle.id) {
        announce(`${subtitle.label} subtitles on`);
      }
    });
    track.addEventListener("error", () => {
      if (state.subtitle === subtitle.id) {
        flash("Subtitles could not be loaded");
      }
    });
    video.append(track);
    tracks.set(subtitle.id, track);
  });

  const applySubtitle = () => {
    tracks.forEach((track, id) => {
      track.track.mode = id === state.subtitle ? "showing" : "disabled";
    });
    findAll("[data-menu-button='subtitles']").forEach((button) => {
      button.classList.toggle("is-active", Boolean(state.subtitle || state.burn !== null));
    });
  };

  const selectSubtitle = (subtitle) => {
    const wasBurning = state.burn !== null;
    state.subtitle = subtitle && subtitle.kind === "text" ? subtitle.id : null;
    state.burn = subtitle && subtitle.kind === "image" ? subtitle.stream : null;
    preference.set("subtitleLanguage", subtitle ? subtitle.language || subtitle.id : "");
    applySubtitle();
    if (state.burn !== null || wasBurning) {
      begin(currentPosition());
    }
    flash(subtitle ? `Subtitles: ${subtitle.label}` : "Subtitles off");
    closeMenus();
  };

  if (subtitleOptions.length) {
    find("[data-menu-button='subtitles']").hidden = false;
    const preferred = preference.get("subtitleLanguage", "");
    const initial = preferred
      ? subtitleOptions.find(
          (subtitle) =>
            subtitle.kind === "text" && (subtitle.language === preferred || subtitle.id === preferred),
        )
      : null;
    state.subtitle = initial ? initial.id : null;
    elements.subtitleOptions.append(
      option("Off", "", !initial, () => selectSubtitle(null)),
      ...subtitleOptions.map((subtitle) =>
        option(subtitle.label, subtitle.detail, initial === subtitle, () => selectSubtitle(subtitle)),
      ),
    );
    applySubtitle();
  }

  const cycleSubtitles = () => {
    const texts = subtitleOptions.filter((subtitle) => subtitle.kind === "text");
    if (!texts.length) {
      return;
    }
    const index = texts.findIndex((subtitle) => subtitle.id === state.subtitle);
    const next = index + 1 < texts.length ? texts[index + 1] : null;
    selectSubtitle(next);
    elements.subtitleOptions.querySelectorAll(".player-option").forEach((button, position) => {
      const target = next ? subtitleOptions.indexOf(next) + 1 : 0;
      button.setAttribute("aria-checked", String(position === target));
    });
  };

  if (audioOptions.length > 1) {
    find("[data-settings-audio]").hidden = false;
    elements.audioOptions.append(
      ...audioOptions.map((audio) =>
        option(audio.label, "", state.audio === null ? audio.default : audio.index === state.audio, () => {
          state.audio = audio.default ? null : audio.index;
          preference.set("audioLanguage", audio.language || "");
          closeMenus();
          begin(currentPosition());
        }),
      ),
    );
  }

  if (config.can_transcode && Array.isArray(config.qualities) && config.qualities.length) {
    find("[data-settings-quality]").hidden = false;
    const choices = [null, ...config.qualities];
    elements.qualityOptions.append(
      ...choices.map((height) =>
        option(
          height ? `${height}p` : "Auto",
          height ? "Converted on the server" : "Original quality when this browser supports it",
          state.maxHeight === height,
          () => {
            state.maxHeight = height;
            preference.set("quality", height);
            closeMenus();
            begin(currentPosition());
          },
        ),
      ),
    );
  }

  if (Array.isArray(config.versions) && config.versions.length > 1) {
    find("[data-settings-version]").hidden = false;
    elements.versionOptions.append(
      ...config.versions.map((version) =>
        option(version.label, "", version.current, () => {
          if (!version.current) {
            const position = Math.floor(currentPosition());
            window.location.assign(`${version.url}${position ? `&t=${position}` : ""}`);
          }
        }),
      ),
    );
  }

  [0.5, 0.75, 1, 1.25, 1.5, 2].forEach((rate) => {
    elements.speedOptions.append(
      option(rate === 1 ? "Normal" : `${rate}×`, "", rate === 1, () => {
        video.playbackRate = rate;
        video.defaultPlaybackRate = rate;
      }),
    );
  });

  const describePlayback = (payload) => {
    elements.mode.textContent = payload.label;
    elements.detail.textContent = [config.media, ...(payload.reasons || [])].filter(Boolean).join(". ");
  };

  const renderWatched = () => {
    elements.watched.textContent = state.watched ? "Mark as unwatched" : "Mark as watched";
  };
  renderWatched();
  elements.watched.addEventListener("click", () => {
    const watched = !state.watched;
    fetch("/api/playback/watched", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: config.url, watched }),
    })
      .then((response) => {
        if (response.ok) {
          state.watched = watched;
          renderWatched();
          flash(watched ? "Marked as watched" : "Marked as unwatched");
        }
      })
      .catch(() => {});
  });

  // Back goes to the page the viewer came from when it was MyTaste.
  findAll("[data-back]").forEach((link) => {
    link.addEventListener("click", (event) => {
      let sameOrigin = false;
      try {
        sameOrigin = new URL(document.referrer).origin === window.location.origin;
      } catch {
        sameOrigin = false;
      }
      if (sameOrigin && window.history.length > 1) {
        event.preventDefault();
        window.history.back();
      }
    });
  });

  // Keyboard -------------------------------------------------------------------------------

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    const target = event.target;
    if (target instanceof HTMLElement && target.closest("[data-menu]") && event.key !== "Escape") {
      return;
    }
    const onButton = target instanceof HTMLButtonElement || target instanceof HTMLAnchorElement;
    switch (event.key) {
      case " ":
      case "k":
      case "K":
        if (onButton && event.key === " ") {
          return;
        }
        event.preventDefault();
        togglePlay();
        break;
      case "ArrowLeft":
      case "j":
      case "J":
        event.preventDefault();
        skip(-10);
        break;
      case "ArrowRight":
      case "l":
      case "L":
        event.preventDefault();
        skip(10);
        break;
      case "ArrowUp":
        if (target === elements.volume) {
          return;
        }
        event.preventDefault();
        changeVolume(0.05);
        break;
      case "ArrowDown":
        if (target === elements.volume) {
          return;
        }
        event.preventDefault();
        changeVolume(-0.05);
        break;
      case "m":
      case "M":
        video.muted = !video.muted;
        flash(video.muted ? "Muted" : "Sound on");
        break;
      case "f":
      case "F":
        toggleFullscreen();
        break;
      case "c":
      case "C":
        cycleSubtitles();
        break;
      case "n":
      case "N":
        if (config.next) {
          window.location.assign(elements.next.href);
        }
        break;
      case "Escape":
        if (closeMenus()) {
          event.preventDefault();
          find("[data-menu-button][aria-expanded]")?.focus();
        }
        break;
      default:
        if (/^[0-9]$/.test(event.key) && state.duration) {
          seekTo((Number(event.key) / 10) * state.duration);
        }
        return;
    }
    showControls();
  });

  // Start ------------------------------------------------------------------------------------

  elements.retry.addEventListener("click", () => {
    state.networkRestarts = 0;
    begin(currentPosition());
  });

  renderTime();
  const startAt = Number.isFinite(config.start) && config.start > 0 ? config.start : null;
  if (startAt !== null) {
    begin(startAt);
  } else if (config.resume > 0 && config.autoplay) {
    // Moving on to the next episode should not stop to ask.
    begin(config.resume);
  } else if (config.resume > 0) {
    setLoading(false);
    elements.resumeLabel.textContent = `Resume from ${formatTime(config.resume)}`;
    elements.resume.hidden = false;
    elements.resumePlay.focus();
    elements.resumePlay.addEventListener("click", () => begin(config.resume));
    elements.resumeRestart.addEventListener("click", () => begin(0));
  } else {
    begin(0);
  }
  root.focus({ preventScroll: true });
})();
