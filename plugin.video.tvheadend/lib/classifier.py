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

import re
import time
import unicodedata
from datetime import datetime

from .tvh_client import _SimpleTTLCache


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
_TECH_MARKER_PATTERN = re.compile(
    r'\s*\(\s*(?:ST|HD|AD|SS|3D|UHD|DD|DTS)\s*\)\s*', re.IGNORECASE
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

_KEYWORD_TO_SUBCAT = (
    (re.compile(r'\b(detektiv|kriminal|krimi|thriller|vraz|policajn|vysetrov)'),
     _MV_KRIMI),
    (re.compile(r'\b(sci-?fi|sci\.\s?fi|fantasy|vedeckofant|vesmirn|mimozem|robot|kybern)'),
     _MV_SCIFI),
    (re.compile(r'\b(komedi|veselohra|humor|grotesk|sitcom)'),
     _MV_KOMEDIA),
    (re.compile(r'\b(horor|horror|desiv|hruz)'),
     _MV_HOROR),
    (re.compile(r'\b(romantick|milostn|romant)'),
     _MV_ROMANTIKA),
    (re.compile(r'\b(akcn|action|honic|prestrelk)'),
     _MV_AKCNY),
    (re.compile(r'\b(western|kovbo)'),
     _MV_WESTERN),
    (re.compile(r'\b(historick|valecn|vojensk|vojnov|histori)'),
     _MV_HISTORICKY),
    (re.compile(r'\b(dobrodruz|adventur|exped|cestopis)'),
     _MV_DOBRODR),
    (re.compile(r'\b(animovan|kreslen|animak|loutkov|cartoon|anime)'),
     _MV_ANIMAK),
    (re.compile(r'\b(drama|dramati)'),
     _MV_DRAMA),
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


def _movie_subgenre(entry):
    """Sub-kategória pre film/seriál (DVB genre, potom keyword scan)."""
    for g in (entry.get('genre') or []):
        try:
            g = int(g)
        except (ValueError, TypeError):
            continue
        sub = _DVB_GENRE_TO_SUBCAT.get(g)
        if sub:
            return sub
    text = ((entry.get('disp_title') or '') + ' ' +
            (entry.get('disp_subtitle') or '') + ' ' +
            (entry.get('disp_description') or ''))
    if not text.strip():
        return _MV_INE
    text = _strip_accents_lower(text)
    for pattern, subcat in _KEYWORD_TO_SUBCAT:
        if pattern.search(text):
            return subcat
    return _MV_INE


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
    (re.compile(r'\b(rozpravk|pohadk|detsk|pre\s+deti|pro\s+deti|kreslen|'
                r'animovan|loutkov)'),
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
def _determine_top_cat(entry):
    channel_top = _channel_top_hint(entry)
    if channel_top in (CAT_DETSKE, CAT_SPORT, CAT_HUDBA, CAT_SPRAVODAJSTVO):
        return channel_top
    if _is_series_entry(entry):
        return CAT_SERIAL
    try:
        ct = int(entry.get('content_type') or 0)
    except Exception:
        ct = 0
    if ct == 1:
        return CAT_FILM
    if ct in _CT_TO_CAT_BASE:
        return _CT_TO_CAT_BASE[ct]
    return _guess_top_category_from_keywords(entry)


def classify_dvr_entry(entry):
    """Vráti (top_cat, sub_cat). sub_cat môže byť None."""
    top = _determine_top_cat(entry)
    if top == CAT_FILM or top == CAT_SERIAL:
        sub = _channel_subgenre_hint(entry) or _movie_subgenre(entry)
        return top, sub
    cfg = SUBCAT_REGISTRY.get(top)
    if cfg and cfg[1] is not None:
        return top, cfg[1](entry)
    return top, None


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
