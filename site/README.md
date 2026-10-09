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
pixels, so phones show fewer rectangles rather than narrower ones. The rectangles crossing
the hero sit in front of it except the few nearest its middle, so the hero stands among them, and each one darkens
towards its left side like a shadow. They hang from just under the top bar with slanted top
edges, like a curtain. The "My" is an SVG whose
gradient turns slowly around the colour wheel and glows in the same colour. Opening a page
sweeps more rectangles in from the right like a curtain until the hero is hidden, ending with
a solid sheet behind the page text. At the same time a few opaque rectangles in the top-left
corner slide off the screen, uncovering a small copy of the logo that was behind them all
along (`--dock-*` in `styles.css`). The page scrolls on its own layer over the curtain. Escape, the browser's back
button, or a click on the small logo returns home. `prefers-reduced-motion` turns off the hue cycle, the cursor
effect, and the slides. Without JavaScript the pages are plain sections below the hero.

The Login page's **Join the waitlist** button opens a dialog (`<dialog class="waitlist">`)
whose form posts JSON to the MyTaste Hub (`https://hub.mytaste.cc/api/waitlist`, the form's
`action`), which keeps the list; the hub only accepts it from `mytaste.cc` and
`www.mytaste.cc`. The answers' values (`me`, `household`, …, `streaming`, `games`, …) must
match the ones the hub accepts. To try the form locally, run a hub with
`HUB_WAITLIST_ORIGINS` set to the preview's address and point the `action` at it.

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
