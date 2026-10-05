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

  // Bands: semi-transparent strips that overlap into a banding pattern.
  const backLayer = document.querySelector(".bands");
  const frontLayer = document.querySelector(".bands-front");
  const bands = [];
  const addBand = (x, width, alpha, gap) => {
    const el = document.createElement("div");
    el.className = gap ? "band is-gap" : "band";
    el.style.setProperty("--x", `${Math.round(x)}px`);
    el.style.setProperty("--w", `${Math.round(width)}px`);
    el.style.setProperty("--a", alpha.toFixed(3));
    backLayer.append(el);
    bands.push({ el, gap, center: x + width / 2 });
    movers.push({
      el,
      // Both layers start at the same left edge.
      edge: () => backLayer.offsetLeft + el.offsetLeft + el.offsetWidth / 2,
      offset: 0,
      target: 0,
    });
  };
  // Sizes are in pixels so phones get the same rectangles as desktops, only fewer of them.
  // One band per slot keeps them spread out; the jitter makes them overlap.
  const SPAN = 3840;
  for (let x = 0; x < SPAN; x += 120) {
    addBand(x + between(-45, 45), between(80, 260), between(0.03, 0.08), false);
  }
  for (let x = 40; x < SPAN; x += 300) {
    addBand(x + between(0, 200), between(3, 45), between(0.5, 0.85), true);
  }

  // Every band crossing the hero sits in front of it except the few nearest its middle, so the
  // hero reads as standing among them. The thin gaps stay behind; in front they cut the letters.
  const hero = document.querySelector(".hero");
  const layerBands = () => {
    // offsetLeft ignores the hero's transform, so this is its home position even on a page.
    const middle = hero.offsetLeft + hero.offsetWidth / 2 - backLayer.offsetLeft;
    const keepBehind = hero.offsetWidth * 0.17;
    for (const band of bands) {
      const front = !band.gap && Math.abs(band.center - middle) > keepBehind;
      const layer = front ? frontLayer : backLayer;
      if (band.el.parentElement !== layer) layer.append(band.el);
    }
  };
  layerBands();
  window.addEventListener("resize", layerBands);

  // On a page the hero shrinks into the corner. Its target comes from the CSS (a hidden probe
  // placed at --dock-left/--dock-top/--dock-w); offsetLeft/Top ignore the hero's transform.
  const probe = document.querySelector(".dock-probe");
  const dock = () => {
    const target = probe.getBoundingClientRect();
    hero.style.setProperty("--dock-x", `${(target.left - hero.offsetLeft).toFixed(1)}px`);
    hero.style.setProperty("--dock-y", `${(target.top - hero.offsetTop).toFixed(1)}px`);
    hero.style.setProperty("--dock-s", (target.width / hero.offsetWidth).toFixed(4));
  };
  dock();
  window.addEventListener("resize", dock);
  // The hero's height, and so its centred position, changes once the web font arrives.
  document.fonts?.ready.then(dock);

  // Curtain panels: offset from the curtain's edge (px), shade, opacity, and stagger.
  const curtain = document.querySelector(".curtain");
  const panels = [
    [0, 2, 0.55],
    [36, 1, 0.8],
    [72, 3, 0.88],
    [150, 1, 0.6],
    [250, 4, 0.55],
    [370, 2, 0.5],
    [500, 3, 0.55],
    [660, 4, 0.45],
  ];
  panels.forEach(([offset, shade, alpha], index) => {
    const el = document.createElement("div");
    el.className = "panel";
    el.style.setProperty("--o", `${offset}px`);
    el.style.setProperty("--open-delay", `${index * 0.03}s`);
    el.style.setProperty("--close-delay", `${(panels.length - index) * 0.02}s`);
    const fill = document.createElement("span");
    fill.style.setProperty("--shade", `var(--shade-${shade})`);
    fill.style.setProperty("--a", String(alpha));
    el.append(fill);
    // The solid sheet behind the page text comes last, on top of the panels.
    curtain.insertBefore(el, curtain.querySelector(".sheet"));
    // Panels slide in and out, so their edge is read where it is now. Only the inner fill
    // takes the cursor's push, so the outer box's position is unaffected by it.
    movers.push({ el: fill, edge: () => el.getBoundingClientRect().left, live: true, offset: 0, target: 0 });
  });

  // The cursor pushes the closest rectangles a little to either side.
  const RADIUS = 150;
  const MAX_PUSH = 22;
  let edges = [];
  let frame = 0;
  const measure = () => {
    edges = movers.map((mover) => mover.edge());
  };
  const measureLive = () => {
    movers.forEach((mover, index) => {
      if (mover.live) edges[index] = mover.edge();
    });
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
    measureLive();
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
  const scroller = document.querySelector(".pages");
  let current = null;

  const show = (id, { focus = true } = {}) => {
    const next = pages.has(id) ? id : null;
    if (next === current) return;
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
    scroller.scrollTop = 0;
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
  // Page links update the URL themselves: following the anchor would scroll the target
  // section into view, starting the page halfway down.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-page], .mark-link");
    if (!link || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const id = link.dataset.page || null;
    if (id === current) return;
    window.history.pushState(null, "", id ? `#${id}` : window.location.pathname + window.location.search);
    show(id);
  });
  // Back and Forward also scroll to the section; reset that once the browser has done it.
  window.history.scrollRestoration = "manual";
  window.addEventListener("popstate", () => {
    show(fromHash());
    requestAnimationFrame(() => {
      scroller.scrollTop = 0;
    });
  });
  const goHome = () => {
    if (!current) return;
    window.history.pushState(null, "", window.location.pathname + window.location.search);
    show(null);
  };
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") goHome();
  });
  // The scrolling layer covers the docked logo, so clicks there are passed on to it.
  const overLogo = (event) => {
    const box = hero.querySelector(".mark-svg").getBoundingClientRect();
    return (
      event.target === scroller &&
      event.clientX >= box.left &&
      event.clientX <= box.right &&
      event.clientY >= box.top &&
      event.clientY <= box.bottom
    );
  };
  scroller.addEventListener("pointermove", (event) => {
    scroller.style.cursor = overLogo(event) ? "pointer" : "";
  });
  scroller.addEventListener("click", (event) => {
    if (current && overLogo(event)) {
      goHome();
    }
  });

  // The browser scrolls a linked page's section into view once the document loads; undo it.
  window.addEventListener(
    "load",
    () => {
      scroller.scrollTop = 0;
      window.scrollTo(0, 0);
    },
    { once: true },
  );

  // Open a linked page without animating the curtain in.
  body.classList.add("no-anim");
  show(fromHash(), { focus: false });
  requestAnimationFrame(() => requestAnimationFrame(() => body.classList.remove("no-anim")));
})();
