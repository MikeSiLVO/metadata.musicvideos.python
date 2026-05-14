# SPDX-License-Identifier: GPL-3.0-or-later

"""Artwork collection and output."""

from lib.api.audiodb import get_track_screenshots

# MySQL TEXT = 65,535 bytes; margin for safety
_C01_BUDGET = 60000

_THUMB_ORDER_DEFAULT = ('screenshots', 'album_cover', 'artist_thumb')
_THUMB_TOGGLE = {
    'screenshots': 'screenshots',
    'album_cover': 'album_thumb',
    'artist_thumb': 'thumb',
}


def set_artwork(vtag, track_data, album_data, artist_artwork, fanarttv_artwork,
                settings=None):
    """Collect artwork from all sources and add to the listitem."""
    fanart = []
    enabled = _enabled_map(settings)

    # Bucket thumb candidates by source so user-preferred ordering wins.
    # Primary source bypasses its toggle so a stale setting can't silently
    # hide what the user explicitly chose.
    primary = (settings or {}).get('thumb_primary', 'screenshots')
    by_source = {k: [] for k in _THUMB_ORDER_DEFAULT}
    c01_other = []

    if primary == 'screenshots' or enabled[_THUMB_TOGGLE['screenshots']]:
        for url, preview in get_track_screenshots(track_data):
            by_source['screenshots'].append((url, preview))

    if primary == 'album_cover' or enabled[_THUMB_TOGGLE['album_cover']]:
        u = _album_thumb(album_data)
        if u:
            by_source['album_cover'].append((u, '{}/preview'.format(u)))

    allow_artist_thumb = enabled['thumb'] or primary == 'artist_thumb'

    # Fanart.tv first — higher quality, community-curated
    if fanarttv_artwork:
        for art_type, items in fanarttv_artwork.items():
            if art_type == 'thumb' and not allow_artist_thumb:
                continue
            if art_type != 'thumb' and not enabled.get(art_type, True):
                continue
            for url, preview, _ in items:
                if art_type == 'fanart':
                    fanart.append(url)
                elif art_type == 'thumb':
                    by_source['artist_thumb'].append((url, preview))
                else:
                    c01_other.append((art_type, url, preview))

    if artist_artwork:
        for art_type, items in artist_artwork.items():
            if art_type == 'thumb' and not allow_artist_thumb:
                continue
            if art_type != 'thumb' and not enabled.get(art_type, True):
                continue
            for url, preview in items:
                if art_type == 'fanart':
                    fanart.append(url)
                elif art_type == 'thumb':
                    by_source['artist_thumb'].append((url, preview))
                else:
                    c01_other.append((art_type, url, preview))

    c01 = []
    for src in _ordered_thumb_sources(settings):
        for url, preview in by_source[src]:
            c01.append(('thumb', url, preview))
    c01.extend(c01_other)

    # Fanart has no column storage for musicvideos, no limit needed
    if fanart:
        # setAvailableFanart is Piers (v22) only; fall back on Omega/Nexus
        if hasattr(vtag, 'setAvailableFanart'):
            vtag.setAvailableFanart([{'image': url} for url in fanart])
        else:
            for url in fanart:
                vtag.addAvailableArtwork(url, arttype='fanart')

    used = 0
    for art_type, url, preview in c01:
        cost = _byte_cost(art_type, url, preview)
        if used + cost > _C01_BUDGET:
            break
        vtag.addAvailableArtwork(url, arttype=art_type, preview=preview)
        used += cost


def _byte_cost(art_type, url, preview):
    """Estimate XML byte cost of one artwork entry in the database."""
    # Kodi serializes as: <thumb spoof="" cache="" aspect="TYPE" preview="PREVIEW">URL</thumb>
    return 52 + len(art_type) + len(preview) + len(url)


_ART_KEYS = (
    'screenshots', 'album_thumb', 'thumb', 'fanart',
    'clearlogo', 'banner', 'clearart', 'landscape',
)


def _enabled_map(settings):
    """Resolve per-art-type toggles; default to enabled when unset."""
    out = {k: True for k in _ART_KEYS}
    if not settings:
        return out
    for key in _ART_KEYS:
        val = settings.get('art_' + key)
        if val is not None:
            out[key] = bool(val)
    return out


def _ordered_thumb_sources(settings):
    """Thumb source keys in user-preferred order, primary first."""
    primary = (settings or {}).get('thumb_primary', 'screenshots')
    return (primary,) + tuple(
        k for k in _THUMB_ORDER_DEFAULT if k != primary)


def _album_thumb(album_data):
    """Pick the best album cover URL from an AudioDB album record."""
    if not album_data:
        return ''
    return (album_data.get('strAlbumThumb')
            or album_data.get('strAlbumThumbHQ')
            or '')
