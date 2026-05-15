# Andrebas Kodi Repozitár

**Čítaj v:** 🇬🇧 [English](README.md) · 🇸🇰 [Slovenčina](README.sk.md) · 🇨🇿 [Čeština](README.cs.md)

Osobný Kodi repozitár hostovaný používateľom **Andrebas**.

Momentálne obsahuje:

- **[plugin.video.tvheadend](plugin.video.tvheadend/)** — *Tvheadend Archive* — doplnkový prehliadač DVR archívu pre `pvr.hts`. Prechádza DVR nahrávky zoskupené podľa inteligentných kategórií a podžánrov, vyhľadáva podľa názvu bez diakritiky a vedie históriu naposledy pozeraných.

Repozitár je hostovaný cez GitHub Pages a aktualizácie sa publikujú automaticky cez GitHub Actions pri každom novom tagu.

---

## Pre koncových používateľov — inštalácia v Kodi

### Krok 1 — Povoľ inštaláciu z neznámych zdrojov

V Kodi: **Nastavenia → Systém → Doplnky → Neznáme zdroje** = ON.

### Krok 2 — Nainštaluj repozitár (jednorazovo)

Stiahni najnovší **`repository.andrebas.kodi-X.Y.Z.zip`** zo [stránky Releases](../../releases/latest).

V Kodi: **Doplnky → Inštalovať zo ZIP-u** → vyber stiahnutý ZIP.

### Krok 3 — Nainštaluj Tvheadend Archive z repozitára

**Doplnky → Inštalovať z repozitára → Andrebas Kodi Repository → Video doplnky → Tvheadend Archive → Inštalovať**.

Od tohto momentu si Kodi bude kontrolovať aktualizácie automaticky — keď bude vydaná nová verzia, dostaneš upozornenie do 24 hodín (alebo skôr; manuálne sa dá vynútiť cez Add-on browser).

---

## Pre vývojárov — vydanie novej verzie

Repo má plochú štruktúru. Každý doplnok žije vo svojom top-level adresári:

```
kodi-repo/
├── plugin.video.tvheadend/          ← zdroj doplnku (edituj tu)
├── repository.andrebas.kodi/        ← samotný repo doplnok
├── scripts/build_repo.py            ← regeneruje docs/ zo zdrojov
├── docs/                            ← obsah, ktorý servuje GitHub Pages (auto-generated)
└── .github/workflows/release.yml    ← CI: build a publish pri push tagu
```

### Release flow

1. Edituj kód v `plugin.video.tvheadend/` (alebo inom doplnku)
2. Bumpni `version="..."` v `addon.xml` toho doplnku
3. Pridaj nový `<news>` záznam nad predchádzajúci
4. Commitni, taguj, pushni:

```bash
git add plugin.video.tvheadend
git commit -m "Tvheadend Archive 1.0.1 — popis zmien"
git tag v1.0.1
git push origin main --tags
```

5. GitHub Actions prevezme kontrolu:
   - Spustí `scripts/build_repo.py` → prebalí všetky doplnky, regeneruje `docs/addons.xml` + MD5
   - Commitne aktualizované `docs/` späť do `main`
   - Vytvorí GitHub Release s priloženými ZIP-mi
   - GitHub Pages publikuje nový `docs/` do ~1 minúty

V Kodi používatelia uvidia update pri ďalšej periodickej kontrole (default ~24h, konfigurovateľné v **Nastavenia → Doplnky → Aktualizácie**).

### Lokálny rebuild (bez push-u)

```bash
python3 scripts/build_repo.py
```

Skontroluj `docs/` aby si overil, že výstup je v poriadku, pred push-om.

### Pridanie nového doplnku

1. Pridaj nový adresár (napr. `plugin.video.example/`) s validným `addon.xml` vedľa existujúcich
2. Spusti `python3 scripts/build_repo.py` — auto-discoveruje všetko, čo matchuje `plugin.*`, `repository.*`, `script.*`, `skin.*`, `service.*`, `audioencoder.*`, `pvr.*`
3. Commitni, taguj, pushni

---

## Setup GitHub Pages (jednorazovo, manuálne)

Po push-u tohto repa na GitHub:

1. Choď do **Settings → Pages** v GitHub repe
2. Nastav **Source** = "Deploy from a branch"
3. Branch = `main`, Folder = `/docs`
4. Save

Do ~1 minúty bude `https://<username>.github.io/kodi-repo/addons.xml` servovať repo feed.

URL v `repository.andrebas.kodi/addon.xml` musí súhlasiť s touto. Defaultne ukazuje na:

```
https://mikrotikexe.github.io/kodi-repo/
```

**Ak tvoj GitHub username NIE JE `mikrotikexe`**, pred prvým push-om:

```bash
# Nájdi a nahraď v repository.andrebas.kodi/addon.xml:
sed -i 's|mikrotikexe.github.io|TVOJ-USERNAME.github.io|g' repository.andrebas.kodi/addon.xml
python3 scripts/build_repo.py
```

(Aktualizuj aj link v `README.md` a inde, ak je relevantné.)

---

## Licencia

GPL-2.0-or-later. Pozri [LICENSE](LICENSE).
