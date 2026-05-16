# -*- coding: utf-8 -*-
"""
Tvheadend HTTP API klient – Kodi 20+ port.

Vychádza z plugin_video_tvheadend/tvheadend.py (ArchivCZSK 0.49d) s úpravami:
  - Python 3 only (žiadne Py2 fallbacky)
  - Importy urllib.parse priame (žiadny tools_archivczsk.six)
  - ExpiringLRUCache nahradený lokálnou jednoduchou implementáciou
  - AddonErrorException importovaný z lokálneho errors modulu
  - Picon download / PIL konverzia / XMLTV fetch vypustené (v1 ich nepotrebuje)
  - init_picons_async / _init_picons_worker / _candidate_image_paths vypustené

Zachované:
  - HTTP API volania (api_get, api_get_all) s retry/backoff
  - Thread-safe auth handling (_apply_auth_to_session, _req_lock)
  - check_login s krátkym timeoutom
  - Stream URL builder (make_live_stream_url, make_dvr_url)
  - Channel/tag/DVR/EPG API metódy (get_tags, get_channels, get_dvr_finished,
    get_epg_now, get_epg_now_next, get_channels_by_tag, get_channel_name_by_service_uuid)
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlparse, urlunparse, quote, urlencode

from requests.auth import HTTPDigestAuth

from .errors import AddonErrorException


# --------------------------------------------------------------------------
# Jednoduchý TTL cache (náhrada za tools_archivczsk.cache.ExpiringLRUCache)
# --------------------------------------------------------------------------
class _SimpleTTLCache:
    """Minimalistický thread-safe TTL cache.

    Pre potreby tvh_client.py stačí 1 slot (zoznam kanálov) s 60s TTL.
    Implementuje rozhranie: get(key) -> hodnota|None, put(key, val),
    invalidate(key).
    """

    def __init__(self, capacity: int = 1, default_timeout: int = 60):
        self._capacity = capacity
        self._timeout = default_timeout
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get(self, key: str):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._capacity and key not in self._data:
                # evict oldest by expiry
                oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
                self._data.pop(oldest, None)
            self._data[key] = (time.time() + self._timeout, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


# --------------------------------------------------------------------------
# Tvheadend klient
# --------------------------------------------------------------------------
class Tvheadend:
    """
    Thin wrapper nad Tvheadend HTTP API (port 9981/9982).

    Nevyužíva HTSP – všetko ide cez REST JSON API. Konštruktor prijíma
    `cp` objekt (ContentProviderShim z lib.util) ktorý vystavuje:
      cp.get_setting(key), cp.get_requests_session(), cp._(text)
    """

    PREFER_CHANNEL_STREAM = True
    USE_TITLE_PARAM = True

    STREAM_CH_ENDPOINT = "stream/channel/%s"
    STREAM_CHID_ENDPOINT = "stream/channelid/%s"
    STREAM_SVC_ENDPOINT = "stream/service/%s"

    # Cache pre kanály s TTL 60 sekúnd – class-level zdieľaná medzi instanciami
    _channels_cache = _SimpleTTLCache(1, default_timeout=60)

    def __init__(self, cp):
        self.cp = cp
        self._ = cp._
        self.req = cp.get_requests_session()
        # Thread-safe auth handling – pozri komentár v originále
        self._req_lock = threading.RLock()
        self._auth_sig: tuple | None = None
        # Pri mode="auto" si pamätáme aktuálne použitú metódu (basic/digest).
        # Začíname Basic (default v TVH 4.x), po 401 prepneme na Digest.
        self._auto_effective: str = "basic"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _timeout(self):
        try:
            t = int(self.cp.get_setting("loading_timeout"))
        except Exception:
            t = 15
        return None if t == 0 else t

    def base_url(self) -> str:
        host = (self.cp.get_setting("host") or "").strip()
        if not host:
            raise AddonErrorException(
                self._("Missing Tvheadend server address in settings.")
            )

        if host.startswith("http://") or host.startswith("https://"):
            u = urlparse(host)
            scheme = u.scheme
            hostname = u.hostname or ""
            port = str(u.port or (9982 if scheme == "https" else 9981))
            return "%s://%s:%s" % (scheme, hostname, port)

        port_raw = self.cp.get_setting("port")
        port = str(port_raw or "9981").strip() if isinstance(port_raw, str) else str(port_raw or 9981)
        use_https = bool(self.cp.get_setting("use_https"))
        scheme = "https" if use_https else "http"
        return "%s://%s:%s" % (scheme, host, port)

    def _auth_signature(self):
        try:
            return (
                (self.cp.get_setting("username") or "").strip(),
                (self.cp.get_setting("password") or ""),
                (self.cp.get_setting("http_auth_mode") or "auto").strip().lower(),
            )
        except Exception:
            return None

    def _apply_auth_to_session(self, sess=None, force: bool = False) -> None:
        if sess is None or sess is self.req:
            sess = self.req
            with self._req_lock:
                sig = self._auth_signature()
                if not force and sig == self._auth_sig and self._auth_sig is not None:
                    return
                self._do_apply_auth(sess, sig)
                self._auth_sig = sig
            return

        # externá session – nezdieľaná
        sig = self._auth_signature()
        self._do_apply_auth(sess, sig)

    def _do_apply_auth(self, sess, sig) -> None:
        if sig is None:
            user, pwd, mode = "", "", "auto"
        else:
            user, pwd, mode = sig

        if not user or mode == "none":
            sess.auth = None
            return
        if mode == "digest":
            sess.auth = HTTPDigestAuth(user, pwd)
        elif mode == "auto":
            # TVH 4.x defaultne používa Basic. Pri prvom 401 v api_get
            # prepneme _auto_effective na "digest" a sess.auth sa preapliuje.
            if self._auto_effective == "digest":
                sess.auth = HTTPDigestAuth(user, pwd)
            else:
                sess.auth = (user, pwd)
        else:
            sess.auth = (user, pwd)

    def _url(self, path: str) -> str:
        path = (path or "").lstrip("/")
        return self.base_url().rstrip("/") + "/" + path

    def invalidate_auth_cache(self) -> None:
        with self._req_lock:
            self._auth_sig = None

    # ------------------------------------------------------------------
    # API volania s retry/backoff
    # ------------------------------------------------------------------

    _RETRY_ATTEMPTS = 3
    _RETRY_BACKOFF_BASE = 0.5  # sekundy
    _RETRY_STATUS_CODES = (500, 502, 503, 504, 408, 429)

    def api_get(self, path: str, params=None, timeout_override=None):
        url = self._url(path)
        last_err = None
        req_timeout = (
            timeout_override if timeout_override is not None else self._timeout()
        )
        for attempt in range(self._RETRY_ATTEMPTS):
            with self._req_lock:
                self._apply_auth_to_session()
                try:
                    resp = self.req.get(
                        url, params=params or {}, timeout=req_timeout
                    )
                except Exception as e:
                    last_err = e
                    resp = None

            if resp is not None:
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    try:
                        return resp.json()
                    except Exception:
                        raise AddonErrorException(
                            self._("Tvheadend returned invalid JSON.")
                        )
                # Auto-detect: ak sme v "auto" móde a server vrátil 401 s Basic,
                # prepneme na Digest a retrynieme (TVH inštalácie s Digest auth
                # nastaveným cez reverse proxy / nginx, alebo staršie verzie).
                if status == 401:
                    sig = self._auth_signature()
                    if sig and sig[2] == "auto" and self._auto_effective == "basic":
                        self._auto_effective = "digest"
                        with self._req_lock:
                            self._apply_auth_to_session(force=True)
                        last_err = Exception("HTTP 401 — switching auth to digest and retrying")
                        # Retry immediately bez backoff
                        continue
                if status not in self._RETRY_STATUS_CODES:
                    try:
                        resp.raise_for_status()
                    except Exception as e:
                        raise AddonErrorException(
                            "%s\n%s"
                            % (self._("Tvheadend API request failed."), str(e))
                        )
                last_err = Exception("HTTP %s for %s" % (status, url))

            if attempt < self._RETRY_ATTEMPTS - 1:
                try:
                    time.sleep(self._RETRY_BACKOFF_BASE * (2 ** attempt))
                except Exception:
                    pass

        raise AddonErrorException(
            "%s\n%s"
            % (
                self._("Tvheadend API request failed."),
                str(last_err) if last_err else "unknown error",
            )
        )

    def api_get_all(self, path: str, params=None, page_limit: int = 500):
        params = dict(params or {})
        start = int(params.get("start", 0))
        limit = int(params.get("limit", page_limit)) or page_limit

        entries: list = []
        total = None
        for _ in range(200):
            params["start"] = start
            params["limit"] = limit
            data = self.api_get(path, params)
            page = data.get("entries") or []
            entries.extend(page)

            if total is None:
                try:
                    total = int(data.get("total"))
                except Exception:
                    total = None

            if total is not None and len(entries) >= total:
                break
            if not page or len(page) < limit:
                break
            start += limit

        return entries

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        host = (self.cp.get_setting("host") or "").strip()
        return bool(host)

    _CHECK_LOGIN_TIMEOUT = 5

    def check_login(self, force_reauth: bool = False) -> bool:
        """Overí spojenie volaním /api/serverinfo. Vyhodí výnimku pri chybe."""
        if force_reauth:
            try:
                self.invalidate_auth_cache()
            except Exception:
                pass
        self.api_get(
            "api/serverinfo", params={}, timeout_override=self._CHECK_LOGIN_TIMEOUT
        )
        return True

    def serverinfo(self) -> dict:
        """Vráti /api/serverinfo dict (verzia TVH, capabilities)."""
        try:
            return self.api_get("api/serverinfo") or {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Stream URL builders
    # ------------------------------------------------------------------

    def _url_with_creds(self, full_url: str) -> str:
        user = (self.cp.get_setting("username") or "").strip()
        pwd = (self.cp.get_setting("password") or "")
        if not user:
            return full_url
        u = urlparse(full_url)
        netloc = "%s:%s@%s" % (
            quote(user, safe=""),
            quote(pwd, safe=""),
            u.netloc,
        )
        return urlunparse(
            (u.scheme, netloc, u.path, u.params, u.query, u.fragment)
        )

    def _build_stream_url(
        self, endpoint_path: str, profile=None, channel_title=None
    ) -> str:
        url = self._url(endpoint_path)
        params: dict = {}
        if profile:
            params["profile"] = profile
        if self.USE_TITLE_PARAM and channel_title:
            try:
                ct = str(channel_title).strip()
                if ct:
                    params["title"] = ct
            except Exception:
                pass
        if params:
            url = url + "?" + urlencode(params)
        return self._url_with_creds(url)

    def make_live_stream_url(
        self, channel_uuid=None, service_uuid=None, channel_title=None
    ) -> str:
        profile = (self.cp.get_setting("profile") or "pass").strip()

        if self.PREFER_CHANNEL_STREAM and channel_uuid:
            return self._build_stream_url(
                self.STREAM_CH_ENDPOINT % channel_uuid,
                profile=profile,
                channel_title=channel_title,
            )
        if service_uuid:
            return self._build_stream_url(
                self.STREAM_SVC_ENDPOINT % service_uuid,
                profile=profile,
                channel_title=channel_title,
            )
        if channel_uuid:
            return self._build_stream_url(
                self.STREAM_CHID_ENDPOINT % channel_uuid,
                profile=profile,
                channel_title=channel_title,
            )
        raise AddonErrorException(
            self._("Missing channel/service identifier for streaming.")
        )

    def make_dvr_url(self, entry_url_field):
        if not entry_url_field:
            return None
        return self._url_with_creds(self._url(entry_url_field))

    def make_icon_url(self, icon_public_url: str) -> str:
        """V Kodi nepotrebujeme sťahovať picons – stačí vrátiť URL s credentials,
        Kodi ListItem.setArt() to vykreslí priamo. Ak icon URL nie je absolútna,
        doplníme TVH base_url."""
        if not icon_public_url:
            return ""
        if icon_public_url.startswith("http://") or icon_public_url.startswith("https://"):
            return self._url_with_creds(icon_public_url)
        # relative path k TVH – napr. "imagecache/123"
        return self._url_with_creds(self._url(icon_public_url))

    # ------------------------------------------------------------------
    # Channel / tag / DVR / EPG API
    # ------------------------------------------------------------------

    def get_tags(self) -> list:
        return self.api_get_all("api/channeltag/grid", {"start": 0}, page_limit=200)

    def get_channels(self, force: bool = False) -> list:
        """Vráti zoznam kanálov. Výsledok sa cachuje na 60 sekúnd."""
        if not force:
            cached = self._channels_cache.get("channels")
            if cached is not None:
                return cached
        result = self.api_get_all(
            "api/channel/grid", {"start": 0}, page_limit=1000
        )
        self._channels_cache.put("channels", result)
        return result

    def invalidate_channels_cache(self) -> None:
        self._channels_cache.invalidate("channels")

    def get_channels_by_tag(self, tag_uuid):
        channels = self.get_channels()
        if not tag_uuid:
            return channels
        return [ch for ch in channels if tag_uuid in (ch.get("tags") or [])]

    def get_dvr_finished(self) -> list:
        return self.api_get_all(
            "api/dvr/entry/grid_finished", {"start": 0}, page_limit=500
        )

    def get_epg_now(self, limit: int = 5000) -> dict:
        """Vráti dict {channelUuid: event} pre práve bežiace programy."""
        try:
            data = self.api_get(
                "api/epg/events/grid",
                params={"mode": "now", "limit": int(limit)},
            )
        except Exception:
            return {}
        out: dict = {}
        for e in (data.get("entries") or []):
            ch = e.get("channelUuid")
            if ch:
                out[ch] = e
        return out

    def get_epg_now_next(self, channel_uuid):
        """Vráti (now_event, next_event) pre daný kanál."""
        if not channel_uuid:
            return (None, None)

        def _fetch(mode):
            params = {
                "mode": mode,
                "limit": 1,
                "start": 0,
                "channel": channel_uuid,
            }
            try:
                data = self.api_get("api/epg/events/grid", params=params) or {}
            except Exception:
                return None
            entries = data.get("entries")
            if entries:
                return entries[0]
            params.pop("channel", None)
            params["channelUuid"] = channel_uuid
            try:
                data = self.api_get("api/epg/events/grid", params=params) or {}
            except Exception:
                return None
            entries = data.get("entries")
            return entries[0] if entries else None

        now_event = next_event = None
        try:
            now_event = _fetch("now")
        except Exception:
            pass
        try:
            next_event = _fetch("next")
        except Exception:
            pass
        return (now_event, next_event)

    def get_channel_name_by_service_uuid(self, service_uuid):
        if not service_uuid:
            return None
        try:
            for ch in self.get_channels():
                if service_uuid in (ch.get("services") or []):
                    return ch.get("name") or None
        except Exception:
            pass
        return None
