#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_repo.py — generuje obsah docs/ priečinka ktorý servuje GitHub Pages
ako Kodi repozitár.

Čo robí:
  1. Pre každý addon (priečinok začínajúci na "plugin." alebo "repository.")
     v root-e prečíta addon.xml, zistí id+version
  2. Zabalí addon do <id>-<version>.zip
  3. Skopíruje addon.xml a icon.png vedľa ZIPu (Kodi sa na to ide pozerať)
  4. Zlúči všetky addon.xml-y do jedného docs/addons.xml
  5. Vypočíta MD5 a uloží do docs/addons.xml.md5

Po behu stačí git commit + push. GitHub Pages obslúži docs/ ako repo.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# Priečinky ktoré sa považujú za addony
ADDON_PREFIXES = ("plugin.", "repository.", "script.", "skin.", "service.", "audioencoder.", "pvr.")


def discover_addons(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and any(p.name.startswith(pref) for pref in ADDON_PREFIXES):
            if (p / "addon.xml").exists():
                out.append(p)
    return out


def get_addon_info(addon_dir: Path) -> tuple[str, str]:
    """(addon_id, version) z addon.xml"""
    tree = ET.parse(addon_dir / "addon.xml")
    root = tree.getroot()
    return root.get("id"), root.get("version")


def zip_addon(addon_dir: Path, out_zip: Path) -> None:
    """Zabalí addon do ZIP-u. Štruktúra v zipe: <addon_id>/<files...>"""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    addon_id = addon_dir.name
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in addon_dir.rglob("*"):
            # Preskočíme cache/.git/.DS_Store
            if any(part in {"__pycache__", ".git", ".DS_Store"} for part in path.parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_file():
                arcname = Path(addon_id) / path.relative_to(addon_dir)
                zf.write(path, arcname.as_posix())


def build_docs(addons: list[Path]) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)

    # Zlúčený addons.xml — XML hlavička + <addons> wrapper + inner z každého addonu
    combined = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<addons>"]

    for addon_dir in addons:
        addon_id, version = get_addon_info(addon_dir)
        if not addon_id or not version:
            print(f"  WARN: skipping {addon_dir.name} (missing id/version)")
            continue

        # 1. ZIP do docs/<id>/<id>-<version>.zip
        zip_target = DOCS / addon_id / f"{addon_id}-{version}.zip"
        zip_addon(addon_dir, zip_target)

        # 2. Kopírujeme addon.xml a icon.png vedľa ZIP-u (Kodi to pýta)
        shutil.copy(addon_dir / "addon.xml", DOCS / addon_id / "addon.xml")
        icon = addon_dir / "icon.png"
        if icon.exists():
            shutil.copy(icon, DOCS / addon_id / "icon.png")

        # 3. Pripojiť addon.xml do zlúčeného feed-u (bez XML hlavičky)
        with open(addon_dir / "addon.xml", encoding="utf-8") as f:
            xml_text = f.read()
        # Vystrihneme XML deklaráciu ak je
        if xml_text.lstrip().startswith("<?xml"):
            xml_text = xml_text.split("?>", 1)[1]
        combined.append(xml_text.strip())

        size_kb = zip_target.stat().st_size / 1024
        print(f"  OK   {addon_id} v{version} ({size_kb:.1f} KB)")

    combined.append("</addons>")
    addons_xml = "\n".join(combined) + "\n"

    # 4. addons.xml + addons.xml.md5
    (DOCS / "addons.xml").write_text(addons_xml, encoding="utf-8")
    md5 = hashlib.md5(addons_xml.encode("utf-8")).hexdigest()
    (DOCS / "addons.xml.md5").write_text(md5 + "\n", encoding="utf-8")

    print(f"\n  Wrote {DOCS / 'addons.xml'} ({len(addons_xml)} bytes)")
    print(f"  MD5:  {md5}")


def main() -> int:
    print(f"Repo root: {ROOT}")
    addons = discover_addons(ROOT)
    if not addons:
        print("ERROR: no addons found", file=sys.stderr)
        return 1
    print(f"Found {len(addons)} addon(s):")
    for a in addons:
        print(f"  - {a.name}")
    print()

    build_docs(addons)

    print("\nDone. Commit and push docs/ to deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
