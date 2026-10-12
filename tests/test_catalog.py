"""Catalog tests: guessing the artist from YouTube titles, and listing what's downloaded."""

import pytest

from app import catalog, db


def songs(*rows):
    return [{"video_id": f"v{i}", "title": t, "channel": c} for i, (t, c) in enumerate(rows)]


def artists(*rows):
    return list(catalog.guess_artists(songs(*rows)).values())


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Coldplay - The Scientist (Official 4K Video)", "Coldplay"),
        ("Queen – Bohemian Rhapsody (Official Video Remastered)", "Queen"),
        ("La Oreja de Van Gogh — Jueves [Letra]", "La Oreja de Van Gogh"),
        ("Don Omar | Dile (Video Oficial )", "Don Omar"),
        ("BAD BUNNY -  CALLAÍTA (Official Video)", "BAD BUNNY"),
        ("KARAOKE Los Vaguens - Lo Que Soy", "Los Vaguens"),
        ("Karaoke Belanova - Rosa Pastel", "Belanova"),
        ("Wisin & Yandel - Pegao (Official Video)", "Wisin & Yandel"),
        ("Xavi, Manuel Turizo - En Privado (Official Video)", "Xavi"),
        ("Simpson Ahuevo ft. Lil Pesci - Semana Diabla", "Simpson Ahuevo"),
        ("Grupo Frontera x Grupo Firme - EL AMOR DE SU VIDA", "Grupo Frontera"),
        ("10 - Bandoleros - Tokyo Drift Soundtrack", "Bandoleros"),
        ("A song with no separators at all", catalog.UNKNOWN_ARTIST),
    ],
)
def test_artist_is_the_left_side(title, expected):
    assert artists((title, "Some Uploader")) == [expected]


def test_artist_on_the_right_is_found_through_other_songs():
    assert artists(
        ("KARAOKE Los Vaguens - Lo Que Soy", "Cescpc"),
        ("Penurias - Los Vaguens (versión karaoke)", "The Rose Queen"),
        ("División Minúscula - Las Luces De Esta Ciudad", "División Minúscula"),
        ("Las luces de esta ciudad (Karaoke) - División Minúscula", "Musicallsing"),
        ("Don Omar - Pobre Diabla", "Sandrewification"),
        ("Best of Fast And Furious (Music Video) | Don Omar - Los bandoleros", "Oscurits"),
    ) == ["Los Vaguens", "Los Vaguens", "División Minúscula", "División Minúscula", "Don Omar", "Don Omar"]


def test_official_channel_named_in_the_title_wins():
    assert artists(
        ("AYA - Murda & Ezhel (prod. Spanker)", "Ezhel"),
        ("Hicazkar Oyun Havası & Yalan Gözlerin - MURAT BAYRAKDAR", "Murat Bayrakdar"),
        ("Xavi - La Diabla (Official Video)", "Xavi Oficial"),
        ("Queen – Bohemian Rhapsody", "Queen Official"),
    ) == ["Ezhel", "Murat Bayrakdar", "Xavi", "Queen"]


def test_channel_only_mentioned_in_a_note_is_not_the_artist():
    assert artists(("Angine de Poitrine - Sarniezz (Live on KEXP)", "KEXP")) == ["Angine de Poitrine"]


def test_title_without_separators_matches_a_known_artist_ignoring_accents():
    assert artists(
        ("Miguel Bose - Morena Mia (Versión Karaoke)", "PARTY TYME"),
        ("Miguel Bosé Si Tu No Vuelves Karaoke 4K", "Karaoking 4K"),
        ("BAD BUNNY -  CALLAÍTA", "Bad Bunny"),
        ("KLOuFRENS Bad Bunny Tony Desk", "Erick Hernandez"),
    ) == ["Miguel Bose", "Miguel Bose", "Bad Bunny", "Bad Bunny"]


def test_one_spelling_per_artist():
    got = artists(
        ("BAD BUNNY - CALLAÍTA", "x"), ("Bad Bunny - Safaera", "x"), ("bad bunny - Dakiti", "x"),
        ("Bad Bunny - Yonaguni", "x"),
    )
    assert set(got) == {"Bad Bunny"}


def test_key_ignores_accents_case_and_spaces():
    assert catalog.key("  José   JOSÉ ") == "jose jose"
    assert catalog.key("İbrahim Tatlıses") == catalog.key("i̇brahim tatlıses")


# --- what's downloaded --------------------------------------------------------

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def add(conn, tmp_path, vid, title, channel="Uploader", downloaded=True):
    db.upsert_song(conn, vid, title, channel, 200, None)
    if downloaded:
        f = tmp_path / f"{vid}.mp4"
        f.write_bytes(b"x")
        db.set_song_file(conn, vid, str(f))


def test_downloaded_lists_only_files_on_disk_sorted_by_title(conn, tmp_path):
    add(conn, tmp_path, "b", "Queen - Bohemian Rhapsody")
    add(conn, tmp_path, "a", "Ángel - Belinda")
    add(conn, tmp_path, "c", "Coldplay - Yellow", downloaded=False)
    add(conn, tmp_path, "d", "Zoé - Labios Rotos")
    (tmp_path / "d.mp4").unlink()  # the row says it's there, the disk doesn't
    got = catalog.downloaded(conn)
    assert [s["video_id"] for s in got] == ["a", "b"]
    assert got[1] == {
        "video_id": "b", "title": "Queen - Bohemian Rhapsody", "artist": "Queen",
        "duration_s": 200, "play_count": 0,
    }


def test_songs_not_downloaded_still_help_the_guess(conn, tmp_path):
    add(conn, tmp_path, "a", "Penurias - Los Vaguens (versión karaoke)")
    add(conn, tmp_path, "b", "Los Vaguens - Lo Que Soy", downloaded=False)
    assert [s["artist"] for s in catalog.downloaded(conn)] == ["Los Vaguens"]


def test_artist_set_by_hand_wins_and_empty_goes_back_to_the_guess(conn, tmp_path):
    add(conn, tmp_path, "a", "Yo Ya No Vuelvo Contigo - (Video Oficial) - Lenin Ramirez ft. Grupo Firme")
    assert catalog.downloaded(conn)[0]["artist"] == "Yo Ya No Vuelvo Contigo"
    assert catalog.set_artist(conn, "a", "  Lenin   Ramirez ")
    assert catalog.downloaded(conn)[0]["artist"] == "Lenin Ramirez"
    assert catalog.set_artist(conn, "a", "")
    assert catalog.downloaded(conn)[0]["artist"] == "Yo Ya No Vuelvo Contigo"
    assert not catalog.set_artist(conn, "nope", "x")


def test_upsert_keeps_the_artist_set_by_hand(conn, tmp_path):
    add(conn, tmp_path, "a", "Some title")
    catalog.set_artist(conn, "a", "Someone")
    db.upsert_song(conn, "a", "Some title (new)", "Uploader", 200, None)
    assert catalog.downloaded(conn)[0]["artist"] == "Someone"
