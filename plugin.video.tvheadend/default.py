# -*- coding: utf-8 -*-
"""
Tvheadend Kodi addon – default.py

Architektúra: URL routing cez query string ?action=...

Hlavné akcie:
  root                          - Hlavné menu (Live TV / Archív / kategórie / Settings)
  live_root                     - Tagy kanálov pre Live TV
  live_channels                 - Kanály v rámci tagu
  play_live                     - Spustí Live stream
  archive_by_channel            - Archív podľa kanálov (zoznam kanálov)
  archive_dates                 - Dátumy nahrávok pre kanál
  archive_day                   - Nahrávky pre konkrétny dátum kanálu
  archive_category              - Top kategória (Filmy/Seriály/...) → podžánre alebo plochý zoznam
  archive_movie_subgenre        - Filmy v podžánri (plochý zoznam)
  archive_series_subgenre       - Zoznam sérií v podžánri seriálov
  archive_series                - Epizódy konkrétneho seriálu
  archive_generic_subgenre      - Generický plochý zoznam pre top_cat+sub
  play_dvr                      - Spustí DVR nahrávku
  test_connection               - Test pripojenia
  settings                      - Otvorí Settings dialog
"""

from __future__ import annotations

import sys
from datetime import datetime
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcgui
import xbmcplugin

from lib import classifier
from lib import history
from lib import resume
from lib.errors import AddonErrorException
from lib.tvh_client import Tvheadend
from lib.util import ContentProviderShim, addon, log, tr


_HANDLE = int(sys.argv[1])
_BASE_URL = sys.argv[0]


# --------------------------------------------------------------------------
# Mapovanie kategórií na localized string IDs (strings.po)
# --------------------------------------------------------------------------
_CAT_STRING_IDS = {
    classifier.CAT_FILM:          30500,
    classifier.CAT_SERIAL:        30501,
    classifier.CAT_SPORT:         30502,
    classifier.CAT_SPRAVODAJSTVO: 30503,
    classifier.CAT_SHOW:          30504,
    classifier.CAT_DETSKE:        30505,
    classifier.CAT_HUDBA:         30506,
    classifier.CAT_UMENIE:        30507,
    classifier.CAT_DOKUMENTY:     30508,
    classifier.CAT_HOBBY:         30509,
    classifier.CAT_INE:           30510,
}


def _cat_label(cat_id: str, fallback: str) -> str:
    sid = _CAT_STRING_IDS.get(cat_id)
    if sid:
        s = tr(sid)
        if s:
            return s
    return fallback


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------
def build_url(**params) -> str:
    return _BASE_URL + "?" + urlencode(params)


def parse_args() -> dict:
    qs = sys.argv[2][1:] if len(sys.argv) > 2 and sys.argv[2].startswith("?") else ""
    return dict(parse_qsl(qs))


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------
def add_dir(label: str, url: str, icon: str | None = None,
            info: dict | None = None) -> None:
    li = xbmcgui.ListItem(label=label)
    if icon:
        li.setArt({"icon": icon, "thumb": icon})
    if info:
        try:
            tag = li.getVideoInfoTag()
            if "plot" in info:
                tag.setPlot(info["plot"])
            if "title" in info:
                tag.setTitle(info["title"])
        except Exception:
            li.setInfo("video", info)
    xbmcplugin.addDirectoryItem(_HANDLE, url, li, isFolder=True)


def add_playable(label: str, url: str, icon: str | None = None,
                 info: dict | None = None,
                 resume_uuid: str | None = None) -> None:
    li = xbmcgui.ListItem(label=label)
    li.setProperty("IsPlayable", "true")
    if icon:
        li.setArt({"icon": icon, "thumb": icon, "poster": icon})
    if info:
        try:
            tag = li.getVideoInfoTag()
            if "plot" in info:
                tag.setPlot(info["plot"])
            if "title" in info:
                tag.setTitle(info["title"])
            if "mediatype" in info:
                tag.setMediaType(info["mediatype"])
            if "duration" in info:
                try:
                    tag.setDuration(int(info["duration"]))
                except Exception:
                    pass
        except Exception:
            li.setInfo("video", info)
    # Resume properties — ak je uuid známe a v úložisku máme pozíciu, doplníme.
    # Skin (Estuary aj viaceré tretie strany) potom kreslia progress bar overlay
    # na thumbnail a v info dialógu zobrazujú "Resume from X:XX".
    if resume_uuid and _settings_bool("resume_enabled", True):
        try:
            saved = resume.get(resume_uuid)
            if saved:
                pos, total, _ts = saved
                if pos >= 30:
                    li.setProperty("ResumeTime", str(pos))
                    if total > 0:
                        li.setProperty("TotalTime", str(total))
        except Exception:
            pass
    xbmcplugin.addDirectoryItem(_HANDLE, url, li, isFolder=False)


def notify(message: str, heading: str | None = None,
           icon: str = xbmcgui.NOTIFICATION_INFO) -> None:
    xbmcgui.Dialog().notification(heading or tr(30400), message, icon, 5000)


def end_directory(succeeded: bool = True, content_type: str = "videos") -> None:
    if succeeded and content_type:
        try:
            xbmcplugin.setContent(_HANDLE, content_type)
        except Exception:
            pass
    xbmcplugin.endOfDirectory(_HANDLE, succeeded=succeeded)


def set_category(*parts: str) -> None:
    """Set Kodi plugin category header — zobrazí breadcrumb cestu typu
    'Filmy / Akčné' alebo 'Archív podľa kanálu / Markíza' v hlavičke listu.
    Volá sa na začiatku handlera (idealne pred budovaním list-u).
    Prázdne/None časti sa skipnú."""
    label = " / ".join(p for p in parts if p)
    if not label:
        return
    try:
        xbmcplugin.setPluginCategory(_HANDLE, label)
    except Exception:
        pass


