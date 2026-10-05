"use strict";

(() => {
  const body = document.body;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // A seeded generator keeps the band layout the same on every visit.
  let seed = 20261005;
  const random = () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const between = (min, max) => min + (max - min) * random();

  // Everything the cursor can nudge: { el, edge(), offset, target }.
  const movers = [];

  // Background bands: semi-transparent strips that overlap into a banding pattern.
  const bandLayer = document.querySelector(".bands");
  const addBand = (x, width, alpha, gap) => {
    const el = document.createElement("div");
    el.className = gap ? "band is-gap" : "band";
    el.style.setProperty("--x", `${x.toFixed(2)}%`);
    el.style.setProperty("--w", `${width.toFixed(2)}%`);
    el.style.setProperty("--a", alpha.toFixed(3));
    bandLayer.append(el);
    movers.push({ el, edge: () => el.offsetLeft + el.offsetWidth / 2, offset: 0, target: 0 });
  };
  // One band per slot keeps them spread across the page; the jitter makes them overlap.
  for (let x = 11; x < 100; x += 6.5) {
    addBand(x + between(-3, 3), between(3, 13), between(0.03, 0.08), false);
  }
  for (let x = 14; x < 100; x += 21) {
    addBand(x + between(0, 14), between(0.2, 3), between(0.5, 0.85), true);
  }

  // Curtain panels: offset from the curtain's edge (vw), shade, opacity, and stagger.
  const curtain = document.querySelector(".curtain");
  const panels = [
    [0, 2, 0.55],
    [2.2, 1, 0.8],
    [4.5, 3, 0.88],
    [9, 1, 0.6],
    [15, 4, 0.55],
    [23, 2, 0.5],
    [31, 3, 0.55],
    [41, 4, 0.45],
  ];
  panels.forEach(([offset, shade, alpha], index) => {
    const el = document.createElement("div");
    el.className = "panel";
    el.style.setProperty("--o", `${offset}vw`);
    el.style.setProperty("--open-delay", `${index * 0.035}s`);
    el.style.setProperty("--close-delay", `${(panels.length - index) * 0.03}s`);
    el.style.setProperty("--wave", `${-(2 + index * 0.6).toFixed(1)}vw`);
    const fill = document.createElement("span");
    fill.style.setProperty("--shade", `var(--shade-${shade})`);
    fill.style.setProperty("--a", String(alpha));
    el.append(fill);
    curtain.append(el);
    movers.push({ el: fill, edge: () => el.offsetLeft, offset: 0, target: 0 });
  });

  // The cursor pushes the closest rectangles a little to either side.
  const RADIUS = 150;
  const MAX_PUSH = 22;
  let edges = [];
  let frame = 0;
  const measure = () => {
    edges = movers.map((mover) => mover.edge());
  };
  const step = () => {
    let moving = false;
    for (const mover of movers) {
      const delta = mover.target - mover.offset;
      if (Math.abs(delta) > 0.05) {
        mover.offset += delta * 0.12;
        moving = true;
      } else {
        mover.offset = mover.target;
      }
      mover.el.style.transform = mover.offset ? `translate3d(${mover.offset.toFixed(2)}px,0,0)` : "";
    }
    frame = moving ? requestAnimationFrame(step) : 0;
  };
  const aim = (x) => {
    movers.forEach((mover, index) => {
      if (x === null) {
        mover.target = 0;
        return;
      }
      const u = (edges[index] - x) / RADIUS;
      // u·e^(−u²) peaks at u ≈ 0.707 with 0.429, and passes smoothly through 0 at the cursor.
      mover.target = (MAX_PUSH / 0.429) * u * Math.exp(-u * u);
    });
    if (!frame) frame = requestAnimationFrame(step);
  };
  const onPointer = (event) => {
    if (reduceMotion.matches || event.pointerType === "touch") return;
    aim(event.clientX);
  };
  measure();
  window.addEventListener("resize", measure);
  document.addEventListener("pointermove", onPointer, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => aim(null));
  reduceMotion.addEventListener("change", () => aim(null));

  // Pages: the hash names the open page; an empty hash is the home view.
  const pages = new Map([...document.querySelectorAll(".page")].map((page) => [page.id, page]));
  const pageLinks = document.querySelectorAll("a[data-page]");
  let current = null;
  let switchTimer = 0;

  const show = (id, { focus = true } = {}) => {
    const next = pages.has(id) ? id : null;
    if (next === current) return;
    if (current && next) {
      body.classList.add("is-switching");
      window.clearTimeout(switchTimer);
      switchTimer = window.setTimeout(() => body.classList.remove("is-switching"), 320);
    }
    body.classList.toggle("is-page", next !== null);
    pages.forEach((page, key) => {
      page.classList.toggle("is-active", key === next);
      page.inert = key !== next;
    });
    pageLinks.forEach((link) => {
      if (link.closest(".nav") && link.dataset.page === next) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    const wasOpen = current !== null;
    current = next;
    if (focus && next) {
      pages.get(next).querySelector("h2").focus({ preventScroll: true });
    } else if (focus && wasOpen) {
      document.querySelector(".mark-link").focus({ preventScroll: true });
    }
    // The fixed layers never scroll the document, but the browser's jump to the anchor may.
    window.scrollTo(0, 0);
  };

  const fromHash = () => decodeURIComponent(window.location.hash.slice(1)) || null;
  window.addEventListener("hashchange", () => show(fromHash()));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && current) window.location.hash = "";
  });

  // Open a linked page without animating the curtain in.
  body.classList.add("no-anim");
  show(fromHash(), { focus: false });
  requestAnimationFrame(() => requestAnimationFrame(() => body.classList.remove("no-anim")));
})();
