import pytest

from feeds import series_key


@pytest.mark.parametrize("a, b", [
    # Hardcore History style: leading show number + trailing roman numerals
    ("Show 66 - Supernova in the East V", "Show 67 - Supernova in the East VI"),
    # Behind the Bastards style: word-number prefix
    ("Part One: The Bastard Who Invented Ayn Rand",
     "Part Two: The Bastard Who Invented Ayn Rand"),
    # (N/M) style
    ("The Siege of Malta (1/3)", "The Siege of Malta (3/3)"),
    # plain Part N suffix
    ("History of Rome Part 3", "History of Rome Part 7"),
    # Pt. N style
    ("Dracula Pt. 2", "Dracula Pt. 3"),
])
def test_part_markers_group_siblings(a: str, b: str) -> None:
    assert series_key(a) is not None
    assert series_key(a) == series_key(b)


@pytest.mark.parametrize("title", [
    "10.91- The End",              # leading number only — show numbering, not a part
    "HoP 442 - Scholastic Metaphysics",  # embedded numbering, no marker
    "The Mothman Prophecies",      # plain title
    "437. A Plain Episode",        # leading number only
    "Part 2",                      # marker with empty remainder
    "Songs to remix",              # lowercase word endings are not romans
    "What comes after xi",
])
def test_standalone_titles_have_no_key(title: str) -> None:
    assert series_key(title) is None


def test_series_key_matches_real_siblings() -> None:
    assert (series_key("The Bronze Age Collapse (Part One)")
            == series_key("The Bronze Age Collapse (Part Three)"))
    assert (series_key("The Palmer Raids, Part 1")
            == series_key("The Palmer Raids, Part 2"))
    assert (series_key("Part One: Rebecca Felton: The First Female Senator")
            == series_key("Part Two: Rebecca Felton: The First Female Senator"))


def test_series_key_separates_stories_behind_a_shared_banner() -> None:
    """Regression, found against the live library: a key that truncates at the
    first separator collapsed every "SYMHC Classics: …" episode to "symhc
    classics", so queueing the series off one of them offered sixteen
    unrelated stories — Haunted Mansion, Gertrude Bell, the Moon Hoax."""
    palmer_1 = "SYMHC Classics: Palmer Raids Pt. 1"
    palmer_2 = "SYMHC Classics: Palmer Raids Pt. 2"
    famine = "SYMHC Classics: Irish Famine, Part 1"

    assert series_key(palmer_1) == series_key(palmer_2)
    assert series_key(palmer_1) != series_key(famine)


def test_series_key_is_none_without_a_part_marker() -> None:
    assert series_key("A Standalone Episode") is None
    assert series_key("") is None