def _cat_label_for_id(cat_id: str) -> str:
    """Vráti lokalizovaný label pre top-level kategóriu (Filmy, Seriály, ...).
    Wrapper okolo existujúceho _cat_label ktorý dohľadáva fallback z
    CAT_LABELS_ORDER namiesto explicitného odovzdania."""
    if not cat_id:
        return ""
    fallback = ""
    for cid, label in classifier.CAT_LABELS_ORDER:
        if cid == cat_id:
            fallback = label
            break
    return _cat_label(cat_id, fallback)


def _subcat_label(labels_tuple, sub_id: str) -> str:
    """Vráti čitateľný label pre podžáner v rámci kategórie."""
    if not sub_id:
        return ""
    for sid, label in labels_tuple:
        if sid == sub_id:
            return label
    return sub_id


# --------------------------------------------------------------------------
# TVH klient singleton
# --------------------------------------------------------------------------
_cp_shim: ContentProviderShim | None = None
_tvh_client: Tvheadend | None = None


def tvh() -> Tvheadend:
    global _cp_shim, _tvh_client
    if _tvh_client is None:
        _cp_shim = ContentProviderShim()
        _tvh_client = Tvheadend(_cp_shim)
    return _tvh_client


def require_configured() -> bool:
    if not tvh().is_configured():
        notify(tr(30401), icon=xbmcgui.NOTIFICATION_WARNING)
        return False
    return True


def _safe_int(value, default: int = 0) -> int:
    """Bezpečná konverzia hocijakej hodnoty na int.

    TVH HTTP API niekedy vracia 'number' polia ako string ('21'), niekedy
    ako float-string ('21.0'), niekedy s neviditeľnými unicode znakmi alebo
    ako None/''. Volania ako `int(value)` alebo porovnania `value > 0` na
    nepredvídateľnom type tichú failnú. Tento helper to rieši bullet-proof
    cez float pretvorenie + try/except.
    """
    if value is None or value == '':
        return default
    if isinstance(value, bool):  # bool je tiež int v Pythone — neprejaviť
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _settings_int(key: str, default: int = 0) -> int:
    """Bezpečné čítanie integer nastavenia.

    Rovnaký problém ako u `_settings_bool` — `getSettingInt()` vracia 0 pre
    nezapísané nastavenia, nevieme rozlíšiť od "explicitne 0". Preto najprv
    skúsime string accessor.
    """
    a = addon()
    try:
        raw = a.getSettingString(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw.strip())
        except (TypeError, ValueError):
            return default
    except Exception:
        pass
    try:
        v = a.getSettingInt(key)
        return int(v) if v is not None else default
    except Exception:
        return default


def _settings_bool(key: str, default: bool = False) -> bool:
    """Bezpečné čítanie boolean nastavenia.

    POZOR: `addon.getSettingBool()` vracia `False` aj keď nastavenie ešte
    NEBOLO zapísané do user storage (defaulty zo settings.xml sa pri upgrade
    addonu aplikujú lazy — až keď user prvý raz otvorí dialog Nastavenia).
    To znamená že nevieme rozlíšiť "explicitne False" od "ešte nezapísané".

    Preto najprv skúsime string accessor — ten vráti `""` pre nezapísané
    nastavenia (zatiaľ čo `True/False` sú v storage uložené ako string
    "true"/"false"). Pri prázdnom stringu padáme na default.
    """
    a = addon()
    try:
        raw = a.getSettingString(key)
        if raw is None or raw == "":
            return default  # nezapísané → použiť default zo settings.xml
        raw_lower = raw.strip().lower()
        if raw_lower in ("true", "1", "yes", "on"):
            return True
        if raw_lower in ("false", "0", "no", "off"):
            return False
        return default
    except Exception:
        pass
    try:
        return bool(a.getSettingBool(key))
    except Exception:
        return default


# --------------------------------------------------------------------------
# Common helpers pre DVR rendering
# --------------------------------------------------------------------------
def _dvr_info(entry: dict, label_title: str) -> dict:
    info: dict = {"title": label_title, "mediatype": "video"}

    def _pick(v):
        if not v:
            return ''
        if isinstance(v, dict):
            for k in ('slk', 'slo', 'cze', 'ces', 'eng'):
                if k in v and v[k]:
                    return str(v[k]).strip()
            for _val in v.values():
                if _val:
                    return str(_val).strip()
            return ''
        return str(v).strip()

    main = _pick(entry.get('disp_title') or entry.get('title'))
    sub = _pick(entry.get('disp_subtitle') or entry.get('disp_summary')
                or entry.get('subtitle') or entry.get('summary'))
    desc = _pick(entry.get('disp_description') or entry.get('description'))
    plot_parts = [p for p in (main, sub, desc) if p]
    if plot_parts:
        info['plot'] = "\n".join(plot_parts)

    try:
        dur = entry.get('duration')
        if dur:
            info['duration'] = int(dur)
        else:
            start = int(entry.get('start_real') or entry.get('start') or 0)
            stop = int(entry.get('stop_real') or entry.get('stop') or 0)
            if start and stop and stop > start:
                info['duration'] = stop - start
    except Exception:
        pass

    return info


