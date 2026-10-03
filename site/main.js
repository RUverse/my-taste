/* MyTaste landing page.
   Paints the "MY" brush strokes on a canvas, twinkles the glitter in them,
   tints the top bar once the page scrolls, and shows the GitHub star count. */
(() => {
  "use strict";

  const hero = document.querySelector("[data-hero]");
  const paintCanvas = document.querySelector("[data-brush]");
  const glitterCanvas = document.querySelector("[data-glitter]");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const darkScheme = window.matchMedia("(prefers-color-scheme: dark)");

  // Deterministic randomness, so the painting looks the same on every visit.
  function mulberry32(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // The artwork, in units where the hero is 1000 tall and x = 0 is its centre.
  // "bottom" means the stroke runs off the bottom edge of the hero.
  // tint: [colour, from, to] paints a hint of colour along that part of the stroke.
  const ART = {
    top: 300,
    width: 820,
    strokes: [
      // M
      { pts: [[-322, 318], [-328, 520], [-336, 760], [-340, "bottom"]], width: 72, profile: [0.9, 1, 0.96, 0.9, 0.86], under: true, tint: ["#7c5cff", 0.3, 0.55] },
      { pts: [[-316, 334], [-258, 402], [-204, 480], [-168, 532]], width: 78, profile: [0.8, 0.98, 1, 0.86, 0.4], under: true, tint: ["#f47b42", 0.15, 0.55] },
      { pts: [[-172, 530], [-92, 462], [-20, 396], [50, 338]], width: 50, profile: [0.82, 0.97, 1, 0.84, 0.45] },
      { pts: [[46, 342], [36, 560], [22, 800], [14, "bottom"]], width: 54, profile: [0.6, 0.92, 1, 1, 0.96], tint: ["#ff4fa3", 0.06, 0.3] },
      // Y
      { pts: [[100, 300], [160, 372], [214, 440], [246, 482]], width: 62, profile: [0.82, 1, 0.98, 0.9, 0.7], under: true, tint: ["#2dd4bf", 0.25, 0.75] },
      { pts: [[392, 306], [338, 380], [290, 440], [252, 478]], width: 44, profile: [0.65, 0.94, 1, 0.86, 0.45] },
      { pts: [[244, 474], [232, 650], [218, 840], [206, "bottom"]], width: 56, profile: [0.7, 0.96, 1, 1, 0.96], tint: ["#f5c451", 0.45, 0.8] },
    ],
  };

  const GLITTER = {
    light: ["#ffffff", "#fff1b8", "#ffd6ea", "#d4f4ff", "#e6dcff", "#ffc9a3"],
    dark: ["#ffcc33", "#ff4fa3", "#2dd4bf", "#7c5cff", "#f47b42", "#1f1f22", "#0a0a0b"],
  };

  // --- Geometry ---------------------------------------------------------

  function catmullRom(points, perSegment) {
    const out = [];
    const n = points.length;
    for (let i = 0; i < n - 1; i++) {
      const p0 = points[Math.max(0, i - 1)];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[Math.min(n - 1, i + 2)];
      for (let j = 0; j < perSegment; j++) {
        const t = j / perSegment;
        const t2 = t * t;
        const t3 = t2 * t;
        out.push([
          0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
          0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3),
        ]);
      }
    }
    out.push([points[n - 1][0], points[n - 1][1]]);
    return out;
  }

  // Re-samples a polyline every `spacing` pixels; t is the fraction of the length.
  function resample(poly, spacing) {
    const lengths = [0];
    for (let i = 1; i < poly.length; i++) {
      lengths.push(lengths[i - 1] + Math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1]));
    }
    const total = lengths[lengths.length - 1];
    const count = Math.max(2, Math.floor(total / spacing));
    const samples = [];
    let seg = 1;
    for (let i = 0; i <= count; i++) {
      const d = (i / count) * total;
      while (seg < poly.length - 1 && lengths[seg] < d) seg++;
      const span = lengths[seg] - lengths[seg - 1] || 1;
      const f = (d - lengths[seg - 1]) / span;
      samples.push({
        x: poly[seg - 1][0] + (poly[seg][0] - poly[seg - 1][0]) * f,
        y: poly[seg - 1][1] + (poly[seg][1] - poly[seg - 1][1]) * f,
        t: i / count,
      });
    }
    return samples;
  }

  function profileAt(profile, t) {
    const pos = t * (profile.length - 1);
    const i = Math.min(profile.length - 2, Math.floor(pos));
    const f = pos - i;
    return profile[i] + (profile[i + 1] - profile[i]) * f;
  }

  function layout(width, height) {
    const unit = height / 1000;
    const fit = Math.min(1, (width - 56) / (ART.width * unit));
    const px = unit * fit;
    const originY = ART.top * unit - ART.top * px;
    return {
      px,
      toX: (x) => width / 2 + x * px,
      toY: (y) => originY + y * px,
      bottomY: (height + 80 - originY) / px,
    };
  }

  function buildStroke(stroke, L, rng) {
    const pts = stroke.pts.map(([x, y]) => [L.toX(x), L.toY(y === "bottom" ? L.bottomY : y)]);
    const samples = resample(catmullRom(pts, 32), 2.2);
    const baseWidth = stroke.width * L.px;
    const amp = 1.4 * L.px;
    const phase = rng() * 10;
    for (let i = 0; i < samples.length; i++) {
      const s = samples[i];
      const prev = samples[Math.max(0, i - 1)];
      const next = samples[Math.min(samples.length - 1, i + 1)];
      let tx = next.x - prev.x;
      let ty = next.y - prev.y;
      const len = Math.hypot(tx, ty) || 1;
      tx /= len;
      ty /= len;
      s.nx = -ty;
      s.ny = tx;
      const wobble = Math.sin(s.t * 9 + phase) * amp + Math.sin(s.t * 23 + phase * 2) * amp * 0.45;
      s.x += s.nx * wobble;
      s.y += s.ny * wobble;
      s.w = baseWidth * profileAt(stroke.profile, s.t);
    }
    const avg = samples.reduce((sum, s) => sum + s.w, 0) / samples.length;
    return { stroke, samples, avg };
  }

  // --- Painting ---------------------------------------------------------

  function withAlpha(color, alpha) {
    const hex = color.replace("#", "");
    const full = hex.length === 3 ? hex.split("").map((c) => c + c).join("") : hex;
    const n = parseInt(full.slice(0, 6), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  function paintBody(ctx, samples, ink, scale, dx, dy, alpha) {
    ctx.globalAlpha = alpha;
    ctx.fillStyle = ink;
    ctx.beginPath();
    for (const s of samples) {
      ctx.moveTo(s.x + dx + (s.w * scale) / 2, s.y + dy);
      ctx.arc(s.x + dx, s.y + dy, (s.w * scale) / 2, 0, Math.PI * 2);
    }
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  // Strokes a track that follows the stroke at a fixed offset across the brush,
  // in continuous runs while `touching(t)` is true (a bristle lifting off the paper).
  function runs(ctx, samples, offset, spread, jitter, rng, touching) {
    let started = false;
    for (const p of samples) {
      if (!touching(p.t)) {
        if (started) ctx.stroke();
        started = false;
        continue;
      }
      const d = offset * p.w * 0.5 * spread + (rng() - 0.5) * jitter;
      const x = p.x + p.nx * d;
      const y = p.y + p.ny * d;
      if (started) {
        ctx.lineTo(x, y);
      } else {
        ctx.beginPath();
        ctx.moveTo(x, y);
        started = true;
      }
    }
    if (started) ctx.stroke();
  }

  // One track between `from` and `to`, stroked with a gradient that fades in and out.
  function fadedTrack(ctx, samples, offset, spread, from, to, color, strength, jitter, rng) {
    const pts = [];
    for (const p of samples) {
      if (p.t < from || p.t > to) continue;
      const d = offset * p.w * 0.5 * spread + (rng() - 0.5) * jitter;
      pts.push([p.x + p.nx * d, p.y + p.ny * d]);
    }
    if (pts.length < 2) return;
    const [x0, y0] = pts[0];
    const [x1, y1] = pts[pts.length - 1];
    const gradient = ctx.createLinearGradient(x0, y0, x1, y1);
    gradient.addColorStop(0, withAlpha(color, 0));
    gradient.addColorStop(0.5, withAlpha(color, strength));
    gradient.addColorStop(1, withAlpha(color, 0));
    ctx.strokeStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.stroke();
  }

  function paintBristles(ctx, built, ink, rng) {
    const { samples, avg } = built;
    const count = 22;
    ctx.strokeStyle = ink;
    ctx.lineCap = "round";
    for (let i = 0; i < count; i++) {
      const offset = -1 + ((i + rng()) * 2) / count;
      const edge = Math.abs(offset);
      ctx.globalAlpha = (0.35 + rng() * 0.45) * (1 - 0.5 * edge * edge);
      ctx.lineWidth = Math.max(0.5, (0.5 + rng() * 0.9) * (avg / count) * 1.8);
      const spread = 1 + rng() * 0.05 + edge * 0.03;
      let down = rng() > 0.25;
      runs(ctx, samples, offset, spread, 0.6, rng, (t) => {
        const lift = 0.015 + 0.08 * edge * edge * edge + 0.08 * Math.max(0, t - 0.75);
        if (down) {
          if (rng() < lift) down = false;
        } else if (rng() < 0.2) {
          down = true;
        }
        return down;
      });
    }
    ctx.globalAlpha = 1;
  }

  // Thin lighter lines inside the stroke, where the brush ran dry.
  function paintDryStreaks(ctx, built, rng) {
    const { samples, avg } = built;
    ctx.globalCompositeOperation = "destination-out";
    ctx.lineCap = "round";
    const count = 3 + Math.floor(rng() * 3);
    for (let i = 0; i < count; i++) {
      const offset = (rng() * 2 - 1) * 0.8;
      const from = rng() * 0.6;
      const to = Math.min(1, from + 0.2 + rng() * 0.5);
      ctx.lineWidth = Math.max(0.6, (0.015 + rng() * 0.035) * avg);
      fadedTrack(ctx, samples, offset, 0.95, from, to, "#000000", 0.2 + rng() * 0.3, 0.3, rng);
    }
    ctx.globalCompositeOperation = "source-over";
  }

  // A hint of colour: a soft sheen plus a few sharper pigment streaks, only where there is paint.
  function paintTints(ctx, builtStrokes, dark, rng, canFilter) {
    ctx.globalCompositeOperation = "source-atop";
    ctx.lineCap = "round";
    for (const built of builtStrokes) {
      const tint = built.stroke.tint;
      if (!tint) continue;
      const [color, from, to] = tint;
      const span = to - from;
      if (canFilter) ctx.filter = "blur(3px)";
      for (let i = 0; i < 2; i++) {
        const start = from + rng() * span * 0.25;
        const stop = Math.min(1, start + span * (0.6 + rng() * 0.4));
        ctx.lineWidth = Math.max(1, (0.2 + rng() * 0.15) * built.avg);
        fadedTrack(ctx, built.samples, (rng() * 2 - 1) * 0.45, 0.8, start, stop, color, dark ? 0.22 + rng() * 0.1 : 0.28 + rng() * 0.12, 0, rng);
      }
      if (canFilter) ctx.filter = "blur(0.5px)";
      for (let i = 0; i < 4; i++) {
        const start = from + rng() * span * 0.4;
        const stop = Math.min(1, start + span * (0.35 + rng() * 0.5));
        ctx.lineWidth = Math.max(0.6, (0.025 + rng() * 0.04) * built.avg);
        fadedTrack(ctx, built.samples, (rng() * 2 - 1) * 0.75, 0.9, start, stop, color, dark ? 0.45 + rng() * 0.2 : 0.55 + rng() * 0.3, 0.2, rng);
      }
    }
    if (canFilter) ctx.filter = "none";
    ctx.globalCompositeOperation = "source-over";
  }

  function makeGlitter(builtStrokes, rng) {
    const items = [];
    for (const { samples, stroke } of builtStrokes) {
      const count = Math.round(samples.length * (stroke.width / 50) * 0.05);
      for (let i = 0; i < count; i++) {
        const p = samples[Math.floor(rng() * samples.length)];
        const u = (rng() * 2 - 1) * 0.8;
        items.push({
          x: p.x + p.nx * u * p.w * 0.5,
          y: p.y + p.ny * u * p.w * 0.5,
          r: 0.8 + rng() * 1.7,
          hue: rng(),
          phase: rng() * Math.PI * 2,
          speed: 0.5 + rng() * 1.5,
          star: rng() < 0.2,
        });
      }
    }
    return items;
  }

  function drawGlitter(ctx, width, height, items, colors, time, animate) {
    ctx.clearRect(0, 0, width, height);
    ctx.lineCap = "round";
    for (const g of items) {
      const twinkle = animate ? 0.2 + 0.8 * (0.5 + 0.5 * Math.sin(time * 0.0018 * g.speed + g.phase)) : 0.85;
      const color = colors[Math.floor(g.hue * colors.length)];
      if (g.star) {
        const size = g.r * 5 * (0.5 + 0.5 * twinkle);
        const glow = ctx.createRadialGradient(g.x, g.y, 0, g.x, g.y, size);
        glow.addColorStop(0, withAlpha(color, 0.55 * twinkle));
        glow.addColorStop(1, withAlpha(color, 0));
        ctx.globalAlpha = 1;
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(g.x, g.y, size, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = glow;
        ctx.lineWidth = 1.3;
        ctx.beginPath();
        ctx.moveTo(g.x - size, g.y);
        ctx.lineTo(g.x + size, g.y);
        ctx.moveTo(g.x, g.y - size);
        ctx.lineTo(g.x, g.y + size);
        ctx.stroke();
        ctx.globalAlpha = twinkle;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(g.x, g.y, g.r * 0.9, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.globalAlpha = twinkle;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(g.x, g.y, g.r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
  }

  // --- Orchestration ----------------------------------------------------

  const state = { items: null, colors: null, width: 0, height: 0, raf: 0, last: 0, visible: true };

  function paint() {
    const width = hero.clientWidth;
    const height = hero.clientHeight;
    if (!width || !height) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const dark = darkScheme.matches;
    const ink = getComputedStyle(document.documentElement).getPropertyValue("--ink").trim() || (dark ? "#f5f5f7" : "#0b0b0c");

    for (const canvas of [paintCanvas, glitterCanvas]) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = paintCanvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const rng = mulberry32(20261003);
    const L = layout(width, height);
    const built = ART.strokes.map((stroke) => buildStroke(stroke, L, rng));

    const canFilter = typeof ctx.filter === "string";
    if (canFilter) ctx.filter = "blur(1.5px)";
    for (const b of built) {
      if (b.stroke.under) paintBody(ctx, b.samples, ink, 1.35, 4 * L.px, 5 * L.px, dark ? 0.13 : 0.2);
    }
    for (const b of built) {
      if (canFilter) ctx.filter = "blur(0.4px)";
      paintBody(ctx, b.samples, ink, 1, 0, 0, 1);
      if (canFilter) ctx.filter = "none";
      paintBristles(ctx, b, ink, rng);
      paintDryStreaks(ctx, b, rng);
    }
    paintTints(ctx, built, dark, rng, canFilter);

    state.items = makeGlitter(built, rng);
    state.colors = dark ? GLITTER.dark : GLITTER.light;
    state.width = width;
    state.height = height;
    const gctx = glitterCanvas.getContext("2d");
    gctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawGlitter(gctx, width, height, state.items, state.colors, 0, false);
    schedule();
  }

  function animating() {
    return Boolean(state.items) && state.visible && !document.hidden && !reduceMotion.matches;
  }

  function frame(now) {
    state.raf = 0;
    if (!animating()) return;
    if (now - state.last >= 40) {
      state.last = now;
      const gctx = glitterCanvas.getContext("2d");
      drawGlitter(gctx, state.width, state.height, state.items, state.colors, now, true);
    }
    state.raf = window.requestAnimationFrame(frame);
  }

  function schedule() {
    if (animating()) {
      if (!state.raf) state.raf = window.requestAnimationFrame(frame);
    } else if (state.items) {
      const gctx = glitterCanvas.getContext("2d");
      drawGlitter(gctx, state.width, state.height, state.items, state.colors, 0, false);
    }
  }

  function safePaint() {
    try {
      paint();
      hero.removeAttribute("data-brush-failed");
    } catch (error) {
      hero.setAttribute("data-brush-failed", "");
      console.error("MyTaste: could not paint the brush strokes", error);
    }
  }

  if (hero && paintCanvas && glitterCanvas && paintCanvas.getContext) {
    safePaint();

    let resizeTimer = 0;
    const observer = new ResizeObserver(() => {
      if (hero.clientWidth === state.width && hero.clientHeight === state.height) return;
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(safePaint, 120);
    });
    observer.observe(hero);

    darkScheme.addEventListener("change", safePaint);
    reduceMotion.addEventListener("change", schedule);
    document.addEventListener("visibilitychange", schedule);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => {
        state.visible = entries.some((entry) => entry.isIntersecting);
        schedule();
      }).observe(hero);
    }
  }

  // --- Top bar ----------------------------------------------------------

  const topbar = document.querySelector("[data-topbar]");
  if (topbar) {
    const update = () => topbar.classList.toggle("is-scrolled", window.scrollY > 12);
    window.addEventListener("scroll", update, { passive: true });
    update();
  }

  // --- GitHub stars -----------------------------------------------------

  const stars = document.querySelector("[data-stars]");
  if (stars && "fetch" in window) {
    const key = "mytaste-stars";
    const show = (count) => {
      if (count <= 0) return;
      stars.textContent = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(count);
      stars.setAttribute("aria-label", `${count} stars on GitHub`);
      stars.hidden = false;
    };
    let cached = null;
    try {
      cached = JSON.parse(window.sessionStorage.getItem(key) || "null");
    } catch {
      cached = null;
    }
    if (cached && typeof cached.count === "number" && Date.now() - cached.at < 3600000) {
      show(cached.count);
    } else {
      fetch("https://api.github.com/repos/RUverse/my-taste", { headers: { Accept: "application/vnd.github+json" } })
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (!data || typeof data.stargazers_count !== "number") return;
          show(data.stargazers_count);
          try {
            window.sessionStorage.setItem(key, JSON.stringify({ count: data.stargazers_count, at: Date.now() }));
          } catch {
            /* storage may be unavailable */
          }
        })
        .catch(() => {});
    }
  }
})();
