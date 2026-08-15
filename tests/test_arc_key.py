import pytest

from feeds import arc_key


@pytest.mark.parametrize("a, b", [
    # Hardcore History style: trailing roman numerals
    ("Show 66 - Supernova in the East V", "Show 67 - Supernova in the East VI"),
    # Behind the Bastards style: word-number prefix
    ("Part One: The Bastard Who Invented Ayn Rand",
     "Part Two: The Bastard Who Invented Ayn Rand"),
    # Rest Is History style: leading number + per-part subtitle + (Part N)
    ("437. The Fall of the Aztecs: Spaniards on the March (Part 1)",
     "440. The Fall of the Aztecs: The Great Escape (Part 4)"),
    # (N/M) style
    ("The Siege of Malta (1/3)", "The Siege of Malta (3/3)"),
    # plain Part N suffix
    ("History of Rome Part 3", "History of Rome Part 7"),
    # Pt. N style
    ("Dracula Pt. 2", "Dracula Pt. 3"),
])
def test_same_arc(a: str, b: str) -> None:
    assert arc_key(a) is not None
    assert arc_key(a) == arc_key(b)


@pytest.mark.parametrize("title", [
    "10.91- The End",              # leading number only — show numbering, not an arc
    "HoP 442 - Scholastic Metaphysics",  # embedded numbering, no marker
    "The Mothman Prophecies",      # plain title
    "437. A Plain Episode",        # leading number only
    "Part 2",                      # marker with empty remainder
    "",
])
def test_standalone(title: str) -> None:
    assert arc_key(title) is None


def test_distinct_arcs_do_not_collide() -> None:
    assert arc_key("The Fall of Rome (Part 1)") != arc_key("The Fall of Carthage (Part 1)")


def test_benign_roman_grouping_is_by_design() -> None:
    # "…World War II" keys with "…World War III" — a false group that merely
    # constrains those episodes to chronological order. Documented tradeoff.
    assert arc_key("The History of World War II") == arc_key("The History of World War III")


def test_lowercase_word_endings_are_not_romans() -> None:
    assert arc_key("Songs to remix") is None
    assert arc_key("What comes after xi") is None


# ── series_key: the strict grouping the "Queue series" action uses ────

from feeds import series_key


def test_series_key_matches_real_siblings() -> None:
    assert (series_key("The Bronze Age Collapse (Part One)")
            == series_key("The Bronze Age Collapse (Part Three)"))
    assert (series_key("The Palmer Raids, Part 1")
            == series_key("The Palmer Raids, Part 2"))
    assert (series_key("Part One: Rebecca Felton: The First Female Senator")
            == series_key("Part Two: Rebecca Felton: The First Female Senator"))


def test_series_key_separates_stories_behind_a_shared_banner() -> None:
    """Regression, found against the live library: arc_key truncates at the
    first separator, so every "SYMHC Classics: …" episode collapsed to the key
    "symhc classics". Queueing the series off one of them offered sixteen
    unrelated stories — Haunted Mansion, Gertrude Bell, the Moon Hoax. The
    rotation guard can absorb that; an action on the whole group cannot."""
    palmer_1 = "SYMHC Classics: Palmer Raids Pt. 1"
    palmer_2 = "SYMHC Classics: Palmer Raids Pt. 2"
    famine = "SYMHC Classics: Irish Famine, Part 1"

    assert series_key(palmer_1) == series_key(palmer_2)
    assert series_key(palmer_1) != series_key(famine)
    # the looser key is exactly what this guards against
    assert arc_key(palmer_1) == arc_key(famine)


def test_series_key_is_none_without_a_part_marker() -> None:
    assert series_key("A Standalone Episode") is None
    assert series_key("") is None