def _build_play_dvr_url(e: dict, title: str) -> str:
    """Postaví play_dvr URL so snapshotom polí potrebných pre históriu.

    História sa ukladá v handler_play_dvr; aby tam boli k dispozícii všetky
    polia (channel, ts, ...), posielame ich cez query string.
    """
    ts_val = classifier.ts_of(e)
    try:
        duration = int(e.get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0
    sub = (e.get('disp_subtitle') or e.get('subtitle') or '')
    if isinstance(sub, dict):
        sub = next((str(v) for v in sub.values() if v), '')
    sub = (sub or '').strip()
    return build_url(
        action="play_dvr",
        dvr_url=e.get('url') or '',
        title=title or '',
        uuid=e.get('uuid') or '',
        subtitle=sub[:200],
        channelname=e.get('channelname') or '',
        channel_icon=e.get('channel_icon') or '',
        ts=str(ts_val),
        duration=str(duration),
    )


def _add_dvr_entry_item(e: dict, episode_format: bool = False) -> None:
    title = e.get('disp_title') or e.get('title') or tr(30530)
    sub = (e.get('disp_subtitle') or '').strip()
    ts_val = classifier.ts_of(e)
    dstr = datetime.fromtimestamp(ts_val).strftime('%d.%m. %H:%M') if ts_val > 0 else ''
    ch = e.get('channelname') or ''

    if episode_format:
        m = classifier.SUBTITLE_SERIES_PATTERN.match(sub)
        if m:
            ep_part = sub[:m.end()].strip()
            rest = sub[m.end():].strip()[:60]
            if rest:
                label = '%s · %s · %s · %s' % (ep_part, rest, dstr, ch)
            else:
                label = '%s · %s · %s' % (ep_part, dstr, ch)
        else:
            m2 = classifier.TITLE_EPISODE_PATTERN.search(title)
            if m2:
                ep_n = m2.group(1)
                try:
                    if not (1900 <= int(ep_n) <= 2099):
                        short_sub = sub[:50] if sub else ''
                        if short_sub:
                            label = '(%s) · %s · %s · %s' % (ep_n, short_sub, dstr, ch)
                        else:
                            label = '(%s) · %s · %s' % (ep_n, dstr, ch)
                    else:
                        label = '%s · %s · %s' % (dstr, sub[:60] or title, ch)
                except ValueError:
                    label = '%s · %s · %s' % (dstr, sub[:60] or title, ch)
            else:
                short_sub = sub[:60] if sub else title
                label = '%s · %s · %s' % (dstr, short_sub, ch)
    else:
        parts = [p for p in (dstr, title, ch) if p]
        label = ' · '.join(parts)

    icon = None
    try:
        icon = tvh().make_icon_url(e.get('channel_icon') or '')
    except Exception:
        pass

    dvr_url = e.get('url') or ''
    play_url = _build_play_dvr_url(e, title)
    add_playable(label, play_url, icon=icon, info=_dvr_info(e, label),
                 resume_uuid=e.get('uuid'))


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def handler_root() -> None:
    """Hlavné menu (Search / Live TV / Archív / kategórie / Recent / Settings)."""
    # Search – úplne hore (rýchly prístup)
    if tvh().is_configured():
        add_dir(tr(30321), build_url(action="search"),
                icon="DefaultAddonsSearch.png")

    add_dir(tr(30300), build_url(action="live_root"))
    add_dir(tr(30301), build_url(action="archive_by_channel"))

    if tvh().is_configured():
        try:
            dvr_limit = _settings_int('dvr_limit', 0)
            days_limit = _settings_int('archive_days_limit', 0)
            _, _, counts, _, _ = classifier.get_classified_dvr(
                tvh(), dvr_limit=dvr_limit, days_limit=days_limit
            )
            for cat_id, label_base in classifier.CAT_LABELS_ORDER:
                n = counts.get(cat_id, 0)
                if n <= 0:
                    continue
                label = _cat_label(cat_id, label_base)
                add_dir(label, build_url(action="archive_category", cat=cat_id))
        except Exception as e:
            log("root: classify failed (skipping categories): %s" % e, xbmc.LOGWARNING)

    # Recently watched – tesne pred Nastaveniami, iba ak niečo v histórii máme
    if _settings_bool('history_enabled', True) and history.load():
        add_dir(tr(30320), build_url(action="recent"))

    add_dir(tr(30302), build_url(action="settings"),
            icon="DefaultAddonService.png")
    end_directory(content_type="")


def handler_live_root() -> None:
    if not require_configured():
        end_directory(False)
        return

    set_category(tr(30300))  # "Live TV"
    try:
        tags = tvh().get_tags() or []
    except (AddonErrorException, Exception) as e:
        log("get_tags failed: %s" % e, xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        end_directory(False)
        return

    add_dir(tr(30310), build_url(action="live_channels", tag="", tag_name=tr(30310)))

    sorted_tags = sorted(
        (t for t in tags if t.get("name")),
        key=lambda t: (t.get("index") or 999999, (t.get("name") or "").lower()),
    )
    for tag in sorted_tags:
        name = tag.get("name") or "?"
        uuid = tag.get("uuid")
        if not uuid:
            continue
        icon_url = tag.get("icon_public_url") or ""
        icon = tvh().make_icon_url(icon_url) if icon_url else None
        add_dir(name, build_url(action="live_channels", tag=uuid, tag_name=name), icon=icon)

    end_directory()


def handler_live_channels(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return

    tag_uuid = args.get("tag", "") or ""
    tag_name = args.get("tag_name", "") or ""
    set_category(tr(30300), tag_name)  # "Live TV / <tag>"
    try:
        channels = tvh().get_channels_by_tag(tag_uuid) if tag_uuid else tvh().get_channels()
    except (AddonErrorException, Exception) as e:
        log("get_channels failed: %s" % e, xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        end_directory(False)
        return

    if not channels:
        notify(tr(30404), icon=xbmcgui.NOTIFICATION_WARNING)
        end_directory()
        return

    now_epg = {}
    try:
        now_epg = tvh().get_epg_now(limit=5000) or {}
    except Exception as e:
        log("get_epg_now soft-fail: %s" % e, xbmc.LOGDEBUG)

    def _sort_key(ch):
        num = ch.get("number")
        try:
            num = float(num) if num not in (None, "") else 999999.0
        except (TypeError, ValueError):
            num = 999999.0
        return (num, (ch.get("name") or "").lower())

    sorted_channels = sorted(
        (c for c in channels if c.get("enabled", True) and c.get("name")),
        key=_sort_key,
    )

    for ch in sorted_channels:
        uuid = ch.get("uuid")
        if not uuid:
            continue
        name = ch.get("name") or "?"
        lcn = _safe_int(ch.get("number"), 0)
        label = "%s. %s" % (lcn, name) if lcn > 0 else name

        ev = now_epg.get(uuid) or {}
        ev_title = ev.get("title") or ""
        if ev_title:
            label = "%s — %s" % (label, ev_title)

        icon_url = ch.get("icon_public_url") or ""
        icon = tvh().make_icon_url(icon_url) if icon_url else None

        info = {"title": name, "mediatype": "video"}
        plot_parts = []
        if ev_title:
            plot_parts.append(ev_title)
        if ev.get("description"):
            plot_parts.append(ev["description"])
        elif ev.get("summary"):
            plot_parts.append(ev["summary"])
        if plot_parts:
            info["plot"] = "\n\n".join(plot_parts)

        play_url = build_url(action="play_live", uuid=uuid, title=name)
        add_playable(label, play_url, icon=icon, info=info)

    end_directory()


def handler_play_live(args: dict) -> None:
    if not require_configured():
        xbmcplugin.setResolvedUrl(_HANDLE, False, xbmcgui.ListItem())
        return

    uuid = args.get("uuid") or ""
    title = args.get("title") or None
    if not uuid:
        xbmcplugin.setResolvedUrl(_HANDLE, False, xbmcgui.ListItem())
        return

    try:
        stream_url = tvh().make_live_stream_url(channel_uuid=uuid, channel_title=title)
    except AddonErrorException as e:
        log("make_live_stream_url failed: %s" % e, xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.setResolvedUrl(_HANDLE, False, xbmcgui.ListItem())
        return

    log("Playing live stream: %s" % stream_url)
    li = xbmcgui.ListItem(label=title or "", path=stream_url)
    li.setProperty("IsPlayable", "true")
    try:
        li.setMimeType("video/mp2t")
        li.setContentLookup(False)
    except Exception:
        pass
    xbmcplugin.setResolvedUrl(_HANDLE, True, li)


# --------------------------------------------------------------------------
# Archive: by channel (alternate view)
# --------------------------------------------------------------------------
def handler_archive_by_channel() -> None:
    if not require_configured():
        end_directory(False)
        return

    set_category(tr(30301))  # "Archive"
    try:
        entries = classifier.get_dvr_finished_cached(tvh())
        channels = tvh().get_channels()
    except (AddonErrorException, Exception) as e:
        log("archive_by_channel load failed: %s" % e, xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        end_directory(False)
        return

    ch_info = {}
    for ch in channels:
        cid = ch.get('uuid') or ''
        if not cid:
            continue
        ch_info[cid] = {
            'name':   ch.get('name') or cid,
            'number': _safe_int(ch.get('number'), 0),
            'icon':   tvh().make_icon_url(ch.get('icon_public_url') or ''),
        }

    days_limit = _settings_int('archive_days_limit', 0)
    if days_limit > 0:
        cutoff = datetime.now().timestamp() - days_limit * 86400
        entries = [e for e in entries if classifier.ts_of(e) >= cutoff]
    dvr_limit = _settings_int('dvr_limit', 0)
    if dvr_limit > 0:
        entries = sorted(entries, key=classifier.ts_of, reverse=True)[:dvr_limit]

    counts = {}
    days = {}
    for e in entries:
        cid = e.get('channel') or ''
        if not cid:
            continue
        counts[cid] = counts.get(cid, 0) + 1
        ts_val = classifier.ts_of(e)
        if ts_val > 0:
            days.setdefault(cid, set()).add(classifier.date_key_from_ts(ts_val))

    items = []
    for cid, cnt in counts.items():
        info = ch_info.get(cid) or {}
        items.append((
            info.get('number', 0),
            (info.get('name') or cid).lower(),
            cid,
            info.get('name') or cid,
            info.get('icon'),
            cnt,
            len(days.get(cid) or set()),
        ))

    items.sort(key=lambda x: (x[0] if x[0] > 0 else 999999, x[1]))

    days_label = tr(30531)
    for num, _norm, cid, name, icon, cnt, day_cnt in items:
        label = name
        if day_cnt > 0:
            label = '%s - %d %s' % (label, day_cnt, days_label)
        add_dir(label, build_url(action="archive_dates", channel=cid, channel_name=name),
                icon=icon, info={"title": name})

    end_directory()


def handler_archive_dates(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return

    channel_id = args.get("channel") or ""
    channel_name = args.get("channel_name") or channel_id
    set_category(tr(30301), channel_name)

    if not channel_id:
        end_directory(False)
        return

    try:
        entries = classifier.get_dvr_finished_cached(tvh())
    except Exception as e:
        log("get_dvr_finished failed: %s" % e, xbmc.LOGERROR)
        end_directory(False)
        return

    entries = [e for e in entries if (e.get('channel') or '') == channel_id]

    by_date = {}
    for e in entries:
        ts_val = classifier.ts_of(e)
        if ts_val <= 0:
            continue
        d = classifier.date_key_from_ts(ts_val)
        by_date.setdefault(d, []).append(e)

    for d in sorted(by_date.keys(), reverse=True):
        cnt = len(by_date[d])
        label = '%s (%d)' % (d, cnt)
        add_dir(label, build_url(action="archive_day", channel=channel_id,
                                  channel_name=channel_name, date=d))

    end_directory()


def handler_archive_day(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return

    channel_id = args.get("channel") or ""
    date = args.get("date") or ""
    channel_name = args.get("channel_name") or channel_id
    set_category(tr(30301), channel_name, date)
    if not (channel_id and date):
        end_directory(False)
        return

    try:
        entries = classifier.get_dvr_finished_cached(tvh())
    except Exception as e:
        log("get_dvr_finished failed: %s" % e, xbmc.LOGERROR)
        end_directory(False)
        return

    entries = [e for e in entries if (e.get('channel') or '') == channel_id]
    day = [e for e in entries
           if classifier.ts_of(e) > 0
           and classifier.date_key_from_ts(classifier.ts_of(e)) == date]
    day.sort(key=classifier.ts_of, reverse=True)

    for e in day:
        title = e.get('disp_title') or e.get('title') or tr(30530)
        ts_val = classifier.ts_of(e)
        tstr = datetime.fromtimestamp(ts_val).strftime('%H:%M') if ts_val > 0 else ''
        label = '%s %s' % (tstr, title) if tstr else title
        icon = None
        try:
            icon = tvh().make_icon_url(e.get('channel_icon') or '')
        except Exception:
            pass
        play_url = _build_play_dvr_url(e, title)
        add_playable(label, play_url, icon=icon, info=_dvr_info(e, label),
                     resume_uuid=e.get('uuid'))

    end_directory()


# --------------------------------------------------------------------------
# Archive: by category / subgenre
# --------------------------------------------------------------------------
def _load_classified():
    dvr_limit = _settings_int('dvr_limit', 0)
    days_limit = _settings_int('archive_days_limit', 0)
    return classifier.get_classified_dvr(tvh(), dvr_limit=dvr_limit, days_limit=days_limit)


def handler_archive_category(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return

    cat_id = args.get("cat") or ""
    cat_label = _cat_label_for_id(cat_id)
    set_category(cat_label)
    try:
        by_top, by_subcat, _counts, _series_by, series_subcat_titles = _load_classified()
    except Exception as e:
        log("archive_category load failed: %s" % e, xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        end_directory(False)
        return

    if cat_id == classifier.CAT_FILM:
        for sub_id, sub_label in classifier.MOVIE_SUBCAT_LABELS:
            if not by_subcat.get((classifier.CAT_FILM, sub_id)):
                continue
            add_dir(sub_label, build_url(action="archive_movie_subgenre", sub=sub_id))
        end_directory()
        return

    if cat_id == classifier.CAT_SERIAL:
        for sub_id, sub_label in classifier.MOVIE_SUBCAT_LABELS:
            if not series_subcat_titles.get((classifier.CAT_SERIAL, sub_id)):
                continue
            add_dir(sub_label, build_url(action="archive_series_subgenre", sub=sub_id))
        end_directory()
        return

    cfg = classifier.SUBCAT_REGISTRY.get(cat_id)
    if cfg and cfg[1] is not None:
        labels = cfg[0]
        for sub_id, sub_label in labels:
            if not by_subcat.get((cat_id, sub_id)):
                continue
            add_dir(sub_label,
                    build_url(action="archive_generic_subgenre", cat=cat_id, sub=sub_id))
        end_directory()
        return

    # Nezaradené – plochý zoznam
    for e in (by_top.get(cat_id) or []):
        _add_dvr_entry_item(e)
    end_directory()


def handler_archive_movie_subgenre(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return
    sub_id = args.get("sub") or ""
    set_category(_cat_label_for_id(classifier.CAT_FILM),
                 _subcat_label(classifier.MOVIE_SUBCAT_LABELS, sub_id))
    try:
        _by_top, by_subcat, _, _, _ = _load_classified()
    except Exception as e:
        log("archive_movie_subgenre load failed: %s" % e, xbmc.LOGERROR)
        end_directory(False)
        return
    for e in (by_subcat.get((classifier.CAT_FILM, sub_id)) or []):
        _add_dvr_entry_item(e)
    end_directory()


def handler_archive_generic_subgenre(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return
    cat_id = args.get("cat") or ""
    sub_id = args.get("sub") or ""
    # Pre breadcrumb potrebujeme labels_tuple z registry pre danú kategóriu
    cfg = classifier.SUBCAT_REGISTRY.get(cat_id)
    sub_lbl = _subcat_label(cfg[0], sub_id) if cfg else sub_id
    set_category(_cat_label_for_id(cat_id), sub_lbl)
    try:
        _by_top, by_subcat, _, _, _ = _load_classified()
    except Exception as e:
        log("archive_generic_subgenre load failed: %s" % e, xbmc.LOGERROR)
        end_directory(False)
        return
    for e in (by_subcat.get((cat_id, sub_id)) or []):
        _add_dvr_entry_item(e)
    end_directory()


def handler_archive_series_subgenre(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return
    sub_id = args.get("sub") or ""
    set_category(_cat_label_for_id(classifier.CAT_SERIAL),
                 _subcat_label(classifier.MOVIE_SUBCAT_LABELS, sub_id))
    try:
        _, _, _, series_by_canonical, series_subcat_titles = _load_classified()
    except Exception as e:
        log("archive_series_subgenre load failed: %s" % e, xbmc.LOGERROR)
        end_directory(False)
        return

    titles = series_subcat_titles.get((classifier.CAT_SERIAL, sub_id)) or set()
    sorted_titles = sorted(
        titles,
        key=lambda t: classifier.ts_of(series_by_canonical[t][0])
                      if series_by_canonical.get(t) else 0,
        reverse=True
    )
    for title in sorted_titles:
        eps = series_by_canonical.get(title) or []
        icon = None
        if eps:
            try:
                icon = tvh().make_icon_url(eps[0].get('channel_icon') or '')
            except Exception:
                pass
        add_dir(title, build_url(action="archive_series", title=title), icon=icon)
    end_directory()


def handler_archive_series(args: dict) -> None:
    if not require_configured():
        end_directory(False)
        return
    series_title = args.get("title") or ""
    set_category(_cat_label_for_id(classifier.CAT_SERIAL), series_title)
    try:
        _, _, _, series_by_canonical, _ = _load_classified()
    except Exception as e:
        log("archive_series load failed: %s" % e, xbmc.LOGERROR)
        end_directory(False)
        return
    eps = series_by_canonical.get(series_title) or []
    for e in eps:
        _add_dvr_entry_item(e, episode_format=True)
    end_directory()


# --------------------------------------------------------------------------
# Play DVR
# --------------------------------------------------------------------------
def handler_play_dvr(args: dict) -> None:
    if not require_configured():
        xbmcplugin.setResolvedUrl(_HANDLE, False, xbmcgui.ListItem())
        return

    dvr_url = args.get("dvr_url") or ""
    title = args.get("title") or "DVR"
    if not dvr_url:
        xbmcplugin.setResolvedUrl(_HANDLE, False, xbmcgui.ListItem())
        return

    # Zápis do histórie (zlyhanie nesmie zastaviť prehrávanie)
    try:
        if _settings_bool('history_enabled', True):
            history.add({
                'uuid':         args.get('uuid') or '',
                'title':        title,
                'subtitle':     args.get('subtitle') or '',
                'channelname':  args.get('channelname') or '',
                'channel_icon': args.get('channel_icon') or '',
                'dvr_url':      dvr_url,
                'ts':           int(args.get('ts') or 0),
                'duration':     int(args.get('duration') or 0),
            }, max_entries=_settings_int('history_limit', 50))
    except Exception as e:
        log("history add failed: %s" % e, xbmc.LOGWARNING)

    try:
        full_url = tvh().make_dvr_url(dvr_url)
    except Exception as e:
        log("make_dvr_url failed: %s" % e, xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.setResolvedUrl(_HANDLE, False, xbmcgui.ListItem())
        return

    log("Playing DVR: %s" % full_url)
    li = xbmcgui.ListItem(label=title, path=full_url)
    li.setProperty("IsPlayable", "true")
    try:
        li.setContentLookup(False)
    except Exception:
        pass

    # Resume playback — načítaj uloženú pozíciu a daj ju Kodi-mu cez properties.
    # Kodi automaticky ponúkne "Resume from X:XX / Start from beginning" dialóg.
    dvr_uuid = args.get("uuid") or ""
    resume_enabled = _settings_bool("resume_enabled", True)
    if resume_enabled and dvr_uuid:
        try:
            saved = resume.get(dvr_uuid)
            if saved:
                pos, total, _ts = saved
                if pos >= 30:  # filter musí byť konzistentný s lib/resume.py
                    li.setProperty("ResumeTime", str(pos))
                    if total > 0:
                        li.setProperty("TotalTime", str(total))
                    log("resume: ponúkam pokračovanie od %.0fs (total %.0fs) pre %s"
                        % (pos, total, dvr_uuid), xbmc.LOGDEBUG)
        except Exception as e:
            log("resume pre-play failed: %s" % e, xbmc.LOGWARNING)

    xbmcplugin.setResolvedUrl(_HANDLE, True, li)

    # Spustí tracker — blokuje plugin script kým prehrávanie nebeží.
    # Po stopnutí uloží poslednú pozíciu (alebo clearne ak dohral do konca).
    if resume_enabled and dvr_uuid:
        try:
            resume.ResumeTracker(dvr_uuid).run()
        except Exception as e:
            log("resume tracker failed: %s" % e, xbmc.LOGWARNING)


# --------------------------------------------------------------------------
# Recently watched
# --------------------------------------------------------------------------
def handler_recent(args: dict) -> None:
    """Zoznam naposledy prehraných DVR nahrávok (perzistované v profile)."""
    set_category(tr(30320))  # "Recently watched"
    entries = history.load()
    if not entries:
        notify(tr(30552))
        end_directory(succeeded=True, content_type="")
        return

    # Akcia "Vymazať históriu" ako prvá položka
    add_dir("[B]" + tr(30553) + "[/B]",
            build_url(action="history_clear"),
            info={"title": tr(30553)})

    is_configured = tvh().is_configured()

    for h in entries:
        title = h.get('title') or tr(30530)
        sub = (h.get('subtitle') or '').strip()
        ts_val = int(h.get('ts') or 0)
        played_at = int(h.get('played_at') or 0)
        ch = h.get('channelname') or ''

        played_str = (datetime.fromtimestamp(played_at).strftime('%d.%m. %H:%M')
                      if played_at > 0 else '')
        rec_str = (datetime.fromtimestamp(ts_val).strftime('%d.%m. %H:%M')
                   if ts_val > 0 else '')

        parts = []
        if played_str:
            parts.append("▶ " + played_str)
        if title:
            parts.append(title)
        if sub:
            parts.append(sub[:60])
        if ch:
            parts.append(ch)
        if rec_str and rec_str != played_str:
            parts.append(rec_str)
        label = " · ".join(parts)

        icon = None
        if is_configured:
            try:
                icon = tvh().make_icon_url(h.get('channel_icon') or '')
            except Exception:
                pass

        info = {"title": title, "mediatype": "video"}
        plot_parts = [p for p in (title, sub) if p]
        if plot_parts:
            info["plot"] = "\n".join(plot_parts)
        try:
            dur = int(h.get('duration') or 0)
            if dur > 0:
                info['duration'] = dur
        except Exception:
            pass

        # Postavíme play URL ručne (rovnaký formát ako _build_play_dvr_url)
        play_url = build_url(
            action="play_dvr",
            dvr_url=h.get('dvr_url') or '',
            title=title,
            uuid=h.get('uuid') or '',
            subtitle=sub[:200],
            channelname=ch,
            channel_icon=h.get('channel_icon') or '',
            ts=str(ts_val),
            duration=str(int(h.get('duration') or 0)),
        )
        add_playable(label, play_url, icon=icon, info=info,
                     resume_uuid=h.get('uuid'))

    end_directory()


def handler_history_clear(args: dict) -> None:
    """Vymaže celú históriu (s potvrdením)."""
    if xbmcgui.Dialog().yesno(tr(30400), tr(30554)):
        history.clear()
        notify(tr(30555))
    # Refresh aktuálneho zoznamu
    xbmc.executebuiltin("Container.Refresh")
    end_directory(succeeded=True, content_type="")


def handler_resume_clear(args: dict) -> None:
    """Vymaže všetky uložené pozície prehrávania (s potvrdením)."""
    n = resume.count()
    if n == 0:
        notify(tr(30414))  # "Žiadne uložené pozície"
        end_directory(succeeded=True, content_type="")
        return
    if xbmcgui.Dialog().yesno(tr(30400), tr(30412) % n if "%d" in tr(30412) else tr(30412)):
        cleared = resume.clear_all()
        notify(tr(30413) % cleared)
    xbmc.executebuiltin("Container.Refresh")
    end_directory(succeeded=True, content_type="")


def handler_imdb_cache_wipe(args: dict) -> None:
    """v1.0.9: Vymaže IMDb cache (online metadata lookup výsledky)."""
    try:
        from lib import imdb_lookup
        n = imdb_lookup.cache_size()
        if n == 0:
            notify(tr(30434) % 0)
            end_directory(succeeded=True, content_type="")
            return
        if xbmcgui.Dialog().yesno(tr(30400), tr(30434) % n + '\n' + tr(30432) + '?'):
            imdb_lookup.cache_wipe()
            notify(tr(30433))
    except Exception:
        pass
    end_directory(succeeded=True, content_type="")


# --------------------------------------------------------------------------
# Search (vyhľadávanie v DVR archíve, bez diakritiky)
# --------------------------------------------------------------------------
import re
_WORD_BOUNDARY_CACHE: dict[str, "re.Pattern"] = {}


def _entry_title_normalized(e: dict) -> str:
    """Vráti názov nahrávky bez diakritiky/lower pre vyhľadávanie.

    Hľadáme len v `disp_title` (resp. `title`). Podtitulok (epizóda),
    kanál ani opis NEzahŕňame — boli zdrojom false positives:
      - „Komisař Rex III" matchuje na podtitulok ako „Vražda nožem"
        (lebo bez diakritiky „nozem" obsahuje substring „noze")
      - Kanál „Prima Krimi" obsahuje „na" v rámci slova „Krimi**na**lka"
    """
    v = e.get('disp_title') or e.get('title') or ''
    if isinstance(v, dict):
        v = ' '.join(str(vv) for vv in v.values() if vv)
    return classifier.strip_accents_lower(str(v))


def _token_pattern(tok: str) -> "re.Pattern":
    """Cachovaný regex pre token — match na začiatku slova (`\\bxyz`).

    Toto zabráni tomu aby „na" matchovalo vnútrajšok „kriminalka".
    Stále povoľuje prefix matching: „kom" matchne „komisar".
    """
    pat = _WORD_BOUNDARY_CACHE.get(tok)
    if pat is None:
        pat = re.compile(r'\b' + re.escape(tok), re.UNICODE)
        _WORD_BOUNDARY_CACHE[tok] = pat
    return pat


def handler_search(args: dict) -> None:
    """Vyhľadanie v DVR archíve – v názve, bez diakritiky, prefix-of-word.

    Dvojfázové aby refresh po playbacku nezobrazoval klávesnicu:

    Fáza 1 (URL `?action=search` bez `q`):
       - Otvor klávesnicu
       - Po zadaní redirectni cez Container.Update(...,replace) na fázu 2
       - `,replace` znamená že fáza-1 URL sa zo back-stacku zahodí — po
         Back-i sa user vráti do hlavného menu, nie znovu na klávesnicu.

    Fáza 2 (URL `?action=search&q=...`):
       - Toto URL si Kodi pamätá ako aktuálnu polohu, takže pri refreshe
         (napr. po skončení prehrávania) handler beží znovu už S `q`,
         rovno vykreslí výsledky a neotvára klávesnicu.
    """
    if not require_configured():
        end_directory(False)
        return

    query_arg = args.get("q")

    # Fáza 1 — žiadny query parameter → klávesnica + redirect
    if not query_arg:
        kb = xbmc.Keyboard("", tr(30550))
        kb.doModal()
        if not kb.isConfirmed():
            end_directory(succeeded=True, content_type="")
            return
        query = (kb.getText() or "").strip()
        if not query:
            end_directory(succeeded=True, content_type="")
            return
        # Ukončíme prázdny directory listing (Kodi musí dostať odpoveď),
        # potom navigujeme na URL s `q=...` cez Container.Update s ,replace
        end_directory(succeeded=True, content_type="")
        xbmc.executebuiltin(
            "Container.Update(%s,replace)" %
            build_url(action="search", q=query)
        )
        return

    # Fáza 2 — máme `q`, vykreslíme výsledky
    query = query_arg.strip()
    set_category(tr(30321), query)  # "Search / <query>"
    if not query:
        end_directory(succeeded=True, content_type="")
        return

    norm_query = classifier.strip_accents_lower(query)
    tokens = [t for t in norm_query.split() if t]
    if not tokens:
        end_directory(succeeded=True, content_type="")
        return

    patterns = [_token_pattern(tok) for tok in tokens]

    try:
        entries = classifier.get_dvr_finished_cached(tvh())
    except (AddonErrorException, Exception) as e:
        log("search load failed: %s" % e, xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        end_directory(False)
        return

    days_limit = _settings_int('archive_days_limit', 0)
    if days_limit > 0:
        cutoff = datetime.now().timestamp() - days_limit * 86400
        entries = [e for e in entries if classifier.ts_of(e) >= cutoff]

    hits = []
    for e in entries:
        title = _entry_title_normalized(e)
        if not title:
            continue
        if all(p.search(title) for p in patterns):
            hits.append(e)

    hits.sort(key=classifier.ts_of, reverse=True)

    if not hits:
        notify(tr(30551) + ': ' + query)
        end_directory(succeeded=True, content_type="")
        return

    add_dir("[I]%s: %s (%d)[/I]" % (tr(30556), query, len(hits)),
            build_url(action="search"),  # klik = nové hľadanie (klávesnica)
            info={"title": tr(30556)})

    for e in hits:
        _add_dvr_entry_item(e)
    end_directory()


# --------------------------------------------------------------------------
# Other handlers
# --------------------------------------------------------------------------
def handler_test_connection() -> None:
    if not require_configured():
        return

    dlg = xbmcgui.Dialog()
    progress = xbmcgui.DialogProgressBG()
    progress.create(tr(30400), tr(30303))
    try:
        tvh().check_login(force_reauth=True)
        info = tvh().serverinfo()
        msg = tr(30402)
        if info:
            server_name = info.get("server_name") or info.get("name") or "TVH"
            sw_version = info.get("sw_version") or "?"
            msg = "%s\n%s %s" % (tr(30402), server_name, sw_version)
        dlg.ok(tr(30400), msg)
    except (AddonErrorException, Exception) as e:
        log("test_connection failed: %s" % e, xbmc.LOGERROR)
        dlg.ok(tr(30400), "%s\n\n%s" % (tr(30403), str(e)))
    finally:
        progress.close()


def handler_settings() -> None:
    addon().openSettings()
    # Sync the IMDb feature flag after the dialog closes (Kodi has now
    # persisted any changes the user made). See _sync_imdb_flag().
    _sync_imdb_flag()
    end_directory(succeeded=False, content_type="")


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
_ROUTES = {
    "root":                     lambda args: handler_root(),
    "live_root":                lambda args: handler_live_root(),
    "live_channels":            handler_live_channels,
    "play_live":                handler_play_live,
    "archive_by_channel":       lambda args: handler_archive_by_channel(),
    "archive_dates":            handler_archive_dates,
    "archive_day":              handler_archive_day,
    "archive_category":         handler_archive_category,
    "archive_movie_subgenre":   handler_archive_movie_subgenre,
    "archive_series_subgenre":  handler_archive_series_subgenre,
    "archive_series":           handler_archive_series,
    "archive_generic_subgenre": handler_archive_generic_subgenre,
    "play_dvr":                 handler_play_dvr,
    "recent":                   handler_recent,
    "history_clear":            handler_history_clear,
    "resume_clear":             handler_resume_clear,
    "imdb_cache_wipe":           handler_imdb_cache_wipe,
    "search":                   handler_search,
    "test_connection":          lambda args: handler_test_connection(),
    "settings":                 lambda args: handler_settings(),
}


def _sync_imdb_flag() -> None:
    """Synchronize the IMDb feature flag file with the Kodi setting value.
    This is called both from handler_settings() (after the user closes the
    Settings dialog) and from route() (once per plugin invocation, so that
    changes made via Kodi's system menu — pravý klik na addon → Nastavenia,
    which bypasses handler_settings — are also picked up).

    The flag file is what lib/imdb_lookup.py checks per-classification —
    using a file avoids per-entry Kodi setting API calls that would log
    "Invalid setting type" if the setting was never stored.

    On state change (flag created or removed), invalidate the classifier
    cache so IMDb lookup takes effect immediately on the next directory
    listing rather than after the 60-second cache TTL expires.
    """
    try:
        import os
        import xbmcvfs
        data_dir = xbmcvfs.translatePath(
            'special://profile/addon_data/plugin.video.tvheadend/')
        if not os.path.isdir(data_dir):
            os.makedirs(data_dir, exist_ok=True)
        flag = os.path.join(data_dir, 'imdb_lookup_enabled')

        was_enabled = os.path.isfile(flag)

        a = addon()
        enabled = False
        method_used = "unknown"

        # Try getSettingBool first — most direct path for boolean settings.
        try:
            enabled = bool(a.getSettingBool('online_metadata_lookup'))
            method_used = "getSettingBool"
        except Exception as e1:
            # Fallback to getSettingString
            try:
                raw = a.getSettingString('online_metadata_lookup')
                enabled = (raw or '').strip().lower() == 'true'
                method_used = "getSettingString (raw=%r)" % raw
            except Exception as e2:
                log("imdb flag sync: both Kodi setting reads failed: %s / %s"
                    % (e1, e2), xbmc.LOGWARNING)
                return

        # Only log on state change, otherwise this would spam every plugin
        # invocation (sync runs in route()).
        state_changed = (enabled != was_enabled)
        if state_changed:
            log("imdb flag sync: enabled=%s via %s (state changed: was=%s)"
                % (enabled, method_used, was_enabled), xbmc.LOGINFO)

        if enabled:
            try:
                with open(flag, 'w') as f:
                    f.write('1')
                if state_changed:
                    log("imdb flag sync: created flag file at %s" % flag,
                        xbmc.LOGINFO)
            except Exception as e:
                log("imdb flag sync: failed to create flag: %s" % e,
                    xbmc.LOGWARNING)
        else:
            if os.path.isfile(flag):
                try:
                    os.remove(flag)
                    if state_changed:
                        log("imdb flag sync: removed flag file", xbmc.LOGINFO)
                except Exception as e:
                    log("imdb flag sync: failed to remove flag: %s" % e,
                        xbmc.LOGWARNING)

        # If the flag state changed (enabled toggled on or off), invalidate
        # the classifier cache so the next listing re-runs classification
        # with the new IMDb lookup behaviour rather than serving stale
        # cached output. Otherwise the user has to wait up to 60 seconds
        # for the cache TTL to expire after toggling the setting.
        if state_changed:
            try:
                from lib import classifier as _classifier_mod
                _classifier_mod.invalidate_caches()
                log("imdb flag sync: invalidated classifier cache",
                    xbmc.LOGINFO)
            except Exception as e:
                log("imdb flag sync: cache invalidation failed: %s" % e,
                    xbmc.LOGWARNING)
    except Exception as e:
        log("imdb flag sync failed: %s" % e, xbmc.LOGWARNING)


def route() -> None:
    args = parse_args()
    # v1.0.4: Wire-up classifier debug setting once per route invocation
    try:
        from lib import classifier as _classifier_mod
        _classifier_mod.set_classifier_debug(
            _settings_bool("classifier_debug", False),
            log_fn=xbmc.log,
        )
    except Exception:
        pass
    # v1.0.9: Proactive IMDb flag sync once per plugin invocation. Covers
    # the case where the user toggled the setting via Kodi's system menu
    # (right-click on addon → Nastavenia) which bypasses handler_settings.
    _sync_imdb_flag()
    action = args.get("action") or "root"
    handler = _ROUTES.get(action)
    if handler is None:
        log("Unknown action: %r — falling back to root" % action, xbmc.LOGWARNING)
        handler_root()
        return
    try:
        handler(args)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log("Handler %r crashed: %s\n%s" % (action, e, tb), xbmc.LOGERROR)
        notify(str(e), icon=xbmcgui.NOTIFICATION_ERROR)
        try:
            end_directory(False)
        except Exception:
            pass


if __name__ == "__main__":
    route()
