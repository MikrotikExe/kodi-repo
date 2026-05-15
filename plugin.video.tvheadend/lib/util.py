# -*- coding: utf-8 -*-
"""
Pomocné funkcie pre Kodi addon: settings adapter, logging, i18n.

Hlavný účel: vystaviť rozhranie, ktoré tvh_client.py očakáva od pôvodného
ArchivCZSK `cp` objektu (get_setting, get_requests_session, _, log), takže
samotný klient sa nemusí prepisovať – stačí ho z neho importovať.
"""

from __future__ import annotations

import threading

import requests

import xbmc
import xbmcaddon


_ADDON = xbmcaddon.Addon()
_ADDON_ID = _ADDON.getAddonInfo("id")
_ADDON_NAME = _ADDON.getAddonInfo("name")


def addon() -> xbmcaddon.Addon:
    """Vráti čerstvý Addon objekt (re-instanciuje – nastavenia sa môžu meniť za behu)."""
    return xbmcaddon.Addon()


def tr(string_id: int) -> str:
    """Localized string helper – vráti preložený string podľa ID z strings.po."""
    return addon().getLocalizedString(string_id)


def log(msg: str, level: int = xbmc.LOGINFO) -> None:
    """Zapíše do Kodi logu s prefixom addon ID."""
    try:
        xbmc.log("[%s] %s" % (_ADDON_ID, msg), level)
    except Exception:
        pass


class ContentProviderShim:
    """
    Tenký adaptér nad Kodi addonom, ktorý vystavuje rozhranie pôvodného
    ArchivCZSK ContentProvideru pre tvh_client.py.

    Pôvodný `cp` poskytoval:
      cp.get_setting(key)         → hodnota nastavenia (str/bool/int)
      cp.get_requests_session()   → requests.Session inštancia
      cp._(text)                  → preklad textu (gettext)

    Keďže tvh_client volá len tieto tri rozhrania, stačí ich emulovať.
    Preklady tu vraciame ako-je (anglický text z kódu), pretože tvh_client
    používa _() len v error správach a tie sa zobrazia v notifikáciách
    cez xbmcgui.Dialog().notification() – netreba ich lokalizovať na úrovni
    knižnice (Kodi GUI vrstva to vyrieši samostatne ak treba).
    """

    # Typy nastavení – musíme vedieť ako čítať z Kodi (getSettingBool vs string)
    _BOOL_KEYS = frozenset({"use_https"})
    _INT_KEYS = frozenset({"port", "loading_timeout", "dvr_limit", "archive_days_limit"})

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session_lock = threading.RLock()

    def get_setting(self, key: str):
        a = addon()
        try:
            if key in self._BOOL_KEYS:
                return a.getSettingBool(key)
            if key in self._INT_KEYS:
                try:
                    return a.getSettingInt(key)
                except Exception:
                    # fallback – staršie inštalácie môžu mať uložené ako string
                    raw = a.getSettingString(key) or ""
                    try:
                        return int(raw)
                    except (TypeError, ValueError):
                        return 0
            return a.getSettingString(key) or ""
        except Exception as exc:
            log("get_setting(%r) failed: %s" % (key, exc), xbmc.LOGWARNING)
            return "" if key not in self._BOOL_KEYS else False

    def get_requests_session(self) -> requests.Session:
        with self._session_lock:
            return self._session

    # gettext-kompatibilný no-op (text vraciame nezmenený)
    def _(self, text: str) -> str:
        return text
