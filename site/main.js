"use strict";

(() => {
  const body = document.body;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // Seeded generators keep the layout the same on every visit.
  const seeded = (start) => {
    let seed = start;
    return () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  };

  // Two rows of folded glass hang from the top bar: one behind the hero, and one in front of
  // it that is parted around the hero's middle, like a curtain opened for it. Sizes are in
  // pixels, so phones get the same folds as desktops, only fewer of them.
  const hero = document.querySelector(".hero");
  const back = document.querySelector(".glass-back");
  const frontLeft = document.querySelector(".half-left");
  const frontRight = document.querySelector(".half-right");
  const ROWS = {
    back: { seed: 11, minW: 110, maxW: 190, depth: 34 },
    left: { seed: 23, minW: 70, maxW: 130, depth: 22 },
    right: { seed: 37, minW: 70, maxW: 130, depth: 22 },
  };

  // A row is panels side by side between fold edges (page pixels). The tops zigzag between
  // ridges, just under the bar, and valleys lower down; each face is shaded from its ridge to
  // its valley, so the row reads as one sheet folded like an accordion.
  const buildRow = (layer, from, to, { seed, minW, maxW, depth }) => {
    const random = seeded(seed);
    const between = (min, max) => min + (max - min) * random();
    let edges = [from];
    while (edges[edges.length - 1] < to) edges.push(edges[edges.length - 1] + between(minW, maxW));
    // Stretch the folds a little so the last edge lands exactly on `to`.
    const scale = (to - from) / (edges[edges.length - 1] - from);
    edges = edges.map((x) => from + (x - from) * scale);
    const heights = edges.map((_, i) => (i % 2 ? between(depth * 0.65, depth) : between(0, 5)));
    const panels = [];
    for (let i = 0; i < edges.length - 1; i += 1) {
      const el = document.createElement("div");
      el.className = heights[i] < heights[i + 1] ? "fold falls" : "fold rises";
      el.style.setProperty("--x", `${edges[i].toFixed(1)}px`);
      el.style.setProperty("--w", `${(edges[i + 1] - edges[i]).toFixed(1)}px`);
      el.style.setProperty("--sl", `${heights[i].toFixed(1)}px`);
      el.style.setProperty("--sr", `${heights[i + 1].toFixed(1)}px`);
      panels.push(el);
    }
    layer.replaceChildren(...panels);
    // The frosted front halves blur what is behind them in one pass, shaped like their folds.
    if (layer !== back) {
      const top = edges.map((x, i) => `${x.toFixed(1)}px calc(var(--row-top) + ${heights[i].toFixed(1)}px)`);
      layer.style.clipPath = `polygon(${top.join(", ")}, ${to.toFixed(1)}px 100%, ${from.toFixed(1)}px 100%)`;
    }
    return { layer, edges, panels, offsets: edges.map(() => 0), targets: edges.map(() => 0) };
  };

  let rows = [];
  const layout = () => {
    // offsetLeft ignores transforms, so this is the hero's resting place.
    const middle = hero.offsetLeft + hero.offsetWidth / 2;
    const opening = hero.offsetWidth * 0.16;
    const width = window.innerWidth;
    rows = [
      buildRow(back, -40, width + 40, ROWS.back),
      buildRow(frontLeft, -40, middle - opening, ROWS.left),
      buildRow(frontRight, middle + opening, width + 40, ROWS.right),
    ];
    // How far each half slides to clear the screen when a page opens.
    frontLeft.style.setProperty("--out", `${-(middle - opening + 80)}px`);
    frontRight.style.setProperty("--out", `${width - (middle + opening) + 80}px`);
  };
  layout();
  window.addEventListener("resize", layout);

  // The cursor pushes the nearest folds aside. Each fold edge moves on its own, and every panel
  // is stretched between its two edges, so the folds compress and spread without coming apart.
  const RADIUS = 150;
  const MAX_PUSH = 22;
  let frame = 0;
  const step = () => {
    let moving = false;
    for (const row of rows) {
      row.offsets = row.offsets.map((offset, i) => {
        const delta = row.targets[i] - offset;
        if (Math.abs(delta) < 0.05) return row.targets[i];
        moving = true;
        return offset + delta * 0.12;
      });
      row.panels.forEach((panel, i) => {
        const left = row.offsets[i];
        const right = row.offsets[i + 1];
        const width = row.edges[i + 1] - row.edges[i];
        panel.style.transform =
          left || right ? `translate3d(${left.toFixed(2)}px,0,0) scaleX(${((width + right - left) / width).toFixed(4)})` : "";
      });
    }
    frame = moving ? requestAnimationFrame(step) : 0;
  };
  const aim = (x) => {
    for (const row of rows) {
      // The front halves are off screen while a page is open.
      const still = x === null || (row.layer !== back && body.classList.contains("is-page"));
      row.targets = row.edges.map((edge) => {
        if (still) return 0;
        const u = (edge - x) / RADIUS;
        // u·e^(−u²) peaks at u ≈ 0.707 with 0.429, and passes smoothly through 0 at the cursor.
        return (MAX_PUSH / 0.429) * u * Math.exp(-u * u);
      });
    }
    if (!frame) frame = requestAnimationFrame(step);
  };
  const onPointer = (event) => {
    if (reduceMotion.matches || event.pointerType === "touch") return;
    aim(event.clientX);
  };
  document.addEventListener("pointermove", onPointer, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => aim(null));
  reduceMotion.addEventListener("change", () => aim(null));

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
  });

  // Pages: the hash names the open page; an empty hash is the home view.
  const pages = new Map([...document.querySelectorAll(".page")].map((page) => [page.id, page]));
  const pageLinks = document.querySelectorAll("a[data-page]");
  const scroller = document.querySelector(".pages");
  const mini = document.querySelector(".mini");
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
    // Only one logo is ever reachable: the hero at home, the small one on a page.
    hero.inert = next !== null;
    mini.inert = next === null;
    const wasOpen = current !== null;
    current = next;
    scroller.scrollTop = 0;
    if (focus && next) {
      pages.get(next).querySelector("h2").focus({ preventScroll: true });
    } else if (focus && wasOpen) {
      // The hero is still covered for a moment; focus it once the curtain has moved.
      requestAnimationFrame(() => document.querySelector(".mark-link").focus({ preventScroll: true }));
    }
    // The fixed layers never scroll the document, but the browser's jump to the anchor may.
    window.scrollTo(0, 0);
  };

  const fromHash = () => decodeURIComponent(window.location.hash.slice(1)) || null;
  window.addEventListener("hashchange", () => show(fromHash()));
  // Page links update the URL themselves: following the anchor would scroll the target
  // section into view, starting the page halfway down.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-page], .mark-link, .mini");
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
