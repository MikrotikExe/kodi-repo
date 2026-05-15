# -*- coding: utf-8 -*-
"""
Resume playback — perzistentné ukladanie pozícií prehrávania DVR nahrávok.

Stratégia:
  - Pozícia sa ukladá do `userdata/addon_data/<addon>/resume.json`
  - Atomic write cez tmp + os.replace (žiadne čiastočné súbory pri crash-i)
  - Cap 500 záznamov — pri prekročení vyhadzujeme najstaršie (LRU style)
  - Smart filtering:
      * < 30s pozície → nepokladá sa to za "začatie sledovania", neukladáme
      * > 95% alebo posledných 60s → považujeme za "pozreté do konca", clearujeme
  - Pri play_dvr handler načíta uloženú pozíciu (ak je) a setne ResumeTime/TotalTime
    properties na ListItem — Kodi automaticky ponúkne dialog "Resume from X:XX
    / Start from beginning"
  - Počas prehrávania `ResumeTracker` (xbmc.Monitor child) polluje xbmc.Player
    každé 2 sekundy a po stopnutí uloží pozíciu

Key = DVR entry UUID (stabilné medzi sessions).
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
_RESUME_FILE = "resume.json"
_MAX_ENTRIES = 500

# Smart filtering thresholds
_MIN_POSITION_SECONDS = 30.0       # pod toto nepovažujeme za začatie sledovania
_END_THRESHOLD_SECONDS = 60.0      # posledných N sekúnd = "pozreté do konca"
_END_FRACTION = 0.05                # alebo posledných 5% = "pozreté do konca"


def _log(msg: str, level: int = xbmc.LOGINFO) -> None:
    try:
        xbmc.log("[plugin.video.tvheadend.resume] %s" % msg, level)
    except Exception:
        pass


def _profile_dir() -> str:
    addon = xbmcaddon.Addon()
    return xbmcvfs.translatePath(addon.getAddonInfo("profile"))


def _resume_path() -> str:
    return os.path.join(_profile_dir(), _RESUME_FILE)


def _ensure_profile() -> None:
    try:
        os.makedirs(_profile_dir(), exist_ok=True)
    except Exception:
        pass


def _load_all() -> dict:
    """Read whole resume dict from disk."""
    try:
        with open(_resume_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log("load failed: %s" % e, xbmc.LOGWARNING)
        return {}


def _save_all(data: dict) -> None:
    """Atomic write — tmp + os.replace."""
    _ensure_profile()
    path = _resume_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception as e:
        _log("save failed: %s" % e, xbmc.LOGWARNING)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def get(uuid: str):
    """Vráti (position_seconds, total_seconds, timestamp) alebo None."""
    if not uuid:
        return None
    with _LOCK:
        data = _load_all()
    entry = data.get(uuid)
    if not entry or not isinstance(entry, dict):
        return None
    try:
        return (
            float(entry.get("position", 0) or 0),
            float(entry.get("total", 0) or 0),
            int(entry.get("ts", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


def save(uuid: str, position: float, total: float) -> None:
    """Uloží pozíciu so smart filtering — vynechá ak <30s alebo blízko konca."""
    if not uuid:
        return
    try:
        position = float(position)
        total = float(total)
    except (TypeError, ValueError):
        return

    # Filter 1: príliš krátko — nepovažujeme za začatie sledovania
    if position < _MIN_POSITION_SECONDS:
        return

    # Filter 2: pozreté do konca — vymazať namiesto uloženia
    if total > 0:
        near_end_by_time = position >= (total - _END_THRESHOLD_SECONDS)
        near_end_by_frac = position >= total * (1.0 - _END_FRACTION)
        if near_end_by_time or near_end_by_frac:
            clear(uuid)
            return

    with _LOCK:
        data = _load_all()
        data[uuid] = {
            "position": round(position, 2),
            "total": round(total, 2),
            "ts": int(time.time()),
        }

        # Cap size — keep newest N
        if len(data) > _MAX_ENTRIES:
            items = sorted(data.items(),
                           key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
                           reverse=True)
            data = dict(items[:_MAX_ENTRIES])

        _save_all(data)


def clear(uuid: str) -> None:
    """Odstráni jeden záznam (napr. pri "pozreté do konca")."""
    if not uuid:
        return
    with _LOCK:
        data = _load_all()
        if uuid in data:
            del data[uuid]
            _save_all(data)


def clear_all() -> int:
    """Zmaže všetky uložené pozície. Vracia počet zmazaných."""
    with _LOCK:
        data = _load_all()
        n = len(data)
        _save_all({})
    return n


def count() -> int:
    """Počet aktuálne uložených pozícií."""
    with _LOCK:
        return len(_load_all())


# --------------------------------------------------------------------------
# Player monitor — sleduje prehrávanie a uloží pozíciu pri stope
# --------------------------------------------------------------------------

class ResumeTracker(xbmc.Monitor):
    """Spúšťa sa po setResolvedUrl, blokuje až do konca prehrávania.

    Polluje xbmc.Player() každé 2 sekundy, pamätá si poslednú validnú pozíciu.
    Keď prehrávanie skončí (užívateľ stopol, video skončilo, Kodi sa vypína),
    uloží poslednú pozíciu cez `save()` (ktoré aplikuje smart filtering).

    Plugin script ostáva nažive počas prehrávania — to je normálna prax pre
    Kodi plugin.video.* doplnky ktoré chcú trackovať playback.
    """

    def __init__(self, dvr_uuid: str):
        super().__init__()
        self.dvr_uuid = dvr_uuid
        self.player = xbmc.Player()
        self.last_position = 0.0
        self.last_total = 0.0
        self.ever_played = False

    def run(self, start_timeout: float = 15.0, poll_interval: float = 2.0) -> None:
        # Wait for playback to start (buffering, network, etc. môže trvať)
        waited = 0.0
        while waited < start_timeout:
            if self.abortRequested():
                return
            if self.player.isPlaying():
                self.ever_played = True
                break
            if self.waitForAbort(0.2):
                return
            waited += 0.2

        if not self.ever_played:
            _log("playback never started for %s — nothing to track" % self.dvr_uuid,
                 xbmc.LOGDEBUG)
            return

        # Poll position while playing
        while self.player.isPlaying():
            try:
                pos = float(self.player.getTime())
                tot = float(self.player.getTotalTime())
                if pos > 0:
                    self.last_position = pos
                if tot > 0:
                    self.last_total = tot
            except RuntimeError:
                # Player went away unexpectedly
                break

            if self.waitForAbort(poll_interval):
                break

        # Player stopped — persist position
        if self.last_position > 0:
            try:
                save(self.dvr_uuid, self.last_position, self.last_total)
                _log("saved position %.1fs / %.1fs for %s" %
                     (self.last_position, self.last_total, self.dvr_uuid),
                     xbmc.LOGDEBUG)
            except Exception as e:
                _log("post-playback save failed: %s" % e, xbmc.LOGWARNING)
