# -*- coding: utf-8 -*-
"""
DVR klasifikátor – top-level kategórie + podžánre.

1:1 port z plugin_video_tvheadend/provider.py (0.49d) riadky 281–1356.
Žiadne logické zmeny — len:
  - Odstránené ArchivCZSK závislosti (_strip_accents_compat → lokálna impl)
  - Import re, unicodedata namiesto _re_dvr/_unicodedata_dvr aliasov
  - Cache `_get_classified_dvr` ostáva ako bola; `_DVR_CACHE` nahradený
    `_SimpleTTLCache` z tvh_client (rovnaký interface)
  - Externally-used identifiers exposed without leading underscore
    (CAT_*, CAT_LABELS_ORDER, SUBCAT_REGISTRY, classify_dvr_entry, atď.)

Vstup: DVR entry dict z TVH `/api/dvr/entry/grid_finished` (kľúče
disp_title, disp_subtitle, disp_description, channelname, content_type,
genre, episode_disp, start_real, start, stop, duration, ...).

Výstup: (top_cat, sub_cat) tuple. sub_cat môže byť None pre kategórie
bez podžánrov (_CAT_INE).
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime

from .tvh_client import _SimpleTTLCache


# --------------------------------------------------------------------------
# Debug klasifikátora (v1.0.4)
# --------------------------------------------------------------------------
# Po aktivácii v settings ("classifier_debug") zapisuje každý DVR entry
# do xbmc.log s detailmi prečo skončil v danej kategórii. Lets users (a aj
# reviewerov fóra) hlásiť konkrétne misklasifikácie aj s context-om.
_debug_enabled = False  # set externally via set_classifier_debug()
_xbmc_log = None        # injectnuté lazy aby bol modul testovateľný mimo Kodi


def set_classifier_debug(enabled, log_fn=None):
    """Aktivuje/deaktivuje diagnostické logovanie klasifikátora.

    Volá sa z plugin entry-pointu po načítaní setting hodnoty.
    log_fn: callable(message, level) — typicky xbmc.log; ak None, použijeme print.
    """
    global _debug_enabled, _xbmc_log
    _debug_enabled = bool(enabled)
    _xbmc_log = log_fn


def _dbg_log(entry, top, sub, reason):
    """Zapíše jeden riadok do logu o klasifikácii entry."""
    if not _debug_enabled:
        return
    title = (entry.get('disp_title') or '?')[:60]
    channel = (entry.get('channelname') or '?')[:25]
    ct = entry.get('content_type', 0)
    genre = entry.get('genre') or []
    msg = ('[classifier] "{title}" [{ch}] ct={ct} genre={g} '
           '→ {top}/{sub} ({reason})').format(
        title=title, ch=channel, ct=ct, g=genre,
        top=top, sub=(sub or '-'), reason=reason)
    if _xbmc_log is not None:
        try:
            _xbmc_log(msg, 1)  # xbmc.LOGINFO = 1
            return
        except Exception:
            pass
    # Fallback (mimo Kodi alebo log_fn fail)
    try:
        print(msg)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Diakritika strip + lower
# --------------------------------------------------------------------------
def _strip_accents_lower(s: str) -> str:
    """Vráti text bez diakritiky a v lowercase. Pre regex match."""
    if not s:
        return ''
    nfd = unicodedata.normalize('NFD', s)
    stripped = ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')
    return stripped.lower()


# --------------------------------------------------------------------------
# Regex patterns
# --------------------------------------------------------------------------
_SUBTITLE_SERIES_PATTERN = re.compile(r'^\s*\d+/\d+\b')
_TITLE_EPISODE_PATTERN = re.compile(r'\((\d{1,4})\)\s*(?:\([A-Z]{1,3}\))?\s*$')

# Single tech/audio/subtitle marker — rozšír ak narazíš na ďalší
# Pozn.: DTS-HD musí byť pred DTS aby alternace zachytila dlhšiu variantu
_TECH_MARKER = (
    r'(?:DD5\.1|DTS-HD|DTS-MA|UHD|DTS|5\.1|7\.1|ST|HD|AD|SS|3D|DD|TT|P)'
)
# Parens s 1+ tech markermi, oddelenými čiarkou alebo lomkou
# (s alebo bez whitespace). Rieši napr. "(AD,ST)", "(HD, DD5.1)", "(AD/ST)".
_TECH_MARKER_PATTERN = re.compile(
    r'\s*\(\s*' + _TECH_MARKER +
    r'(?:\s*[,/]\s*' + _TECH_MARKER + r')*\s*\)\s*',
    re.IGNORECASE
)


def _strip_tech_markers(text: str) -> str:
    if not text:
        return ''
    return _TECH_MARKER_PATTERN.sub(' ', text).strip()


def _has_episode_suffix(title: str) -> bool:
    """True ak title končí '(N)' a N je epizoda (nie rok 1900-2099)."""
    clean = _strip_tech_markers(title)
    m = _TITLE_EPISODE_PATTERN.search(clean)
    if not m:
        return False
    try:
        n = int(m.group(1))
    except (ValueError, TypeError):
        return False
    if 1900 <= n <= 2099:
        return False
    if 1 <= n <= 9999:
        return True
    return False


def series_canonical_title(title: str) -> str:
    """Strip episode suffix + tech markers — aby sa epizódy toho istého seriálu
    dali grupovať pod jeden názov."""
    if not title:
        return ''
    clean = _strip_tech_markers(title).strip()
    m = _TITLE_EPISODE_PATTERN.search(clean)
    if m:
        try:
            n = int(m.group(1))
            if not (1900 <= n <= 2099):
                clean = clean[:m.start()].strip()
        except (ValueError, TypeError):
            pass
    return clean


# --------------------------------------------------------------------------
# Top-level kategórie
# --------------------------------------------------------------------------
CAT_FILM = 'film'
CAT_SERIAL = 'serial'
CAT_SPRAVODAJSTVO = 'spravodajstvo'
CAT_SHOW = 'show'
CAT_SPORT = 'sport'
CAT_DETSKE = 'detske'
CAT_HUDBA = 'hudba'
CAT_UMENIE = 'umenie'
CAT_DOKUMENTY = 'dokumenty'
CAT_HOBBY = 'hobby'
CAT_INE = 'ine'

_CT_TO_CAT_BASE = {
    2:  CAT_SPRAVODAJSTVO,
    3:  CAT_SHOW,
    4:  CAT_SPORT,
    5:  CAT_DETSKE,
    6:  CAT_HUDBA,
    7:  CAT_UMENIE,
    8:  CAT_SHOW,
    9:  CAT_DOKUMENTY,
    10: CAT_HOBBY,
}

CAT_LABELS_ORDER = (
    (CAT_FILM,          'Filmy'),
    (CAT_SERIAL,        'Seriály'),
    (CAT_SPORT,         'Šport'),
    (CAT_SPRAVODAJSTVO, 'Spravodajstvo'),
    (CAT_SHOW,          'Šou / Relácie'),
    (CAT_DETSKE,        'Detské'),
    (CAT_HUDBA,         'Hudba'),
    (CAT_UMENIE,        'Umenie / Kultúra'),
    (CAT_DOKUMENTY,     'Dokumenty / Vzdelávacie'),
    (CAT_HOBBY,         'Voľný čas / Hobby'),
    (CAT_INE,           'Nezaradené'),
)


# --------------------------------------------------------------------------
# Filmy/Seriály podžánre
# --------------------------------------------------------------------------
_MV_AKCNY = 'mv_akcny'
_MV_DRAMA = 'mv_drama'
_MV_KOMEDIA = 'mv_komedia'
_MV_KRIMI = 'mv_krimi'
_MV_SCIFI = 'mv_scifi'
_MV_ROMANTIKA = 'mv_romantika'
_MV_HOROR = 'mv_horor'
_MV_DOBRODR = 'mv_dobrodruzny'
_MV_ANIMAK = 'mv_animovany'
_MV_HISTORICKY = 'mv_historicky'
_MV_WESTERN = 'mv_western'
_MV_INE = 'mv_ine'

MOVIE_SUBCAT_LABELS = (
    (_MV_AKCNY,      'Akčné'),
    (_MV_KOMEDIA,    'Komédia'),
    (_MV_KRIMI,      'Krimi / Thriller / Detektívka'),
    (_MV_DRAMA,      'Drama'),
    (_MV_SCIFI,      'Sci-fi / Fantasy'),
    (_MV_ROMANTIKA,  'Romantické'),
    (_MV_HOROR,      'Horor'),
    (_MV_DOBRODR,    'Dobrodružné'),
    (_MV_ANIMAK,     'Animované'),
    (_MV_HISTORICKY, 'Historické / Vojnové'),
    (_MV_WESTERN,    'Western'),
    (_MV_INE,        'Iné'),
)

_DVB_GENRE_TO_SUBCAT = {
    0x11: _MV_KRIMI,
    0x12: _MV_DOBRODR,
    0x13: _MV_SCIFI,
    0x14: _MV_KOMEDIA,
    0x15: _MV_DRAMA,
    0x16: _MV_ROMANTIKA,
    0x17: _MV_HISTORICKY,
    0x18: _MV_DRAMA,
}

# DVB-SI Level 2 (sub-nibble) mapovanie pre ostatné top kategórie (v1.0.4).
# Iba 6.5% entries má `genre` pole, ale keď ho má, je to spoľahlivejší signál
# ako keyword scan. Lookup je per-top-category, lebo rovnaký Level 2 nibble
# znamená iné veci pre rôzne Level 1.
_DVB_L2_BY_TOP = {}  # vyplnené nižšie po definícii subkategórií


def _dvb_l2_subgenre(entry, top_cat):
    """Vráti subgenre z DVB genre kódu (Level 1 = top nibble, Level 2 = bottom).
    None ak entry nemá genre alebo nie je v mappingu."""
    mapping = _DVB_L2_BY_TOP.get(top_cat)
    if not mapping:
        return None
    for g in (entry.get('genre') or []):
        try:
            g = int(g)
        except (ValueError, TypeError):
            continue
        sub = mapping.get(g)
        if sub:
            return sub
    return None

_KEYWORD_TO_SUBCAT = (
    # Specifické signály ako prvé (animované, sci-fi, western, vojnové) — tieto
    # zriedka generujú false positives a sú prioritné nad generickými
    (re.compile(r'\b(sci-?fi|sci\.\s?fi|fantasy|vedeckofant|vesmirn|mimozem|robot|kybern)'),
     _MV_SCIFI),
    (re.compile(r'\b(animovan|kreslen|animak|loutkov|cartoon|anime)'),
     _MV_ANIMAK),
    (re.compile(r'\b(western|kovbo)'),
     _MV_WESTERN),
    (re.compile(r'\b(historick|valecn|vojensk|vojnov|histori)'),
     _MV_HISTORICKY),
    (re.compile(r'\b(detektiv|kriminal|krimi|thriller|vraz|policajn|vysetrov)'),
     _MV_KRIMI),
    (re.compile(r'\b(komedi|veselohra|humor|grotesk|sitcom)'),
     _MV_KOMEDIA),
    (re.compile(r'\b(romantick|milostn|romant)'),
     _MV_ROMANTIKA),
    (re.compile(r'\b(akcn|action|honic|prestrelk)'),
     _MV_AKCNY),
    (re.compile(r'\b(dobrodruz|adventur|exped|cestopis)'),
     _MV_DOBRODR),
    (re.compile(r'\b(drama|dramati)'),
     _MV_DRAMA),
    # Horor je na konci — keywordy 'desiv'/'hruz' sa často objavujú v opisoch
    # vojnových filmov a thrillerov. Špecifickejšie kategórie (historicky,
    # krimi, akcny) preto vyhrávajú prv než horor (v1.0.4).
    (re.compile(r'\b(horor|horror|desiv|hruz)'),
     _MV_HOROR),
)


# --------------------------------------------------------------------------
# Šport podžánre
# --------------------------------------------------------------------------
_SP_FUTBAL = 'sp_futbal'
_SP_HOKEJ = 'sp_hokej'
_SP_BASKETBAL = 'sp_basketbal'
_SP_TENIS = 'sp_tenis'
_SP_VOLEJBAL = 'sp_volejbal'
_SP_HADZANA = 'sp_hadzana'
_SP_ATLETIKA = 'sp_atletika'
_SP_CYKLISTIKA = 'sp_cyklistika'
_SP_MOTORSPORT = 'sp_motorsport'
_SP_BOJOVE = 'sp_bojove'
_SP_ZIMNE = 'sp_zimne'
_SP_VODNE = 'sp_vodne'
_SP_NEWS = 'sp_news'
_SP_INE = 'sp_ine'

SPORT_SUBCAT_LABELS = (
    (_SP_FUTBAL,      'Futbal'),
    (_SP_HOKEJ,       'Hokej'),
    (_SP_BASKETBAL,   'Basketbal'),
    (_SP_TENIS,       'Tenis'),
    (_SP_VOLEJBAL,    'Volejbal'),
    (_SP_HADZANA,     'Hádzaná'),
    (_SP_ATLETIKA,    'Atletika'),
    (_SP_CYKLISTIKA,  'Cyklistika'),
    (_SP_MOTORSPORT,  'Motorsport'),
    (_SP_BOJOVE,      'Bojové športy'),
    (_SP_ZIMNE,       'Zimné športy'),
    (_SP_VODNE,       'Vodné športy'),
    (_SP_NEWS,        'Športové spravodajstvo'),
    (_SP_INE,         'Iné'),
)

_SPORT_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(sportovni\s+noviny|sportove\s+noviny|sport\s+news|'
                r'spravy\s+zo\s+sportu|sportovni\s+studio|sports?\s+report|'
                r'polední\s+sport|odpoledni\s+sport)'),
     _SP_NEWS),
    (re.compile(r'\b(hokej|hockey|nhl|iihf|khl|hokejov)'),
     _SP_HOKEJ),
    (re.compile(r'\b(ufc|mma|oktagon|kickbox|k-1|judo|karate|wrestl|'
                r'zapas|sumo|taekwon|grappling)'),
     _SP_BOJOVE),
    (re.compile(r'\bbox(er|ing|u|y)?\b'),
     _SP_BOJOVE),
    (re.compile(r'\b(futbal|football|uefa|monacobet|nike\s+liga|niké\s+liga|'
                r'tipsport\s+liga|fortuna\s+liga|premier\s+league|bundesliga|'
                r'la\s+liga|champion(s)?\s+league|europa\s+league|conference\s+league|'
                r'ligue\s+1|serie\s+a\b|el\s+uefa|cl\s+uefa)'),
     _SP_FUTBAL),
    (re.compile(r'\b(basketbal|basketbol|nba|euroliga\s+basketbal|sbl|wnba)'),
     _SP_BASKETBAL),
    (re.compile(r'\b(volejbal|volleyball)'),
     _SP_VOLEJBAL),
    (re.compile(r'\b(hadzana|handball)'),
     _SP_HADZANA),
    (re.compile(r'\b(tenis|tennis|atp|wta|wimbledon|roland\s+garros|'
                r'us\s+open|australian\s+open|french\s+open)'),
     _SP_TENIS),
    (re.compile(r'\b(cyklist|tour\s+de\s+france|giro\s+d|vuelta)'),
     _SP_CYKLISTIKA),
    (re.compile(r'\b(formula|formule|f1\b|motogp|wrc|rally|nascar|'
                r'moto2|moto3|velka\s+cena|grand\s+prix)'),
     _SP_MOTORSPORT),
    (re.compile(r'\b(zoh|olympi.*zimn|zimn.*olympi|lyzov|lyziarsk|'
                r'biatlon|snowboard|sjazd|slalom|krasokorcul|cortina\s+2026|'
                r'milano\s+cortina)'),
     _SP_ZIMNE),
    (re.compile(r'\b(kanoistik|plavan|plav(ec|ky)|jachting|surf|veslov|'
                r'kayaking|swimming|vodn[ey]\s+polo|vodne\s+slalom|'
                r'rychlostna\s+kanoistik)'),
     _SP_VODNE),
    (re.compile(r'\b(atletik|atletic|athletics|maraton|marathon|'
                r'beh\s+na|skok\s+do|hod\s+ostepom|dialk)'),
     _SP_ATLETIKA),
)


def _sport_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _SP_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _SPORT_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _SP_INE


# --------------------------------------------------------------------------
# Spravodajstvo podžánre
# --------------------------------------------------------------------------
_NW_HLAVNE = 'nw_hlavne'
_NW_POLITIKA = 'nw_politika'
_NW_KRIMI = 'nw_krimi'
_NW_MAGAZINY = 'nw_magaziny'
_NW_POCASIE = 'nw_pocasie'
_NW_INE = 'nw_ine'

NEWS_SUBCAT_LABELS = (
    (_NW_HLAVNE,    'Hlavné správy'),
    (_NW_POLITIKA,  'Politika / Diskusie'),
    (_NW_KRIMI,     'Krimi / Reportáže'),
    (_NW_MAGAZINY,  'Magazíny / Lifestyle'),
    (_NW_POCASIE,   'Počasie'),
    (_NW_INE,       'Iné'),
)

_NEWS_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(pocasi|predpoved|predpovid)'),
     _NW_POCASIE),
    (re.compile(r'\b(krimi\s+noviny|reporter|reportaz|investigativ|'
                r'tajomstv|kriminal(ne)?\s+sprav|cernin)'),
     _NW_KRIMI),
    (re.compile(r'\b(politik|diskusia|diskuse|debata|otazk|otazky\s+vaclava|'
                r'studio\s+6|o\s+5\s+minut\s+12|polemika|interview\s+plus|'
                r'partia|sobotne\s+dial)'),
     _NW_POLITIKA),
    (re.compile(r'\b(magazin|spravodajsky\s+magazin|reflex\b|'
                r'7\s+dni|plus\s+7|fokus|profil|lifestyle)'),
     _NW_MAGAZINY),
    (re.compile(r'\b(noviny|sprav[yi]|udalosti|hlavni\s+sprav|hlavne\s+sprav|'
                r'tv\s+noviny|112\b|noviny\s+plus|teleráno|telerano|'
                r'spravy\s+rtvs|sledovanie\s+spravodajstv|spravodajstv)'),
     _NW_HLAVNE),
)


def _news_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _NW_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _NEWS_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _NW_INE


# --------------------------------------------------------------------------
# Šou / Relácie podžánre
# --------------------------------------------------------------------------
_SH_REALITY = 'sh_reality'
_SH_TALK = 'sh_talk'
_SH_SUTAZ = 'sh_sutaz'
_SH_KUCHARSKE = 'sh_kucharske'
_SH_ZABAVA = 'sh_zabava'
_SH_MAGAZINY = 'sh_magaziny'
_SH_INE = 'sh_ine'

SHOW_SUBCAT_LABELS = (
    (_SH_REALITY,    'Reality show'),
    (_SH_SUTAZ,      'Súťažné show / Talenty'),
    (_SH_KUCHARSKE,  'Kuchárske show'),
    (_SH_TALK,       'Talk show'),
    (_SH_ZABAVA,     'Zábava / Humor'),
    (_SH_MAGAZINY,   'Magazíny'),
    (_SH_INE,        'Iné'),
)

_SHOW_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(kucharsk|masterchef|hell\'?s\s+kitchen|'
                r'ano,?\s+sefe|jamie\s+oliver|recept|kuchar|kucharka|'
                r'gordon\s+ramsay)'),
     _SH_KUCHARSKE),
    (re.compile(r'\b(reality\s?show|farmer|farma\b|survivor|big\s+brother|'
                r'rande|love\s+island|vyzva\b|prezit|hlada\s+sa|holky\s+z|'
                r'mama\s+ja\s+chcem)'),
     _SH_REALITY),
    (re.compile(r'\b(talent\b|x\s?factor|got\s+talent|the\s+voice|'
                r'superstar|tvoja\s+tvar|hviezda|dancing\s+with|'
                r'cesko\s+slovenska|stardance|let\'?s\s+dance)'),
     _SH_SUTAZ),
    (re.compile(r'\b(talk\s?show|show\s+jana\s+krausa|late\s+night|'
                r'kraus\b|particka|cestou\s+necestou|vy(2|3|4)\s+show)'),
     _SH_TALK),
    (re.compile(r'\b(magazin|reflex\b|zivot\s+v\s+luxuse|'
                r'plus\s+7\s+dni|5\s+proti\s+5|inkognito|klic|'
                r'lifestyle|polopate)'),
     _SH_MAGAZINY),
    (re.compile(r'\b(zabavn|humor|estrad|skecz|stand-?up|parodi|'
                r'sranda|veselohra|kabaret|satira)'),
     _SH_ZABAVA),
)


def _show_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _SH_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _SHOW_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _SH_INE


# --------------------------------------------------------------------------
# Detské podžánre
# --------------------------------------------------------------------------
_CH_ANIMAK = 'ch_animak'
_CH_ROZPRAVKY = 'ch_rozpravky'
_CH_VZDELAVAC = 'ch_vzdelavac'
_CH_FILMY = 'ch_filmy'
_CH_INE = 'ch_ine'

CHILDREN_SUBCAT_LABELS = (
    (_CH_ANIMAK,     'Animované / Kreslené'),
    (_CH_ROZPRAVKY,  'Rozprávky'),
    (_CH_VZDELAVAC,  'Vzdelávacie'),
    (_CH_FILMY,      'Filmy pre deti'),
    (_CH_INE,        'Iné'),
)

_CHILDREN_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(rozpravk|pohadk|princ\b|princezn|'
                r'kralovstvo|carodej)'),
     _CH_ROZPRAVKY),
    (re.compile(r'\b(animovan|kreslen|loutkov|cartoon|anime|animak)'),
     _CH_ANIMAK),
    (re.compile(r'\b(kouzeln[aé]?\s+skolk|studio\s+kamar|vzdelavac|'
                r'vyuka|naucn|edukacn|do\s+skoly)'),
     _CH_VZDELAVAC),
    (re.compile(r'\b(detsk[yi]\s+film|pre\s+deti\s+film|family\s+film|'
                r'rodinny\s+film)'),
     _CH_FILMY),
)


def _children_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _CH_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _CHILDREN_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _CH_INE


# --------------------------------------------------------------------------
# Hudba podžánre
# --------------------------------------------------------------------------
_MU_KLASIKA = 'mu_klasika'
_MU_KONCERT = 'mu_koncert'
_MU_HITY = 'mu_hity'
_MU_FOLK = 'mu_folk'
_MU_MAGAZINY = 'mu_magaziny'
_MU_INE = 'mu_ine'

MUSIC_SUBCAT_LABELS = (
    (_MU_KONCERT,   'Koncerty'),
    (_MU_KLASIKA,   'Klasická hudba / Opera'),
    (_MU_HITY,      'Hitparáda / Pop'),
    (_MU_FOLK,      'Folk / Country / Ľudová'),
    (_MU_MAGAZINY,  'Hudobné magazíny'),
    (_MU_INE,       'Iné'),
)

_MUSIC_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(klasick[ay]\s+hudb|opera|symfoni|filharmon|'
                r'orchester|orchestr|arie|arij|koncert\s+klasick|smetanova|'
                r'ma\s+vlast)'),
     _MU_KLASIKA),
    (re.compile(r'\b(koncert\b|live\s+concert|tour\s+(world|live)|'
                r'mtv\s+live|unplugged)'),
     _MU_KONCERT),
    (re.compile(r'\b(folk\b|country|ludova\s+hudba|lidova\s+hudba|'
                r'cimbal|ludovk|lidovk|ciganska\s+hudba|folklor)'),
     _MU_FOLK),
    (re.compile(r'\b(hitparad|top\s+\d+|chart|charts|pop\b|popmusic|'
                r'pisnicky\s+z\s+obrazovky|videoklip)'),
     _MU_HITY),
    (re.compile(r'\b(hudobn[ye]\s+magaz|music\s+news|hudba\s+\d|hudobnik)'),
     _MU_MAGAZINY),
)


def _music_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _MU_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _MUSIC_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _MU_INE


# --------------------------------------------------------------------------
# Umenie / Kultúra podžánre
# --------------------------------------------------------------------------
_AR_DIVADLO = 'ar_divadlo'
_AR_FILM = 'ar_film'
_AR_VYTVARNE = 'ar_vytvarne'
_AR_LITERATURA = 'ar_literatura'
_AR_INE = 'ar_ine'

ARTS_SUBCAT_LABELS = (
    (_AR_DIVADLO,    'Divadlo'),
    (_AR_FILM,       'Filmové umenie'),
    (_AR_VYTVARNE,   'Výtvarné umenie / Maľba'),
    (_AR_LITERATURA, 'Literatúra / Knihy'),
    (_AR_INE,        'Iné'),
)

_ARTS_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(divadl|theater|inscenace|cinohra|opera\s+plus|baletn|'
                r'cinoherni)'),
     _AR_DIVADLO),
    (re.compile(r'\b(vytvarn|malba|maliarstv|socharst|galeri|'
                r'umelci|umelec|art\s+(gallery|show)|vystav)'),
     _AR_VYTVARNE),
    (re.compile(r'\b(literatur|literar|knih[ay]|kniha\b|spisovate|'
                r'roman\b|prozaik|poezi|basen|kniznic)'),
     _AR_LITERATURA),
    (re.compile(r'\b(filmov[ey]\s+umen|filmov[ya]\s+klasik|filmovi\s+tvorco|'
                r'reziser|kameraman|filmari)'),
     _AR_FILM),
)


def _arts_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _AR_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _ARTS_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _AR_INE


# --------------------------------------------------------------------------
# Dokumenty / Vzdelávacie podžánre
# --------------------------------------------------------------------------
_DC_PRIRODA = 'dc_priroda'
_DC_HISTORIA = 'dc_historia'
_DC_VEDA = 'dc_veda'
_DC_CESTOPIS = 'dc_cestopis'
_DC_SPOLOCNOST = 'dc_spolocnost'
_DC_OSOBNOSTI = 'dc_osobnosti'
_DC_INE = 'dc_ine'

DOCS_SUBCAT_LABELS = (
    (_DC_PRIRODA,    'Príroda / Zvieratá'),
    (_DC_HISTORIA,   'História / Archeológia'),
    (_DC_VEDA,       'Veda / Technika / Vesmír'),
    (_DC_CESTOPIS,   'Cestopisy / Geografia'),
    (_DC_SPOLOCNOST, 'Spoločnosť / Politika'),
    (_DC_OSOBNOSTI,  'Osobnosti / Biografie'),
    (_DC_INE,        'Iné'),
)

_DOCS_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(prirod|zviera|zvire|zivocich|zivocisn|'
                r'fauna|flora|narodny\s+park|narodni\s+park|safari|'
                r'ocean|dzungla|jerab|orel|sokol|tiger|delfin|velryba|'
                r'animal\s+planet|kralovstvo\s+divociny|kralovstvi\s+divociny)'),
     _DC_PRIRODA),
    (re.compile(r'\b(histori|dejiny|stredovek|stredovek|archeo|'
                r'antick|stara\s+civiliza|imperi|cisar|kral|'
                r'pyramid|rimsk|grecka\s+civi|stredovek)'),
     _DC_HISTORIA),
    (re.compile(r'\b(veda|vedeck|fyzik|chemi|biolog|'
                r'matematik|technika|technolog|vesmir|kozmos|'
                r'planeta|nasa|esa\s+\w|raketa|vynalez|umela\s+inteligenci)'),
     _DC_VEDA),
    (re.compile(r'\b(cestopis|cesty|cestou\s+necestou|krajiny|cestovate|'
                r'expedici|expedice|geografi|narody\s+sveta)'),
     _DC_CESTOPIS),
    (re.compile(r'\b(biografi|portret\s+osob|osobnost|zivotopis|zivot\s+a\s+dielo|'
                r'pamati|memoare|spomienky\s+na|zivot\s+a\s+\w)'),
     _DC_OSOBNOSTI),
    (re.compile(r'\b(spoloc|spolecn|ekonom|politick[ay]\s+dokum|kapitalizm|'
                r'globali|investigativ\s+dokum|chudoba|migra|trzn[ay]\s+ekonomik)'),
     _DC_SPOLOCNOST),
)


def _docs_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _DC_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _DOCS_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _DC_INE


# --------------------------------------------------------------------------
# Voľný čas / Hobby podžánre
# --------------------------------------------------------------------------
_HB_ZAHRADA = 'hb_zahrada'
_HB_BYVANIE = 'hb_byvanie'
_HB_VARENIE = 'hb_varenie'
_HB_AUTO = 'hb_auto'
_HB_CESTOVANIE = 'hb_cestovanie'
_HB_ZDRAVIE = 'hb_zdravie'
_HB_DIY = 'hb_diy'
_HB_INE = 'hb_ine'

HOBBY_SUBCAT_LABELS = (
    (_HB_ZAHRADA,    'Záhrada'),
    (_HB_BYVANIE,    'Bývanie / Renovácie'),
    (_HB_VARENIE,    'Vaření / Recepty'),
    (_HB_AUTO,       'Auto / Moto'),
    (_HB_CESTOVANIE, 'Cestovanie'),
    (_HB_ZDRAVIE,    'Zdravie / Fitness'),
    (_HB_DIY,        'Kutilstvo / DIY'),
    (_HB_INE,        'Iné'),
)

_HOBBY_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(zahrad|kvetin|sklenik|tri\s+v\s+zahrade|'
                r'okrasn[ay]\s+rastlin)'),
     _HB_ZAHRADA),
    (re.compile(r'\b(byvan|interier|renovac|architektur|'
                r'rekonstruk|nabytk|kuchyna\s+(snov|sna|dizajn)|bydleni)'),
     _HB_BYVANIE),
    (re.compile(r'\b(varen|recept|jedl[oa]|kuchar(stvo|ka|i)?|'
                r'peciem|s\s+kuchar|kucharka|babickovy)'),
     _HB_VARENIE),
    (re.compile(r'\b(auto\b|moto\b|automobil|motorka|automotive|'
                r'autosalon|garaz)'),
     _HB_AUTO),
    (re.compile(r'\b(cestovan|cestujeme|destinac|hotel\s+test|'
                r'vylety|vylet\s+po|destination|on\s+the\s+road|cestopis|'
                r'z\s+metropol)'),
     _HB_CESTOVANIE),
    (re.compile(r'\b(zdrav[ie]\s+|fitness|cvicen|wellness|'
                r'beh\s+v\s+meste|zivotospravu|chudnut)'),
     _HB_ZDRAVIE),
    (re.compile(r'\b(kutil|diy\b|hand\s+made|vlastnorucn|svojpomocn|'
                r'workshop|tvorime|dilna)'),
     _HB_DIY),
)


def _hobby_subgenre(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return _HB_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _HOBBY_KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _HB_INE


# --------------------------------------------------------------------------
# Registry: top_cat → (labels, subgenre_fn)
# --------------------------------------------------------------------------
SUBCAT_REGISTRY = {
    CAT_FILM:          (MOVIE_SUBCAT_LABELS,    None),   # špeciálne
    CAT_SERIAL:        (MOVIE_SUBCAT_LABELS,    None),   # špeciálne
    CAT_SPORT:         (SPORT_SUBCAT_LABELS,    _sport_subgenre),
    CAT_SPRAVODAJSTVO: (NEWS_SUBCAT_LABELS,     _news_subgenre),
    CAT_SHOW:          (SHOW_SUBCAT_LABELS,     _show_subgenre),
    CAT_DETSKE:        (CHILDREN_SUBCAT_LABELS, _children_subgenre),
    CAT_HUDBA:         (MUSIC_SUBCAT_LABELS,    _music_subgenre),
    CAT_UMENIE:        (ARTS_SUBCAT_LABELS,     _arts_subgenre),
    CAT_DOKUMENTY:     (DOCS_SUBCAT_LABELS,     _docs_subgenre),
    CAT_HOBBY:         (HOBBY_SUBCAT_LABELS,    _hobby_subgenre),
}


# DVB-SI Level 2 → subgenre mapovanie per top kategória (v1.0.4).
# Naplnené tu, lebo potrebujeme aby subgenre konštanty boli už definované.
_DVB_L2_BY_TOP.update({
    CAT_SPORT: {
        0x43: _SP_FUTBAL, 0x44: _SP_TENIS, 0x45: _SP_INE,  # team sports general
        0x46: _SP_ATLETIKA, 0x47: _SP_MOTORSPORT, 0x48: _SP_VODNE,
        0x49: _SP_ZIMNE, 0x4A: _SP_INE, 0x4B: _SP_BOJOVE,
    },
    CAT_SPRAVODAJSTVO: {
        0x21: _NW_POCASIE,        # News/Weather
        0x22: _NW_MAGAZINY,       # News magazine
        0x23: _NW_HLAVNE,         # Documentary (news context)
        0x24: _NW_POLITIKA,       # Discussion/Interview
    },
    CAT_SHOW: {
        0x31: _SH_SUTAZ,          # Game show
        0x32: _SH_ZABAVA,         # Variety show
        0x33: _SH_TALK,           # Talk show
    },
    CAT_HUDBA: {
        0x61: _MU_HITY,           # Rock/Pop
        0x62: _MU_KLASIKA,        # Classical/Serious music
        0x63: _MU_FOLK,           # Folk/Traditional
        0x64: _MU_HITY,           # Jazz → Hity bucket
        0x65: _MU_KLASIKA,        # Musical/Opera
        0x66: _MU_KLASIKA,        # Ballet
    },
    CAT_UMENIE: {
        0x71: _AR_DIVADLO,        # Performing arts
        0x72: _AR_VYTVARNE,       # Fine arts
        0x73: _AR_INE,            # Religion
        0x74: _AR_INE,            # Popular culture
        0x75: _AR_LITERATURA,     # Literature
        0x76: _AR_FILM,           # Film/Cinema
        0x77: _AR_FILM,           # Experimental film
        0x78: _AR_INE,            # Broadcasting/Press
    },
    CAT_DOKUMENTY: {
        0x91: _DC_PRIRODA,        # Nature/Animals/Environment
        0x92: _DC_VEDA,           # Technology/Natural sciences
        0x93: _DC_VEDA,           # Medicine/Psychology
        0x94: _DC_CESTOPIS,       # Foreign countries/Expeditions
        0x95: _DC_SPOLOCNOST,     # Social/Spiritual sciences
        0x96: _DC_VEDA,           # Further education
        0x97: _DC_INE,            # Languages
    },
    CAT_HOBBY: {
        0xA1: _HB_CESTOVANIE,     # Tourism/Travel
        0xA2: _HB_DIY,            # Handicraft
        0xA3: _HB_AUTO,           # Motoring
        0xA4: _HB_ZDRAVIE,        # Fitness/Health
        0xA5: _HB_VARENIE,        # Cooking
        0xA6: _HB_BYVANIE,        # Shopping/Advertisements
        0xA7: _HB_ZAHRADA,        # Gardening
    },
})



# v1.0.4.1: Známé sci-fi/fantasy franchise tituly — keyword scan nezachytí,
# lebo distributori opisujú plot (vojna, pomsta, drama), nie žáner. Title-based
# override má prednosť pred keyword scan-om aj pred channel subgenre hint-om.
# Hľadáme na začiatku stripped-lowercased názvu, aby sme nezachytávali falošne
# (napr. "Duna" v "Dunaj, k vašim službám" — pattern \bduna\b s word boundary
# za "duna" by nezachytil "dunaj").
# Patterns sú anchored ku konkrétnym title-pozíciám (začiatok alebo s ":")
# pre dvojzmyselné mená (Batman, Hulk, Thor, atď.) ktoré inak môžu byť
# v opise dokumentov mythológie/histórie.
_TITLE_SCIFI_PATTERNS = (
    re.compile(r'^duna\b|\bduna\s*:|dune\b'),       # Duna / Dune
    re.compile(r'\bstar\s*wars\b'),                # Star Wars
    re.compile(r'\bhvezdne\s*valky\b'),            # Hvězdné války
    re.compile(r'\bstar\s*trek\b'),                # Star Trek
    re.compile(r'\bmatrix\b'),                     # Matrix
    re.compile(r'^avatar\b|\bavatar\s*:'),         # Avatar
    re.compile(r'\bterminator|\bterminat'),        # Terminátor
    re.compile(r'\bblade\s*runner\b'),             # Blade Runner
    re.compile(r'^pan\s+prsten|^pan\s+prstenu'),   # Pán prstenů / prsteňov
    re.compile(r'^(hobit|hobbit)\b|:\s*(hobit|hobbit)\b'),  # Hobit
    re.compile(r'\b(vetrelec|alien)\b'),           # Vetřelec / Alien
    re.compile(r'\b(predator|predátor)\b'),        # Predator
    re.compile(r'\btransformers\b'),               # Transformers
    re.compile(r'\bspider-?man\b'),                # Spider-Man
    re.compile(r'^iron\s*man\b|\biron\s*man\s*:'), # Iron Man (anchored)
    re.compile(r'\bavengers\b'),                   # Avengers
    re.compile(r'\bx-?men\b'),                     # X-Men
    re.compile(r'\bhunger\s*games\b'),             # Hunger Games
    re.compile(r'\bmaze\s*runner\b'),              # Maze Runner
    re.compile(r'\bjurassic\b|\bjursk'),           # Jurassic / Jurský
    re.compile(r'\binterstellar\b'),               # Interstellar
    re.compile(r'\binception\b'),                  # Inception
    re.compile(r'^tenet\b'),                       # Tenet (anchored - "tenet" je tiež slovo)
    re.compile(r'\bmen\s+in\s+black\b'),           # Men In Black
    re.compile(r'\bmad\s+max\b'),                  # Mad Max
    re.compile(r'\bedge\s+of\s+tomorrow\b'),       # Edge of Tomorrow
    re.compile(r'\boblivion\b'),                   # Oblivion
    re.compile(r'^gravity\b'),                     # Gravity (anchored)
    re.compile(r'^ender|\bender.?s\s+game\b'),     # Ender's Game
    re.compile(r'\bgodzilla\b|^king\s*kong\b|\bking\s*kong\s*:'),  # Godzilla / King Kong
    re.compile(r'^superman\b|\bsuperman\s*:'),     # Superman (anchored)
    re.compile(r'^batman\b|\bbatman\s*:|the\s+batman\b|\bdark\s+knight\b'),  # Batman (anchored)
    re.compile(r'\bdeadpool\b'),                   # Deadpool
    re.compile(r'\bdoctor\s*strange\b'),           # Doctor Strange
    re.compile(r'\bguardians\s+of\s+the\s+galax'), # Guardians of the Galaxy
    re.compile(r'\bjustice\s*league\b'),           # Justice League
    re.compile(r'\bwonder\s*woman\b'),             # Wonder Woman
    re.compile(r'\bharry\s*potter\b'),             # Harry Potter — fantasy override
)


# --------------------------------------------------------------------------
# v1.0.6: Title corpus
# --------------------------------------------------------------------------
# Statický corpus filmov a seriálov v každom žánri + lokalizované sk/cs
# preklady. Klasifikátor sa pýta corpus-u PRED keyword scan-om — title-based
# match je spoľahlivejší ako "drama" keyword v opise.
#
# Corpus súbor: resources/title_genre_corpus.json relatívne k addon root.
# Lazy načítanie pri prvom volaní _corpus_lookup. Bez I/O ak corpus chýba
# (graceful fallback, len log warning).
#
# Shipped corpus je hand-curated z populárnych slovenských, českých a
# medzinárodných titulov plus analýza dvoch reálnych TVH archívov. Refresh
# sa robí priamo úpravou JSON-u v resources/title_genre_corpus.json.
_CORPUS_CODE_TO_SUBCAT = {
    'ak': _MV_AKCNY,
    'ko': _MV_KOMEDIA,
    'kr': _MV_KRIMI,
    'dr': _MV_DRAMA,
    'sf': _MV_SCIFI,
    'ro': _MV_ROMANTIKA,
    'ho': _MV_HOROR,
    'do': _MV_DOBRODR,
    'an': _MV_ANIMAK,
    'hi': _MV_HISTORICKY,
    'we': _MV_WESTERN,
}

_CORPUS_STATE = {
    'loaded': False,
    'titles': {},    # normalized_title → subcat constant
    'load_error': None,
    'meta': None,
}


def _corpus_path():
    """Vráti absolútnu cestu k corpus JSON súboru."""
    # classifier.py je v plugin.video.tvheadend/lib/, corpus je v
    # plugin.video.tvheadend/resources/.
    here = os.path.dirname(os.path.abspath(__file__))
    addon_root = os.path.dirname(here)
    return os.path.join(addon_root, 'resources', 'title_genre_corpus.json')


def _load_corpus_if_needed():
    """Lazy načítanie corpus-u. Idempotentné — volá sa pred každým lookup-om."""
    if _CORPUS_STATE['loaded']:
        return
    _CORPUS_STATE['loaded'] = True  # set early — jeden pokus o load, no retry loop
    path = _corpus_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        _CORPUS_STATE['load_error'] = 'corpus file not found ({})'.format(path)
        return
    except (OSError, ValueError) as e:
        _CORPUS_STATE['load_error'] = 'corpus load failed: {}'.format(e)
        return

    raw_titles = (data.get('titles') if isinstance(data, dict) else None) or {}
    out = {}
    for k, code in raw_titles.items():
        sub = _CORPUS_CODE_TO_SUBCAT.get(code)
        if sub is None or not isinstance(k, str):
            continue
        # k je už pre-normalizované pri builde, ale strážny rebound:
        if k:
            out[k] = sub
    _CORPUS_STATE['titles'] = out
    _CORPUS_STATE['meta'] = data.get('_meta') if isinstance(data, dict) else None

    if _xbmc_log is not None and out:
        try:
            n = len(out)
            _xbmc_log('[classifier] title corpus loaded: {} entries'.format(n), 1)
        except Exception:
            pass


# Regex na odstránenie "(YYYY)" suffixu — corpus tituly tento suffix nemajú,
# DVR entry-tituly tiež zriedka, ale niektoré broadcaster-y áno (napr.
# "Halloween (1978)"). Pre lookup ho zhodíme rovnako ako pri normalizácii
# pri ručnej tvorbe corpusu.
_TITLE_YEAR_SUFFIX = re.compile(r'\s*\(\s*(?:19|20)\d{2}\s*\)\s*$')


def _canonical_title_for_corpus(title):
    """Normalizuje title pre corpus lookup. Musí ladiť s normalizáciou
    použitou pri tvorbe corpus JSON-u."""
    if not title:
        return ''
    # Strip tech markers (HD, AD, DD5.1, ...)
    t = _strip_tech_markers(title)
    # Strip episode suffix "(12)" alebo "(N)" (rok 1900-2099 sa nemení)
    t = series_canonical_title(t)
    # Strip rok suffix "(1999)"
    t = _TITLE_YEAR_SUFFIX.sub('', t).strip()
    return _strip_accents_lower(t)


def _corpus_subgenre_match(entry):
    """Vráti subcat constant ak title match-ne v corpuse, inak None."""
    _load_corpus_if_needed()
    titles = _CORPUS_STATE['titles']
    if not titles:
        return None
    title = entry.get('disp_title') or ''
    key = _canonical_title_for_corpus(title)
    if not key:
        return None
    return titles.get(key)


def _movie_subgenre(entry):
    """Sub-kategória pre film/seriál (DVB genre → title franchise override →
    keyword scan).

    Pozn.: title corpus match (v1.0.6) sa robí o úroveň vyššie v
    classify_dvr_entry, aby corpus prevážil channel subgenre hint. Tu je
    len DVB explicit genre + franchise scifi + keyword scan.
    """
    for g in (entry.get('genre') or []):
        try:
            g = int(g)
        except (ValueError, TypeError):
            continue
        sub = _DVB_GENRE_TO_SUBCAT.get(g)
        if sub:
            return sub
    title = entry.get('disp_title') or ''
    subtitle = entry.get('disp_subtitle') or ''
    description = entry.get('disp_description') or ''
    text = (title + ' ' + subtitle + ' ' + description)
    if not text.strip():
        return _MV_INE
    text = _strip_accents_lower(text)
    title_only = _strip_accents_lower(title)

    # v1.0.4.1: Title-based franchise override — sci-fi/fantasy "trademark"
    # tituly ktoré keyword scan nezachytí lebo distributor opisuje len plot.
    for pat in _TITLE_SCIFI_PATTERNS:
        if pat.search(title_only):
            return _MV_SCIFI

    # v1.0.4: Horor je špeciálne striktný — TVH `disp_subtitle` často obsahuje
    # dlhý plot description (broadcasters ho zneužívajú namiesto disp_description).
    # Keywordy ako "desiv*", "hruz*" sa preto objavia v plot opisoch vojnových
    # filmov a thrillerov ako "300" (subtitle: "desivej presile"). Horor preto
    # vyžaduje match v *disp_title only*, nie v subtitle ani description.
    # Reálne horror filmy majú typicky kľúčové slovo priamo v názve
    # (napr. "Horor v lese", "Halloween", "Saw").
    for pattern, subcat in _KEYWORD_TO_SUBCAT:
        if subcat == _MV_HOROR:
            if pattern.search(title_only):
                return subcat
            continue
        if pattern.search(text):
            return subcat
    return _MV_INE


def _title_franchise_scifi_match(entry):
    """v1.0.4.1: Vráti True ak title obsahuje známy sci-fi/fantasy franchise.
    Používa sa v classify_dvr_entry pre override aj channel subgenre hint-u."""
    title = entry.get('disp_title') or ''
    if not title:
        return False
    title_only = _strip_accents_lower(title)
    for pat in _TITLE_SCIFI_PATTERNS:
        if pat.search(title_only):
            return True
    return False


# --------------------------------------------------------------------------
# Channel-based hints
# --------------------------------------------------------------------------
_CHANNEL_TOP_HINTS = (
    ('ct :d',       CAT_DETSKE),
    ('ct d-art',    CAT_DETSKE),
    ('ct d/art',    CAT_DETSKE),
    ('decko',       CAT_DETSKE),
    ('jojko',       CAT_DETSKE),
    ('minimax',     CAT_DETSKE),
    ('cartoon',     CAT_DETSKE),
    ('disney',      CAT_DETSKE),
    ('nick',        CAT_DETSKE),
    ('boomerang',   CAT_DETSKE),
    ('baby tv',     CAT_DETSKE),
    ('duck tv',     CAT_DETSKE),
    ('sport',       CAT_SPORT),
    ('eurosport',   CAT_SPORT),
    ('digi sport',  CAT_SPORT),
    ('nova sport',  CAT_SPORT),
    ('o2 sport',    CAT_SPORT),
    ('cnn',         CAT_SPRAVODAJSTVO),
    ('bbc news',    CAT_SPRAVODAJSTVO),
    ('bbc world',   CAT_SPRAVODAJSTVO),
    ('ta3',         CAT_SPRAVODAJSTVO),
    ('ct24',        CAT_SPRAVODAJSTVO),
    ('ct 24',       CAT_SPRAVODAJSTVO),
    ('euronews',    CAT_SPRAVODAJSTVO),
    ('ocko',        CAT_HUDBA),
    ('now 80',      CAT_HUDBA),
    ('now 90',      CAT_HUDBA),
    ('now rock',    CAT_HUDBA),
    ('mtv',         CAT_HUDBA),
    ('vh1',         CAT_HUDBA),
    ('mezzo',       CAT_HUDBA),
    # Documentary channels (v1.0.4) — broadcasters často taggujú dokumenty
    # zle ako Movie/Drama (ct=1) alebo News (ct=2), takže channel hint to
    # opraví. Validované na 373 entries z 2 doc kanálov na server 2.
    ('discovery',          CAT_DOKUMENTY),
    ('national geographic', CAT_DOKUMENTY),
    ('nat geo',            CAT_DOKUMENTY),
    ('viasat history',     CAT_DOKUMENTY),
    ('viasat explore',     CAT_DOKUMENTY),
    ('viasat nature',      CAT_DOKUMENTY),
    ('viasat true crime',  CAT_DOKUMENTY),
    ('spektrum',           CAT_DOKUMENTY),
    ('animal planet',      CAT_DOKUMENTY),
    ('history channel',    CAT_DOKUMENTY),
    ('history hd',         CAT_DOKUMENTY),
    ('history 2',          CAT_DOKUMENTY),
    ('cs history',         CAT_DOKUMENTY),
    ('bbc earth',          CAT_DOKUMENTY),
    ('bbc knowledge',      CAT_DOKUMENTY),
    ('love nature',        CAT_DOKUMENTY),
    ('docubox',            CAT_DOKUMENTY),
    ('crime + investig',   CAT_DOKUMENTY),
    ('crime & investig',   CAT_DOKUMENTY),
    ('crime and investig', CAT_DOKUMENTY),
    ('investigation discovery', CAT_DOKUMENTY),
    ('óčko',        CAT_HUDBA),
)

_CHANNEL_SUBCAT_HINTS = (
    ('krimi',     _MV_KRIMI),
    ('action',    _MV_AKCNY),
    ('romantica', _MV_ROMANTIKA),
    ('romantika', _MV_ROMANTIKA),
    ('comedy',    _MV_KOMEDIA),
    ('cinema',    None),
    ('horror',    _MV_HOROR),
    ('history',   _MV_HISTORICKY),
)


def _channel_top_hint(entry):
    ch = (entry.get('channelname') or '').lower()
    if not ch:
        return None
    for substring, cat in _CHANNEL_TOP_HINTS:
        if substring in ch:
            return cat
    return None


def _channel_subgenre_hint(entry):
    ch = (entry.get('channelname') or '').lower()
    if not ch:
        return None
    for substring, subcat in _CHANNEL_SUBCAT_HINTS:
        if substring in ch:
            return subcat
    return None


# --------------------------------------------------------------------------
# Series detection
# --------------------------------------------------------------------------
_SERIES_KEYWORDS = ('seriál', 'série', ' díl ', 'epizoda', 'epizóda',
                    'season ', 'episode ')


def _is_series_entry(entry):
    subtitle = (entry.get('disp_subtitle') or '').strip()
    if _SUBTITLE_SERIES_PATTERN.match(subtitle):
        return True
    title = (entry.get('disp_title') or '').strip()
    if _has_episode_suffix(title):
        return True
    if entry.get('episode_disp'):
        return True
    desc = ((entry.get('disp_description') or '') + ' ' + subtitle).lower()
    for kw in _SERIES_KEYWORDS:
        if kw in desc:
            return True
    return False


# --------------------------------------------------------------------------
# Fallback keyword guess pre ct=0/11
# --------------------------------------------------------------------------
_FALLBACK_KEYWORD_TO_TOP = (
    (re.compile(r'\b(futbal|hokej|tenis|golf|formula|f1|oktagon|liga|'
                r'majstrov|olympi|rally|cyklist|atletik|box|wrestlin|'
                r'biatlon|lyzovan|sjazd)'),
     CAT_SPORT),
    (re.compile(r'\b(spravodajstvo|sprav[yi]|udalosti|aktualn|reporter|noviny\s+tv|'
                r'tv\s+noviny|pocasi|uvodnik)'),
     CAT_SPRAVODAJSTVO),
    # Detské: iba explicitné detské markery, nie generické "detsk*"
    # (slovo "detský/detská" sa objavuje v opisoch dospelácich shows ako
    # "malú detskú izbu" v design show, "detský domov" v krimi reportáži atď.)
    # v1.0.4: spresnené aby nepadali false positives z keyword "detsk" v opise.
    (re.compile(r'\b(rozpravk|pohadk|pre\s+deti|pro\s+deti|pre\s+najmens|'
                r'kreslen[ay]|animovan[ay]|loutkov[ay])'),
     CAT_DETSKE),
    (re.compile(r'\b(koncert|hudba|hudobn|hudebni|spevok|zpevak|spevak|'
                r'piesn|pisni|pop\s|rock\s|metal\s|klasick)'),
     CAT_HUDBA),
    (re.compile(r'\b(magazin|talk\s?show|\bshow\b|soutez|sutaz|'
                r'reality\s?show|farmer|farma|zabavn|estrada|kucharsk)'),
     CAT_SHOW),
    (re.compile(r'\b(dokument|documentary|prirod|history|'
                r'vesmir|national\s+geographic|discovery)'),
     CAT_DOKUMENTY),
)


def _guess_top_category_from_keywords(entry):
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or '') + ' ' +
            (entry.get('channelname') or ''))
    if not text.strip():
        return CAT_INE
    text = _strip_accents_lower(text)
    for pattern, cat in _FALLBACK_KEYWORD_TO_TOP:
        if pattern.search(text):
            return cat
    return CAT_INE


# --------------------------------------------------------------------------
# Hlavné klasifikačné funkcie
# --------------------------------------------------------------------------
def _determine_top_cat_with_reason(entry):
    """Vráti (top_cat, reason_str). reason vysvetľuje rozhodovaciu cestu."""
    try:
        ct = int(entry.get('content_type') or 0)
    except Exception:
        ct = 0

    channel_top = _channel_top_hint(entry)

    # v1.0.4: Documentary channels (Discovery, Viasat, National Geographic, atď.)
    # broadcasters často taggujú zle ako Movie/Drama (ct=1) alebo News (ct=2).
    # Doc channel hint preto override-uje aj tieto explicitné DVB-SI kódy.
    # Pre ct=3-10 doc hint NEvyhráva (Šport/Hudba/Šou explicitne tagované na
    # dokumentárnom kanále je realisticky špeciálny program, nie dokument).
    if channel_top == CAT_DOKUMENTY and ct in (0, 1, 2, 9):
        return CAT_DOKUMENTY, f'doc channel hint (ct={ct} potentially mislabelled)'

    if ct in (2, 3, 4, 6, 7, 8, 9, 10):
        return _CT_TO_CAT_BASE[ct], f'ct={ct} explicit DVB-SI Level 1'

    if channel_top in (CAT_DETSKE, CAT_SPORT, CAT_HUDBA, CAT_SPRAVODAJSTVO):
        return channel_top, f'channel hint (ct={ct})'

    if _is_series_entry(entry):
        return CAT_SERIAL, f'series pattern detected (ct={ct})'

    if ct == 1:
        return CAT_FILM, 'ct=1 Movie/Drama'
    if ct == 5:
        return CAT_DETSKE, 'ct=5 Children'

    return _guess_top_category_from_keywords(entry), 'keyword fallback (ct=0)'


def _determine_top_cat(entry):
    """Určuje top-level kategóriu pre DVR entry.

    Logika (od v1.0.4):
    - content_type je explicitný DVB-SI Level 1 signál z broadcaster-a — má prednosť
    - Pre špecifické content types (News=2, Show=3, Sport=4, Music=6, Arts=7,
      Social=8, Edu=9, Hobby=10) NEpoužívame channel hint — DVB tag je
      spoľahlivejší ako názov kanála.
    - Pre content_type 1 (Movie/Drama), 5 (Children) alebo 0 (undefined):
      channel hint a series detection majú zmysel.
    """
    return _determine_top_cat_with_reason(entry)[0]


def classify_dvr_entry(entry):
    """Vráti (top_cat, sub_cat). sub_cat môže byť None."""
    top, top_reason = _determine_top_cat_with_reason(entry)
    sub_reason = ''
    if top == CAT_FILM or top == CAT_SERIAL:
        # v1.0.4.1: Title franchise override (Duna, Pán prstenů, Star Wars, …)
        # vyhráva pred channel subgenre hint-om — channel ako "Nova Action HD"
        # by inak prerážal nemenovaný sci-fi obsah na mv_akcny.
        if _title_franchise_scifi_match(entry):
            sub = _MV_SCIFI
            sub_reason = 'title franchise override (sci-fi/fantasy)'
        else:
            # v1.0.6: title corpus — exact-title match. Vyhráva pred
            # channel hint-om aj DVB+keyword fallbackom — známy titul je
            # silnejší signál ako broadcaster-channel kategória, ktorá je
            # coarse-grained. Napr. "Drive" (krimi/thriller) na "Nova Action"
            # nech zostane v krimi, nie action.
            corpus_sub = _corpus_subgenre_match(entry)
            if corpus_sub:
                sub = corpus_sub
                sub_reason = 'title corpus match'
            else:
                sub = _channel_subgenre_hint(entry)
                if sub:
                    sub_reason = 'channel hint'
                else:
                    sub = _movie_subgenre(entry)
                    sub_reason = 'movie_subgenre (DVB/keyword)'
    else:
        cfg = SUBCAT_REGISTRY.get(top)
        if cfg and cfg[1] is not None:
            sub = _dvb_l2_subgenre(entry, top)
            if sub is not None:
                sub_reason = 'DVB L2 nibble'
            else:
                sub = cfg[1](entry)
                sub_reason = 'keyword subgenre'
        else:
            sub = None
            sub_reason = 'no subgenre'
    _dbg_log(entry, top, sub, f'top: {top_reason}; sub: {sub_reason}')
    return top, sub


def _dedup_dvr_entries(entries):
    """Najnovší z každej (title, subtitle) skupiny."""
    by_key = {}
    for e in entries:
        title = (e.get('disp_title') or '').strip()
        if not title:
            continue
        sub = (e.get('disp_subtitle') or '')[:80]
        key = (title, sub)
        prev = by_key.get(key)
        if prev is None or ts_of(e) > ts_of(prev):
            by_key[key] = e
    return list(by_key.values())


def ts_of(e):
    try:
        return int(e.get('start_real') or e.get('start') or 0)
    except Exception:
        return 0


def date_key_from_ts(ts):
    return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d')


# --------------------------------------------------------------------------
# Cache + orchestrator
# --------------------------------------------------------------------------
_DVR_CACHE = _SimpleTTLCache(1, default_timeout=60)
_CLASSIFY_CACHE = {'ts': 0, 'data': None}
_CLASSIFY_TTL_SEC = 60


def invalidate_caches():
    _DVR_CACHE.invalidate('dvr')
    _CLASSIFY_CACHE['ts'] = 0
    _CLASSIFY_CACHE['data'] = None


def get_dvr_finished_cached(tvh):
    """Vráti DVR nahrávky z cache (max 60 sekúnd staré)."""
    cached = _DVR_CACHE.get('dvr')
    if cached is not None:
        return cached
    result = tvh.get_dvr_finished()
    _DVR_CACHE.put('dvr', result)
    return result


def get_classified_dvr(tvh, dvr_limit=0, days_limit=0):
    """Vráti tuple (entries_by_top, entries_by_subcat, counts,
    series_by_canonical, series_subcat_titles).

    dvr_limit: ak > 0, oreže entries na N najnovších pred klasifikáciou
    days_limit: ak > 0, vyfiltruje entries staršie ako N dní

    Cache: 60s. Pri zmene limitov sa cache invaliduje implicitne tým že
    cachujeme `data` jednoznačne — pri zmene volajúceho sa entries
    pre-filtrujú znova.
    """
    now = int(time.time())
    cache_key = (dvr_limit, days_limit)
    cached = _CLASSIFY_CACHE
    if (cached['data'] and (now - cached['ts']) < _CLASSIFY_TTL_SEC
            and cached.get('key') == cache_key):
        return cached['data']

    entries = get_dvr_finished_cached(tvh)

    # Filter podľa days_limit
    if days_limit > 0:
        cutoff = time.time() - days_limit * 86400
        entries = [e for e in entries if ts_of(e) >= cutoff]

    # Dedup pred limitom (aby sme nestratili unikátnu epizódu)
    entries = _dedup_dvr_entries(entries)

    # Sort desc by ts pred limitom (newest first)
    entries.sort(key=ts_of, reverse=True)

    # Limit na N najnovších
    if dvr_limit > 0:
        entries = entries[:dvr_limit]

    entries_by_top = {}
    entries_by_subcat = {}
    series_by_canonical = {}
    series_subcat_titles = {}

    for e in entries:
        top, sub = classify_dvr_entry(e)
        entries_by_top.setdefault(top, []).append(e)
        if sub is not None:
            entries_by_subcat.setdefault((top, sub), []).append(e)
        if top == CAT_SERIAL:
            title = (e.get('disp_title') or '').strip()
            if title:
                canonical = series_canonical_title(title)
                if canonical:
                    series_by_canonical.setdefault(canonical, []).append(e)
                    if sub is not None:
                        series_subcat_titles.setdefault(
                            (CAT_SERIAL, sub), set()).add(canonical)

    for k in entries_by_top:
        entries_by_top[k].sort(key=ts_of, reverse=True)
    for k in entries_by_subcat:
        entries_by_subcat[k].sort(key=ts_of, reverse=True)
    for t in series_by_canonical:
        series_by_canonical[t].sort(key=ts_of, reverse=True)

    counts = {cat: len(entries_by_top[cat]) for cat in entries_by_top}
    data = (entries_by_top, entries_by_subcat, counts,
            series_by_canonical, series_subcat_titles)

    cached['ts'] = now
    cached['data'] = data
    cached['key'] = cache_key
    return data


# Exporty pre _add_dvr_entry_item v default.py
SUBTITLE_SERIES_PATTERN = _SUBTITLE_SERIES_PATTERN
TITLE_EPISODE_PATTERN = _TITLE_EPISODE_PATTERN

# Export pre search (vyhľadávanie bez diakritiky) v default.py
strip_accents_lower = _strip_accents_lower
