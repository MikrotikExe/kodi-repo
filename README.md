# Andrebas Kodi Repository

**Read this in:** 🇬🇧 [English](README.md) · 🇸🇰 [Slovenčina](README.sk.md) · 🇨🇿 [Čeština](README.cs.md)

Personal Kodi addon repository hosting addons maintained by **Andrebas**.

Currently includes:

- **[plugin.video.tvheadend](plugin.video.tvheadend/)** — *Tvheadend Archive* — a DVR archive companion to `pvr.hts`. Browses DVR recordings grouped by smart categories and subgenres, searches by title without diacritics, and keeps a recently-watched history.

The repository is hosted via GitHub Pages and updates are pushed automatically by GitHub Actions on every tag.

---

## For end users — install in Kodi

### Step 1 — Allow installation from unknown sources

In Kodi: **Settings → System → Add-ons → Unknown sources** = ON.

### Step 2 — Install the repository (one-time)

Download the latest **`repository.andrebas.kodi-X.Y.Z.zip`** from the [Releases page](../../releases/latest).

In Kodi: **Add-ons → Install from zip file** → pick the downloaded ZIP.

### Step 3 — Install Tvheadend Archive from the repository

**Add-ons → Install from repository → Andrebas Kodi Repository → Video add-ons → Tvheadend Archive → Install**.

From now on Kodi will check for updates automatically — when a new version is tagged, you'll get an update notification within 24 hours (or sooner; force-check from Add-on browser).

---

## For developers — releasing a new version

The repo is a flat layout. Each addon lives in its own top-level directory:

```
kodi-repo/
├── plugin.video.tvheadend/          ← addon source (edit here)
├── repository.andrebas.kodi/        ← the repo addon itself
├── scripts/build_repo.py            ← regenerates docs/ from sources
├── docs/                            ← what GitHub Pages serves (auto-generated)
└── .github/workflows/release.yml    ← CI: builds and publishes on tag push
```

### Release flow

1. Edit code in `plugin.video.tvheadend/` (or any addon)
2. Bump `version="..."` in that addon's `addon.xml`
3. Add a `<news>` entry above the previous one
4. Commit, tag, push:

```bash
git add plugin.video.tvheadend
git commit -m "Tvheadend Archive 1.0.1 — picon prefetch"
git tag v1.0.1
git push origin main --tags
```

5. GitHub Actions takes over:
   - Runs `scripts/build_repo.py` → repacks all addons, regenerates `docs/addons.xml` + MD5
   - Commits the updated `docs/` back to `main`
   - Creates a GitHub Release with the ZIPs attached
   - GitHub Pages publishes the new `docs/` within ~1 minute

Within Kodi, users see the update on next periodic check (default ~24h, configurable in **Settings → Add-ons → Updates**).

### Local rebuild (without pushing)

```bash
python3 scripts/build_repo.py
```

Inspect `docs/` to verify the output is sane before pushing.

### Adding a new addon

1. Drop a new directory (e.g. `plugin.video.example/`) with a valid `addon.xml` next to the existing ones
2. Run `python3 scripts/build_repo.py` — it auto-discovers anything matching `plugin.*`, `repository.*`, `script.*`, `skin.*`, `service.*`, `audioencoder.*`, `pvr.*`
3. Commit, tag, push

---

## GitHub Pages setup (one-time, manual)

After pushing this repo to GitHub:

1. Go to **Settings → Pages** in the GitHub repo
2. Set **Source** = "Deploy from a branch"
3. Branch = `main`, Folder = `/docs`
4. Save

Within ~1 minute, `https://<username>.github.io/kodi-repo/addons.xml` will serve the repo feed.

The URL in `repository.andrebas.kodi/addon.xml` must match this. By default it points to:

```
https://mikrotikexe.github.io/kodi-repo/
```

**If your GitHub username is NOT `andrebas`**, before first push:

```bash
# Find & replace in repository.andrebas.kodi/addon.xml:
sed -i 's|mikrotikexe.github.io|YOUR-USERNAME.github.io|g' repository.andrebas.kodi/addon.xml
python3 scripts/build_repo.py
```

(Also update the link in your `README.md` and any other place if relevant.)

---

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
