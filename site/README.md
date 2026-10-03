# MyTaste website

The landing page for MyTaste: a static site with no build step and no dependencies.

- `index.html` — the page: hero, features, how it works, footer.
- `styles.css` — styles; dark mode follows the OS theme.
- `main.js` — paints the "MY" brush strokes and their glitter on a canvas, tints the top bar
  on scroll, and shows the repository's star count (fetched from the GitHub API, cached for
  an hour; the page works without it).
- `favicon.svg` — the icon.

The ".taste" wordmark is a set of hand-drawn SVG paths that write themselves in; the glasses in
the background are CSS gradients. Both the writing and the glitter respect
`prefers-reduced-motion`.

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
themes.
