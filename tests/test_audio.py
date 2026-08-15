from audio import delete_local, guess_mime, media_url, normalize_uri, uris_match


def test_guess_mime() -> None:
    assert guess_mime("https://x/a.mp3") == "audio/mpeg"
    assert guess_mime("https://x/a.m4a?tok=1") == "audio/mp4"
    assert guess_mime("https://x/a.ogg") == "audio/ogg"
    assert guess_mime("https://x/whatknows") == "audio/mpeg"


def test_normalize_strips_query_and_lowercases_host() -> None:
    assert normalize_uri("HTTPS://CDN.X.com/a/B.mp3?token=zzz#f") == "https://cdn.x.com/a/B.mp3"


def test_uris_match_exact_and_normalized() -> None:
    assert uris_match("https://x/a.mp3", "https://x/a.mp3")
    assert uris_match("https://x/a.mp3?tok=1", "https://x/a.mp3?tok=2")


def test_uris_match_path_only_fallback() -> None:
    # Sonos rewrites the scheme/host for some streams; the path survives.
    assert uris_match("x-rincon-mp3radio://cdn.x.com/shows/ep1.mp3",
                      "https://other-edge.x.com/shows/ep1.mp3")
    assert not uris_match("https://x/a.mp3", "https://x/b.mp3")
    assert not uris_match("https://x/", "https://y/")


def test_media_url() -> None:
    assert media_url("http://10.0.0.5:8080/", "ep7.mp3") == "http://10.0.0.5:8080/media/ep7.mp3"


def test_delete_local_missing_file_is_silent(tmp_path) -> None:
    delete_local(str(tmp_path / "nope.mp3"))  # must not raise


def test_stream_url_is_short_stable_and_identifying() -> None:
    from audio import SONOS_URI_LIMIT, stream_url

    url = stream_url("http://10.0.0.5:8080/", 15546)
    assert url == "http://10.0.0.5:8080/stream/15546.mp3"
    assert len(url) < SONOS_URI_LIMIT
    # Same episode, same string every time — unlike a signed CDN link, whose
    # expiry token changes per resolve and leaves copies that only match by
    # guesswork.
    assert stream_url("http://10.0.0.5:8080", 15546) == url
    assert normalize_uri(url) == url
