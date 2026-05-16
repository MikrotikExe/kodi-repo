# Andrebas Kodi Repozitář

**Čti v:** 🇬🇧 [English](README.md) · 🇸🇰 [Slovenčina](README.sk.md) · 🇨🇿 [Čeština](README.cs.md)

Osobní Kodi repozitář hostovaný uživatelem **Andrebas**.

Aktuálně obsahuje:

- **[plugin.video.tvheadend](plugin.video.tvheadend/)** — *Tvheadend Archive* — doplňkový prohlížeč DVR archivu pro `pvr.hts`. Prochází DVR nahrávky seskupené podle inteligentních kategorií a podžánrů, vyhledává podle názvu bez diakritiky, **pamatuje si pozici přehrávání (resume)** a vede historii naposledy sledovaných.

Repozitář je hostovaný přes GitHub Pages a aktualizace se publikují automaticky přes GitHub Actions při každém novém tagu.

---

## Pro koncové uživatele — instalace v Kodi

### Krok 1 — Povol instalaci z neznámých zdrojů

V Kodi: **Nastavení → Systém → Doplňky → Neznámé zdroje** = ON.

### Krok 2 — Nainstaluj repozitář (jednorázově)

Stáhni nejnovější **`repository.andrebas.kodi-X.Y.Z.zip`** ze [stránky Releases](../../releases/latest).

V Kodi: **Doplňky → Instalovat ze ZIP** → vyber stažený ZIP.

### Krok 3 — Nainstaluj Tvheadend Archive z repozitáře

**Doplňky → Instalovat z repozitáře → Andrebas Kodi Repository → Video doplňky → Tvheadend Archive → Instalovat**.

Od tohoto momentu si bude Kodi kontrolovat aktualizace automaticky — když bude vydána nová verze, dostaneš upozornění do 24 hodin (nebo dřív; manuálně lze vynutit přes Add-on browser).

---

## Pro vývojáře — vydání nové verze

Repo má plochou strukturu. Každý doplněk žije ve svém top-level adresáři:

```
kodi-repo/
├── plugin.video.tvheadend/          ← zdroj doplňku (edituj tady)
├── repository.andrebas.kodi/        ← samotný repo doplněk
├── scripts/build_repo.py            ← regeneruje docs/ ze zdrojů
├── docs/                            ← obsah, který servíruje GitHub Pages (auto-generated)
└── .github/workflows/release.yml    ← CI: build a publish při push tagu
```

### Release flow

1. Edituj kód v `plugin.video.tvheadend/` (nebo jiném doplňku)
2. Bumpni `version="..."` v `addon.xml` toho doplňku
3. Přidej nový `<news>` záznam nad předchozí
4. Commitni, taguj, pushni:

```bash
git add plugin.video.tvheadend
git commit -m "Tvheadend Archive 1.0.1 — popis změn"
git tag v1.0.1
git push origin main --tags
```

5. GitHub Actions převezme kontrolu:
   - Spustí `scripts/build_repo.py` → přebalí všechny doplňky, regeneruje `docs/addons.xml` + MD5
   - Commitne aktualizované `docs/` zpět do `main`
   - Vytvoří GitHub Release s přiloženými ZIP soubory
   - GitHub Pages publikuje nový `docs/` do ~1 minuty

V Kodi uživatelé uvidí update při další periodické kontrole (default ~24h, konfigurovatelné v **Nastavení → Doplňky → Aktualizace**).

### Lokální rebuild (bez push-u)

```bash
python3 scripts/build_repo.py
```

Zkontroluj `docs/` abys ověřil, že výstup je v pořádku, před push-em.

### Přidání nového doplňku

1. Přidej nový adresář (např. `plugin.video.example/`) s validním `addon.xml` vedle existujících
2. Spusť `python3 scripts/build_repo.py` — auto-objeví vše, co matchuje `plugin.*`, `repository.*`, `script.*`, `skin.*`, `service.*`, `audioencoder.*`, `pvr.*`
3. Commitni, taguj, pushni

---

## Setup GitHub Pages (jednorázově, manuálně)

Po push-u tohoto repa na GitHub:

1. Jdi do **Settings → Pages** v GitHub repu
2. Nastav **Source** = "Deploy from a branch"
3. Branch = `main`, Folder = `/docs`
4. Save

Do ~1 minuty bude `https://<username>.github.io/kodi-repo/addons.xml` servírovat repo feed.

URL v `repository.andrebas.kodi/addon.xml` musí souhlasit s touto. Defaultně ukazuje na:

```
https://mikrotikexe.github.io/kodi-repo/
```

**Pokud tvoje GitHub username NENÍ `mikrotikexe`**, před prvním push-em:

```bash
# Najdi a nahraď v repository.andrebas.kodi/addon.xml:
sed -i 's|mikrotikexe.github.io|TVOJE-USERNAME.github.io|g' repository.andrebas.kodi/addon.xml
python3 scripts/build_repo.py
```

(Aktualizuj i link v `README.md` a jinde, pokud je relevantní.)

---

## Licence

GPL-3.0-or-later. Viz [LICENSE](LICENSE).
