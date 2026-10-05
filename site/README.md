# MyTaste website

The landing page for MyTaste: a static site with no build step and no dependencies.

- `index.html` — the hero ("My" with the `.taste` wordmark), the top bar, and one section per
  page (Media Server, Share Collections, .taste format, Login, Self-host).
- `styles.css` — layout, the curtain transition, and the light and dark themes, which follow
  the OS setting.
- `main.js` — builds the folded glass, lets the cursor push the nearest folds aside, and opens
  pages from the URL hash (`#media-server`, `#login`, …).
- `screenshots/` — the app in light and dark (WebP, 1440×900), shown below each page's text.
  They were taken from a throwaway instance with demo data; never use a real library or
  account.
- `fonts/` — Space Grotesk (latin subset, SIL Open Font License, see `fonts/OFL.txt`).
- `favicon.svg` — the icon.

The background is two rows of folded glass hanging from the top bar, like a curtain: one
behind the hero and one in front of it, parted around the hero's middle. Each row is panels
side by side whose tops zigzag between ridges and valleys, each face shaded from its ridge to
its valley, with a thin highlight along the ridges; the front row is frosted. The layout is
seeded, so it is the same on every visit, and sizes are in pixels, so phones show fewer folds
rather than narrower ones. The cursor moves each fold edge on its own and stretches the panels
between them, so the folds compress and spread without coming apart. The "My" stands behind
the front glass and the text in front of it.

The "My" is an SVG whose gradient turns slowly around the colour wheel and glows in the same
colour. Opening a page slides the two front halves off either side while panels sweep in from
the right and cover the hero, ending with a solid sheet behind the page text; a small copy of
the logo is wiped in at the top left (`--dock-*` in `styles.css`). The page scrolls on its own
layer over the curtain. Escape, the browser's back button, or a click on the small logo
returns home. `prefers-reduced-motion` turns off the hue cycle, the cursor effect, and the
slides. Without JavaScript the pages are plain sections below the hero.

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
