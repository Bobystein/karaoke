"""The catalog: the downloaded songs, to browse and request without internet.

YouTube gives a title and a channel, not an artist, and the channel is often
whoever uploaded the karaoke track. So the artist is guessed from the title,
using the rest of the library as a hint: "Penurias - Los Vaguens" has the
artist on the right, and we know because "Los Vaguens" is on the left of
another song. The guess is made on every read, so it gets better as the
library grows. An admin can set the artist by hand (`songs.artist`), and that
always wins.
"""

import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

UNKNOWN_ARTIST = "Other"
MAX_ARTIST_LEN = 80

# "Artist - Song", "Artist – Song", "Artist | Song", "Artist || Song".
_PARTS = re.compile(r"\s+(?:[-–—]+|\|+)\s*|\s*(?:[-–—]+|\|+)\s+")
# Where the first artist ends in "A, B", "A x B", "A ft. B", "A feat. B".
_MORE_ARTISTS = re.compile(r"\s*,\s*|\s+x\s+|\s+(?:ft|feat|featuring)\b\.?\s*", re.IGNORECASE)
_ALSO_AND = re.compile(r"\s+(?:&|y|and)\s+", re.IGNORECASE)
# Notes that aren't part of any name: (Official Video), [Karaoke], #hashtags.
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|#\w+")
_KARAOKE_PREFIX = re.compile(r"^\s*karaoke\b[\s:.\-–—]*", re.IGNORECASE)
# What official channels add to the artist's name.
_CHANNEL_SUFFIX = re.compile(r"\s*(?:\bofficial\b|\boficial\b|\bvevo\b|-\s*topic\b)\s*$", re.IGNORECASE)


def key(text: str) -> str:
    """How names are compared and sorted: no accents, no case, single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def _parts(title: str) -> list[str]:
    """The title cut at its separators, without bracketed notes or a leading "Karaoke"."""
    title = _KARAOKE_PREFIX.sub("", _BRACKETS.sub(" ", title or ""))
    parts = (part.strip(" -–—|") for part in _PARTS.split(title))
    return [p for p in parts if p and not p.isdigit()]  # "10 - Song" is a track number


def _first_artist(part: str) -> str:
    return _MORE_ARTISTS.split(part, maxsplit=1)[0].strip()


def _artists_in(part: str) -> list[str]:
    """Everyone named in a part: "Murda & Ezhel" is Murda and Ezhel."""
    names = [n.strip() for chunk in _MORE_ARTISTS.split(part) for n in _ALSO_AND.split(chunk)]
    return [n for n in names if n]


def _channel_name(channel: str | None) -> str:
    return _CHANNEL_SUFFIX.sub("", channel or "").strip()


def guess_artists(songs: list[dict]) -> dict[str, str]:
    """{video_id: artist} for songs with `video_id`, `title` and `channel`.

    In order: the channel, when it is someone named in the title (an official
    channel); the left side, when other songs agree it's an artist; another
    side that is an artist in other songs; an artist from other songs named
    in a title with no separators; the left side; UNKNOWN_ARTIST.
    """
    parts = {s["video_id"]: _parts(s["title"]) for s in songs}
    left = {vid: _first_artist(p[0]) for vid, p in parts.items() if len(p) >= 2}
    seen = Counter(key(name) for name in left.values())
    spelling: dict[str, Counter] = {}
    for name in left.values():
        spelling.setdefault(key(name), Counter())[name] += 1

    def others(vid: str, name: str) -> int:
        """How many other songs have this name on their left side."""
        return seen[key(name)] - (1 if key(left.get(vid, "")) == key(name) else 0)

    guessed: dict[str, str] = {}
    for s in songs:
        vid, p = s["video_id"], parts[s["video_id"]]
        channel = _channel_name(s.get("channel"))
        named = {key(n) for part in p for n in [_first_artist(part), *_artists_in(part)]}
        if channel and key(channel) in named:
            guessed[vid] = channel
        elif len(p) >= 2:
            known = [_first_artist(part) for part in p if others(vid, _first_artist(part)) > 0]
            guessed[vid] = known[0] if known else left[vid]
        else:
            title = f" {key(s['title'])} "
            inside = [k for k in seen if k and f" {k} " in title]
            # Found by its key, so it brings no spelling of its own: `best()`
            # gives it the one the other songs use.
            guessed[vid] = max(inside, key=len) if inside else UNKNOWN_ARTIST
            continue
        spelling.setdefault(key(guessed[vid]), Counter())[guessed[vid]] += 1

    # One spelling per artist: the most used, and not the SHOUTED one on a tie.
    def best(name: str) -> str:
        variants = spelling.get(key(name))
        if not variants:
            return name
        return max(variants, key=lambda v: (variants[v], not v.isupper(), v))

    return {vid: best(name) for vid, name in guessed.items()}


def clean_artist(text: str) -> str:
    return " ".join((text or "").split())[:MAX_ARTIST_LEN]


def downloaded(conn: sqlite3.Connection) -> list[dict]:
    """The songs whose file is on disk, with their artist, sorted by title.

    The guess looks at every song we know, downloaded or not: the more
    titles, the better it tells which side the artist is on.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT video_id, title, channel, duration_s, file_path, artist, play_count FROM songs"
    )]
    guessed = guess_artists(rows)
    songs = [
        {
            "video_id": r["video_id"],
            "title": r["title"],
            "artist": r["artist"] or guessed[r["video_id"]],
            "duration_s": r["duration_s"],
            "play_count": r["play_count"],
        }
        for r in rows
        if r["file_path"] and Path(r["file_path"]).is_file()
    ]
    return sorted(songs, key=lambda s: (key(s["title"]), s["video_id"]))


def set_artist(conn: sqlite3.Connection, video_id: str, artist: str) -> bool:
    """Sets the artist by hand; empty goes back to the guess. False if the song doesn't exist."""
    cur = conn.execute(
        "UPDATE songs SET artist = ? WHERE video_id = ?", (clean_artist(artist) or None, video_id)
    )
    return cur.rowcount > 0
