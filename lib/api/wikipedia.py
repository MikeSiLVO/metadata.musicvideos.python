# SPDX-License-Identifier: GPL-3.0-or-later

"""Wikipedia API client."""

import json
import re
import time
from collections import OrderedDict
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from lib import log
from lib.config import CACHE_LIMIT

_MIN_EXTRACT_LEN = 200

_RE_SMART_QUOTES = re.compile(r'[\u201c\u201d\u2018\u2019\u00ab\u00bb]')
_RE_HTML = re.compile(r'<[^>]+>')

# Native keyword beats "song" on non-en wikis (skews toward album articles otherwise).
_SEARCH_KEYWORD = {
    'en': 'song', 'es': 'canci\u00f3n', 'fr': 'chanson', 'de': 'Lied',
    'it': 'canzone', 'pt': 'can\u00e7\u00e3o', 'nl': 'nummer', 'pl': 'utw\u00f3r',
    'sv': 'l\u00e5t', 'ru': '\u043f\u0435\u0441\u043d\u044f', 'ja': '', 'ko': '', 'zh': '',
}

# Per-language lists union with English so en-only filtering never regresses.
_NON_SONG_HINTS_PER_LANG = {
    'en': r'\b(film|movie|album|television|tv series|novel|video game|'
          r'disambiguation)\b|topics referred to by the same term',
    'es': r'\b(pel\u00edcula|\u00e1lbum|serie|novela|videojuego|desambiguaci\u00f3n)\b',
    'fr': r'\b(film|album|s\u00e9rie|roman|jeu vid\u00e9o|homonymie)\b',
    'de': r'\b(Film|Album|Fernsehserie|Roman|Videospiel|Begriffskl\u00e4rung)\b',
    'it': r'\b(film|album|serie|romanzo|videogioco|disambigua)\b',
    'pt': r'\b(filme|\u00e1lbum|s\u00e9rie|romance|jogo eletr\u00f4nico|desambigua\u00e7\u00e3o)\b',
    'ja': r'(\u6620\u753b|\u30a2\u30eb\u30d0\u30e0|\u30c6\u30ec\u30d3\u30c9\u30e9\u30de|\u5c0f\u8aac|\u30b2\u30fc\u30e0|\u66d6\u6627\u3055\u56de\u907f)',
    'ko': r'(\uc601\ud654|\uc74c\ubc18|\ud154\ub808\ube44\uc804|\uc18c\uc124|\uac8c\uc784|\ub3d9\uc74c\uc774\uc758)',
}

_SONG_HINTS_PER_LANG = {
    'en': r'\b(song|single|track|ep)\b',
    'es': r'\b(canci\u00f3n|sencillo|tema)\b',
    'fr': r'\b(chanson|single|titre)\b',
    'de': r'\b(Lied|Single|Song)\b',
    'it': r'\b(canzone|singolo|brano)\b',
    'pt': r'\b(can\u00e7\u00e3o|single|tema)\b',
    'ja': r'(\u66f2|\u30b7\u30f3\u30b0\u30eb)',
    'ko': r'(\ub178\ub798|\uc2f1\uae00|\uace1)',
}

_NON_SONG_CACHE = {}
_SONG_CACHE = {}


def _non_song_re(lang):
    rx = _NON_SONG_CACHE.get(lang)
    if rx is None:
        pat = _NON_SONG_HINTS_PER_LANG.get(lang)
        en = _NON_SONG_HINTS_PER_LANG['en']
        combined = '({})|({})'.format(pat, en) if pat else en
        rx = re.compile(combined, re.IGNORECASE)
        _NON_SONG_CACHE[lang] = rx
    return rx


def _song_re(lang):
    rx = _SONG_CACHE.get(lang)
    if rx is None:
        pat = _SONG_HINTS_PER_LANG.get(lang)
        en = _SONG_HINTS_PER_LANG['en']
        combined = '({})|({})'.format(pat, en) if pat else en
        rx = re.compile(combined, re.IGNORECASE)
        _SONG_CACHE[lang] = rx
    return rx

