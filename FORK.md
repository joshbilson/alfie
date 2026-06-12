# FORK.md — alfie fork of open-webui

This repository is the **Alfie** fork of
[open-webui/open-webui](https://github.com/open-webui/open-webui), maintained
for the self-hosted, phone-first agentic coding stack on the happypixels VPS
(v4). It tracks upstream **release tags** with a thin layer of alfie-specific
changes on top.

The authoritative fork-development methodology lives in the stack repo at
`docs/DEVELOPMENT.md` (the **churn map** for hot files). This file is the
fork-local quick reference.

## Remotes

```
origin    https://github.com/joshbilson/alfie.git        # this fork (default branch: main)
upstream  https://github.com/open-webui/open-webui.git   # source project
```

```
git remote add upstream https://github.com/open-webui/open-webui.git
git fetch upstream --tags --filter=blob:none
```

## Sync discipline (hard rules)

- **Merge upstream RELEASE TAGS only** — never the upstream `dev` branch or
  `main` tip. Pick the newest stable patch tag; avoid an `x.y.0` released under
  a week ago.
- **Always merge, never rebase, published `main`.** After the one-time baseline
  reset recorded below, `main` is never force-pushed or rebased again.
- **Custom code lives in new modules.** Hot/churn-map files
  (`utils/middleware.py`, `main.py`, `Chat.svelte`, `config.py`, i18n locales)
  get only 1–3-line wiring diffs — never large edits. See the stack repo's
  `docs/DEVELOPMENT.md` churn map before editing any hot file.

To take a new upstream release:

```
git fetch upstream --tags --filter=blob:none
git checkout main
git merge vX.Y.Z          # the chosen upstream release tag — merge, never rebase
# resolve any conflicts in the thin alfie wiring diffs, then:
git push origin main
```

## Versioning & combined tags

- Root `package.json` `version` carries an alfie suffix:
  `<upstream-version>-alfie.<N>` (e.g. `0.9.6-alfie.1`).
- Combined release tags are `vX.Y.Z-alfie.N` where `vX.Y.Z` is the upstream
  release the fork is built on and `N` increments per alfie release on that
  base.

## Release flow

1. Land the alfie diffs on `main` (or merge a new upstream release tag).
2. Bump `package.json` `version` to `X.Y.Z-alfie.N`.
3. Tag `vX.Y.Z-alfie.N` and push the tag — this triggers
   `.github/workflows/docker.yaml`.
4. CI builds a multi-arch (amd64 + arm64) image and publishes it to
   `ghcr.io/joshbilson/alfie` (GHCR only; no Docker Hub mirror).
5. In the stack repo, bump the image pin to the new tag, then run `deploy.sh`
   (Joshua-gated; scoped deploy — never recreate the tailscale netns owner).

## CI (this fork)

- `docker.yaml` — multi-arch GHCR build, trimmed to the **`main` variant only**
  (no cuda/cuda126/ollama/slim, no Docker Hub copy). Triggers: `workflow_dispatch`
  and push of `v*` tags only (no branch triggers).
- `gitleaks.yml` — secret scan on PRs, pushes to `main`, and `v*` tags. Loads
  `.gitleaks.toml` from the repo root.
- `frontend.yaml`, `backend.yaml` — upstream PR gates, retained.
- `*.disabled` files — left as upstream ships them (inactive).
- `release.yml`, `release-pypi.yml` — **removed**; the fork tags manually and
  does not publish to PyPI.

## alfie-specific source diffs

Kept minimal and confined to non-churn-map files:

- `backend/open_webui/env.py` — `WEBUI_NAME` default `Alfie`; dropped the
  upstream `' (Open WebUI)'` suffix appended to custom names.
- `src/lib/constants.ts` — `APP_NAME = 'Alfie'`.
- `src/app.html` — `<title>Alfie</title>`.
- `src/routes/+layout.svelte` — notification branding `… • Alfie`.

i18n locales are intentionally **not** touched.

## One-time baseline reset (recorded per playbook)

This fork was created from upstream and initially sat 37 commits ahead of the
chosen release tag (it had drifted to upstream `main` tip with **no**
alfie-specific commits). On **2026-06-12** `main` was force-aligned **once** to
the release-tag baseline:

```
old main: 02dc3e689ceac915a870b373318b99c029ddf603   (upstream main tip)
new main: 1a97751e376e00a1897bc3679215ae1c7bd8fd42   (tag v0.9.6 — BASETAG)
```

This was the **only** sanctioned force-push of `main`. From here on `main` is
append-only (merge upstream release tags; never rebase/force-push).
