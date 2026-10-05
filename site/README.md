# MyTaste website

The landing page for MyTaste: a static site with no build step and no dependencies.

- `index.html` — the hero ("My" with the `.taste` wordmark), the top bar, and one section per
  page (Media Server, Share Collections, .taste format, Login, Self-host).
- `styles.css` — layout, the curtain transition, and the light and dark themes, which follow
  the OS setting.
- `main.js` — draws the background bands, nudges the bands closest to the cursor, and opens
  pages from the URL hash (`#media-server`, `#login`, …).
- `screenshots/` — the app in light and dark (WebP, 1440×900), shown below each page's text.
  They were taken from a throwaway instance with demo data; never use a real library or
  account.
- `fonts/` — Space Grotesk (latin subset, SIL Open Font License, see `fonts/OFL.txt`).
- `favicon.svg` — the icon.

The background is a set of semi-transparent vertical rectangles whose overlaps make the
banding; their layout is seeded, so it is the same on every visit, and their sizes are in
pixels, so phones show fewer rectangles rather than narrower ones. Most rectangles sit in
front of the hero and a few behind it, so the hero stands among them, and each one darkens
towards its left side like a shadow. The "My" is an SVG whose
gradient turns slowly around the colour wheel. Opening a page slides the hero aside while
more rectangles close over the middle like a curtain, then keep closing slowly until most of
the hero is hidden. The page scrolls on its own layer over the curtain. Escape, the browser's back button, or a
click on the hero returns home. `prefers-reduced-motion` turns off the hue cycle, the cursor
effect, and the slides. Without JavaScript the pages are plain sections below the hero.

## Preview

```bash
python3 -m http.server -d site 4173
```

Then open <http://127.0.0.1:4173>. Any static host can serve the folder as is; keep the asset
paths relative so it also works from a subdirectory.

## Checks

```bash
node --check site/main.js
```

For visual changes, look at the page at desktop and mobile widths in both light and dark OS
themes, with and without reduced motion.
