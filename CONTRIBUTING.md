# Contributing to MyTaste

Thanks for helping out. This guide covers how branches, pull requests, and releases work. For the
architecture and coding conventions, read [AGENTS.md](AGENTS.md); for running the app, read the
[README](README.md).

## Branches

| Branch | Purpose | Who writes to it |
| --- | --- | --- |
| `main` | Released code. Its latest commit is always the latest release. | The release process only |
| `dev` | Integration branch for the next release. | Pull requests only |
| `feature/…`, `fix/…`, `docs/…`, `chore/…` | One change each, branched from `dev`. | You |

The rules:

- Always branch from `dev`, never from `main`.
- Always open pull requests against `dev`. A pull request against `main` will be asked to
  retarget `dev`.
- Never push directly to `dev` or `main`.
- Keep a branch to one topic. Unrelated fixes go in their own branch and pull request.

## Making a change

```bash
git fetch origin
git switch -c feature/short-description origin/dev

# ...edit, then run the checks below...

git push -u origin feature/short-description
gh pr create --base dev
```

If `dev` moves while your branch is open, rebase onto it rather than merging it in:

```bash
git fetch origin
git rebase origin/dev
git push --force-with-lease
```

## Before opening a pull request

Run the same checks the maintainers run:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
node --check src/mytaste/web/static/app.js
node --check src/mytaste/web/static/player.js
```

Also:

- Add or update tests for behavior changes. Tests must not call TMDB or other live services; use
  the test doubles injected through `create_app()`.
- Run `uv build` when packaging, templates, or static assets change.
- For UI changes, check the real page at desktop and mobile widths in both light and dark OS
  themes, and include screenshots in the pull request.
- When changing an existing SQLite table, add a migration that upgrades existing databases.
  `CREATE TABLE IF NOT EXISTS` does not alter a table that already exists.
- Update the README or `docs/` when user-visible behavior or configuration changes.
- Never commit `.env`, TMDB tokens, SQLite databases, or details of a particular host (hostnames,
  IP addresses, personal media paths). This repository is public.

## Pull requests

- Write a title that says what changes for the user, and a description that explains why and how
  it was tested.
- A pull request is merged into `dev` once the checks pass and it has been reviewed.
- Pull requests are squash-merged, so `dev` gets one commit per pull request. The pull request
  title becomes the commit subject.

## Releases

Releases move `dev` into `main`; nothing else changes `main`.

A release workflow that automates this is planned. It will merge `dev` into `main`, tag the
version from `pyproject.toml`, and publish a GitHub release with notes built from the pull
requests merged since the last release. Until it exists, a maintainer does the same by hand:

1. On a branch from `dev`, bump `version` in `pyproject.toml` and open a pull request to `dev`.
2. After it is merged, fast-forward `main` to `dev` (`git merge --ff-only origin/dev` on `main`)
   so both branches share the same commits.
3. Tag the release as `vX.Y.Z` and publish a GitHub release from that tag.

Do not squash `dev` into `main`: squashing would make `main` and `dev` diverge.