_cache = OrderedDict()

_MISSING = object()


def _lru_set(key, value):
    while len(_cache) >= CACHE_LIMIT:
        _cache.popitem(last=False)
    _cache[key] = value


def get_track_summary(artist, track, lang='en'):
    """Search for a track's Wikipedia article and return the intro."""
    key = (artist.lower(), track.lower())
    cached = _cache.get(key, _MISSING)
    if cached is not _MISSING:
        return cached or None

    base = 'https://{}.wikipedia.org'.format(lang)

    # Loose fallback rescues CJK tokenization quirks where exact-phrase misses.
    keyword = _SEARCH_KEYWORD.get(lang, '')
    kw_suffix = ' ' + keyword if keyword else ''
    queries = [
        '"{}" "{}"{}'.format(track, artist, kw_suffix),
        '{} {}{}'.format(track, artist, kw_suffix),
    ]

    for query in queries:
        pages = _search(base, query)
        if not pages:
            continue
        for page in pages:
            if _validate_result(page, track, artist, lang):
                title = page.get('title', '')
                extract = _get_extract(base, title)
                if extract and len(extract) >= _MIN_EXTRACT_LEN:
                    _lru_set(key, extract)
                    return extract

    _lru_set(key, '')
    return None


def _http_json(url, label):
    """GET JSON with one retry on transient network errors."""
    req = Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'metadata.musicvideos.python',
    })
    for attempt in range(2):
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except HTTPError as exc:
            if exc.code == 404:
                log.debug('Wikipedia {}: not found'.format(label))
                return None
            log.error('Wikipedia {} failed: {}'.format(label, exc))
            return None
        except Exception as exc:
            if attempt == 0:
                time.sleep(0.5)
                continue
            log.error('Wikipedia {} failed: {}'.format(label, exc))
            return None


def _search(base_url, query, limit=10):
    """Search Wikipedia pages by query string."""
    url = '{}/w/rest.php/v1/search/page?{}'.format(
        base_url, urlencode({'q': query, 'limit': limit}),
    )
    log.debug('Wikipedia search: {}'.format(query[:60]))
    data = _http_json(url, 'search')
    if not isinstance(data, dict):
        return None
    pages = data.get('pages')
    return pages if isinstance(pages, list) else None


def _get_extract(base_url, title):
    """Fetch the plain-text intro extract for a page title."""
    params = {
        'action': 'query',
        'titles': title,
        'prop': 'extracts',
        'exintro': 1,
        'explaintext': 1,
        'format': 'json',
    }
    url = '{}/w/api.php?{}'.format(base_url, urlencode(params))
    data = _http_json(url, 'extract')
    if not isinstance(data, dict):
        return None

    if isinstance(data.get('error'), dict):
        return None

    query_data = data.get('query')
    if not isinstance(query_data, dict):
        return None
    pages = query_data.get('pages')
    if not isinstance(pages, dict):
        return None

    for page in pages.values():
        extract = page.get('extract')
        if extract:
            return extract.strip()
    return None


# Bounded prefix prevents "YOU" matching "You were.../BALLAD".
_RE_TITLE_BOUNDARY = re.compile(r'^(?:$|\s*\(|\s+song\b|\s*[-–—:]|[.…])')


def _validate_result(page, track_name, artist, lang='en'):
    """Check that a search result is about the right song."""
    title = _RE_SMART_QUOTES.sub('', page.get('title', '')).lower()
    track_lower = track_name.lower()

    if not title.startswith(track_lower):
        return False
    remainder = title[len(track_lower):]
    if remainder and not _RE_TITLE_BOUNDARY.match(remainder):
        return False

    description = page.get('description', '')
    if description and _non_song_re(lang).search(description):
        if not _song_re(lang).search(description):
            return False

    artist_lower = artist.lower()
    if artist_lower in title:
        return True
    if artist_lower in description.lower():
        return True

    excerpt = page.get('excerpt', '')
    if excerpt and artist_lower in _RE_HTML.sub('', excerpt).lower():
        return True

    return False
