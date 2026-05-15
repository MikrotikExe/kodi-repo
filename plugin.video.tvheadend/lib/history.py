# -*- coding: utf-8 -*-
"""
Recently watched DVR entries — perzistencia v addon profile directory.

Kodi plugin proces sa spúšťa novo pri každej navigácii, takže históriu
ukladáme na disk do `addon_data/<addon_id>/history.json`.

Schéma entry (JSON):
  {
    "uuid":         "...",   # TVH UUID nahrávky (primárny key)
    "title":        "...",
    "subtitle":     "...",
    "channelname":  "...",
    "channel_icon": "...",   # raw 'channel_icon' z TVH (treba make_icon_url)
    "dvr_url":      "...",   # to čo používa make_dvr_url()
    "ts":           1234567890,  # original recording start (start_real/start)
    "duration":     3600,        # seconds (0 ak neznáme)
    "played_at":    1234567890,  # kedy to user prehral naposledy
  }

Súbor je JSON list, najnovšie prvé (insertion order = played_at desc).
"""

from __future__ import annotations

import json
import os
import threading
import time

import xbmc
import xbmcaddon
import xbmcvfs


_LOCK = threading.RLock()
_HISTORY_FILE_NAME = "history.json"
_MAX_ENTRIES_DEFAULT = 50


def _profile_dir() -> str:
    """Vráti profile directory addonu, vytvorí ho ak neexistuje."""
    addon = xbmcaddon.Addon()
    profile = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    if not xbmcvfs.exists(profile):
        xbmcvfs.mkdirs(profile)
    return profile


def _history_path() -> str:
    return os.path.join(_profile_dir(), _HISTORY_FILE_NAME)


def _log(msg: str, level: int = xbmc.LOGINFO) -> None:
    try:
        xbmc.log("[plugin.video.tvheadend.history] %s" % msg, level)
    except Exception:
        pass


def _entry_key(e: dict) -> str:
    """Identifikátor pre dedup — uuid má prednosť, dvr_url ako fallback."""
    return (e.get("uuid") or "").strip() or (e.get("dvr_url") or "").strip()


def load() -> list:
    """Vráti zoznam recently watched entries (najnovšie prvé). Tichý fail → []."""
    path = _history_path()
    with _LOCK:
        try:
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                # Filter out invalid entries
                return [e for e in data if isinstance(e, dict) and _entry_key(e)]
            return []
        except Exception as e:
            _log("load failed: %s" % e, xbmc.LOGWARNING)
            return []


def _save(entries: list) -> None:
    path = _history_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception as e:
        _log("save failed: %s" % e, xbmc.LOGWARNING)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def add(entry: dict, max_entries: int = _MAX_ENTRIES_DEFAULT) -> None:
    """Pridá entry na top zoznamu, dedup podľa uuid/dvr_url, oreže na max_entries."""
    if not entry:
        return
    key = _entry_key(entry)
    if not key:
        return

    now = int(time.time())
    e = dict(entry)
    e["played_at"] = now

    with _LOCK:
        entries = load()
        entries = [x for x in entries if _entry_key(x) != key]
        entries.insert(0, e)
        if max_entries > 0:
            entries = entries[:max_entries]
        _save(entries)


def remove(key: str) -> bool:
    """Odstráni entry podľa uuid alebo dvr_url. True ak niečo bolo odstránené."""
    if not key:
        return False
    with _LOCK:
        entries = load()
        new_entries = [e for e in entries if _entry_key(e) != key]
        if len(new_entries) == len(entries):
            return False
        _save(new_entries)
        return True


def clear() -> None:
    """Vymaže celú históriu."""
    with _LOCK:
        _save([])
