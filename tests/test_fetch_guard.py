"""The outbound fetch guard, and the two origin-level defences around it.

Every URL this app fetches came out of a feed, and POST /feeds is open to
anyone who can reach the app. These tests pin the three things that keep a
feed from turning the appliance into a window onto the house server: no
internal address is ever fetched (on any redirect hop), no proxied body is
served as anything but audio, and no other site can drive a state change.
"""
from __future__ import annotations

import pytest
import requests

import audio
import feeds as feeds_mod
from audio import UnsafeURL, check_url, download_episode, open_upstream
# bound here, not through the audio module: conftest's no_network fixture
# replaces audio.resolve_audio_url with an identity lambda, and this is the
# only place that tests the real one.
from audio import resolve_audio_url
from config import Config
from db import Database
from dj import DJ
from web import create_app

# Public literals, so nothing here needs a resolver. Documentation ranges
# (192.0.2.0/24, 198.51.100.0/24) are no use — ipaddress calls them private.
CDN = "http://93.184.216.34/shows/ep1.mp3"
OTHER_CDN = "http://23.45.67.89/edge/ep1.mp3"
INTERNAL = "http://127.0.0.1:9200/_search"


class FakeResponse:
    """Just the surface safe_request and its callers read."""

    def __init__(self, status_code: int = 200, headers: dict | None = None,
                 body: bytes = b"") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = body
        self.url = ""
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def redirect_to(location: str, status: int = 302) -> FakeResponse:
    return FakeResponse(status, {"Location": location})


def serve(monkeypatch, routes: dict[str, FakeResponse]) -> list[str]:
    """Answer from `routes`; returns the list of URLs actually fetched.

    That list is the whole point — a guard that refuses a hop after
    fetching it has already leaked the request."""
    fetched: list[str] = []

    def fetch(url, headers=None, timeout=None, stream=False,
              allow_redirects=True):
        assert allow_redirects is False, "redirects must be walked by hand"
        fetched.append(url)
        resp = routes[url]
        resp.url = url
        return resp

    monkeypatch.setattr(audio.requests, "get", fetch)
    monkeypatch.setattr(audio.requests, "head", fetch)
    return fetched


def resolves_to(monkeypatch, address: str) -> None:
    """Every hostname resolves to one address, without a resolver."""
    monkeypatch.setattr(audio.socket, "getaddrinfo",
                        lambda *a, **kw: [(0, 0, 0, "", (address, 80))])


# ── check_url ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9200/_search",       # the house server's own loopback
    "http://[::1]:9200/_search",
    "http://192.168.1.1/admin",            # the router
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0:8005/",
    "http://224.0.0.1/",
    "http://[::ffff:127.0.0.1]/",          # loopback in a v6 coat
])
def test_internal_addresses_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURL):
        check_url(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://93.184.216.34/",
    "ftp://93.184.216.34/ep.mp3",
    "http:///no-host",
])
def test_only_public_http_urls_pass(url: str) -> None:
    with pytest.raises(UnsafeURL):
        check_url(url)
    check_url(CDN)
    check_url("https://93.184.216.34/ep.mp3")


def test_a_name_resolving_inward_is_refused(monkeypatch) -> None:
    """The host is public-looking; its A record is not. Only resolution
    tells them apart, which is why the check resolves."""
    resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(UnsafeURL):
        check_url("https://feeds.example.com/rss")


def test_a_name_that_will_not_resolve_is_refused(monkeypatch) -> None:
    """Fail closed. The guard's lookup and the socket's are separate events,
    so a nameserver free to answer SERVFAIL here can answer 127.0.0.1 there
    — letting the failure through buys the fetch for one bad reply, with no
    race to win. A flaky resolver costs a refresh; this costs loopback."""
    def refuse(*a, **kw):
        raise OSError("no such host")

    monkeypatch.setattr(audio.socket, "getaddrinfo", refuse)
    with pytest.raises(UnsafeURL):
        check_url("https://feeds.example.com/rss")


def test_carrier_nat_is_internal(monkeypatch) -> None:
    """100.64.0.0/10 is neither is_private nor is_global, so it slips every
    flag unless it is named."""
    resolves_to(monkeypatch, "100.64.0.1")
    with pytest.raises(UnsafeURL):
        check_url("https://feeds.example.com/rss")


# ── safe_request: the redirect chain ──────────────────────────────────

def test_a_redirect_into_loopback_is_refused_before_it_is_fetched(monkeypatch) -> None:
    """The one that a pre-flight check on the submitted URL alone misses: a
    perfectly public host answers 302 into the house server."""
    fetched = serve(monkeypatch, {CDN: redirect_to(INTERNAL)})

    with pytest.raises(UnsafeURL):
        audio.safe_request("GET", CDN, {}, timeout=5)

    assert fetched == [CDN], "the internal hop was fetched anyway"


def test_a_public_redirect_chain_is_followed_to_its_end(monkeypatch) -> None:
    fetched = serve(monkeypatch, {
        CDN: redirect_to(OTHER_CDN),
        OTHER_CDN: FakeResponse(200, body=b"ID3audio"),
    })

    resp = audio.safe_request("GET", CDN, {}, timeout=5)

    assert (resp.status_code, resp.url) == (200, OTHER_CDN)
    assert fetched == [CDN, OTHER_CDN]


def test_a_relative_location_is_resolved_and_still_checked(monkeypatch) -> None:
    fetched = serve(monkeypatch, {
        CDN: redirect_to("/edge/final.mp3"),
        "http://93.184.216.34/edge/final.mp3": FakeResponse(200),
    })

    audio.safe_request("GET", CDN, {}, timeout=5)

    assert fetched[-1] == "http://93.184.216.34/edge/final.mp3"


def test_an_endless_redirect_loop_is_bounded(monkeypatch) -> None:
    fetched = serve(monkeypatch, {CDN: redirect_to(CDN)})

    with pytest.raises(UnsafeURL):
        audio.safe_request("GET", CDN, {}, timeout=5)

    assert len(fetched) == audio.MAX_REDIRECT_HOPS + 1


# ── the guarded call sites ────────────────────────────────────────────

def test_resolve_audio_url_returns_the_end_of_the_chain(monkeypatch) -> None:
    """Its whole contract: the URI handed to Sonos is the final hop."""
    serve(monkeypatch, {CDN: redirect_to(OTHER_CDN),
                        OTHER_CDN: FakeResponse(200)})
    assert resolve_audio_url(CDN, "UA") == OTHER_CDN


def test_resolve_audio_url_refuses_a_redirect_into_the_house(monkeypatch) -> None:
    fetched = serve(monkeypatch, {CDN: redirect_to(INTERNAL)})
    with pytest.raises(UnsafeURL):
        resolve_audio_url(CDN, "UA")
    assert INTERNAL not in fetched


def test_download_refuses_an_internal_url(monkeypatch, tmp_path) -> None:
    serve(monkeypatch, {})  # nothing is servable; nothing may be fetched
    with pytest.raises(UnsafeURL):
        download_episode(INTERNAL, str(tmp_path), 7, "UA")
    assert list(tmp_path.iterdir()) == []


def test_downloads_take_their_extension_from_the_audio_table(
        monkeypatch, tmp_path) -> None:
    """A feed naming its enclosure .html wrote ep7.html into the media
    directory, which /media then served as HTML on this app's origin."""
    html = "http://93.184.216.34/notes/ep.html"
    serve(monkeypatch, {html: FakeResponse(200, body=b"<script>")})

    filename = download_episode(html, str(tmp_path), 7, "UA")

    assert filename == "ep7.mp3"
    assert [p.name for p in tmp_path.iterdir()] == ["ep7.mp3"]


def test_a_known_audio_extension_survives(monkeypatch, tmp_path) -> None:
    m4a = "http://93.184.216.34/shows/ep.M4A?token=1"
    serve(monkeypatch, {m4a: FakeResponse(200, body=b"ftyp")})
    assert download_episode(m4a, str(tmp_path), 7, "UA") == "ep7.m4a"


def test_upstream_content_type_is_replaced_with_audio(monkeypatch) -> None:
    serve(monkeypatch, {CDN: FakeResponse(
        200, {"Content-Type": "text/html", "Content-Length": "9"},
        body=b"<script>x")})

    _, forwarded = open_upstream(CDN, "UA", None)

    assert forwarded["Content-Type"] == "audio/mpeg"
    assert forwarded["X-Content-Type-Options"] == "nosniff"
    assert "text/html" not in forwarded.values()
    assert forwarded["Content-Length"] == "9"  # the useful ones still pass


def test_feed_fetch_refuses_a_redirect_into_the_house(monkeypatch) -> None:
    """Same reach as /stream, blind: the body never comes back, but the
    request is made and the error text reports on it."""
    rss = "http://93.184.216.34/rss"
    fetched = serve(monkeypatch, {rss: redirect_to(INTERNAL)})

    with pytest.raises(UnsafeURL):
        feeds_mod.fetch_feed(rss, "UA")

    assert fetched == [rss]


# ── the routes ────────────────────────────────────────────────────────

@pytest.fixture
def client(db: Database, cfg: Config):
    dj = DJ(db, cfg, lambda: None)
    app = create_app(db, dj, cfg)
    app.config["TESTING"] = True
    return app.test_client(), db


def test_a_foreign_origin_cannot_change_state(client) -> None:
    """An auto-submitted form on any page a house browser visits reaches
    these routes; no preflight stops a form POST."""
    c, db = client

    resp = c.post("/categories", json={"name": "Injected"},
                  headers={"Origin": "http://evil.example"})

    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False
    assert db.list_categories() == []


def test_a_foreign_origin_cannot_add_a_feed_by_form(client) -> None:
    c, db = client
    resp = c.post("/feeds", data={"url": "http://93.184.216.34/rss"},
                  headers={"Origin": "https://attacker.test"})
    assert resp.status_code == 403
    assert db.list_feeds() == []


def test_a_sandboxed_origin_is_foreign_too(client) -> None:
    c, db = client
    assert c.post("/categories", json={"name": "Injected"},
                  headers={"Origin": "null"}).status_code == 403
    assert db.list_categories() == []


def test_an_absent_origin_is_allowed(client) -> None:
    """curl sends none, and README documents the transport API as a
    scripting surface — an absent header must stay a working one."""
    c, db = client

    resp = c.post("/categories", json={"name": "Documentaries"})

    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert [cat["name"] for cat in db.list_categories()] == ["Documentaries"]


def test_our_own_origin_is_allowed(client) -> None:
    c, db = client
    resp = c.post("/categories", json={"name": "Comedy"},
                  headers={"Origin": "http://localhost"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True


def test_reads_are_never_cross_site_rejected(client) -> None:
    c, _ = client
    resp = c.get("/api/status", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200


def test_stream_never_serves_html_on_our_origin(client, monkeypatch) -> None:
    c, db = client
    fid = db.add_feed("http://93.184.216.34/rss", "Show", None, False)
    db.insert_episode(fid, "g-1", "Ep 1", CDN, "2026-01-01T00:00:00Z")
    episode_id = db.new_episodes_for_feed(fid)[0]["id"]
    serve(monkeypatch, {CDN: FakeResponse(
        200, {"Content-Type": "text/html"}, body=b"<script>steal()</script>")})

    resp = c.get(f"/stream/{episode_id}.mp3")

    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "audio/mpeg"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_stream_will_not_proxy_an_internal_episode_url(client, monkeypatch) -> None:
    """The full-read primitive: a feed whose enclosure names the house
    server's loopback, read back through the proxy."""
    c, db = client
    fid = db.add_feed("http://93.184.216.34/rss", "Show", None, False)
    db.insert_episode(fid, "g-1", "Ep 1", INTERNAL, "2026-01-01T00:00:00Z")
    episode_id = db.new_episodes_for_feed(fid)[0]["id"]
    fetched = serve(monkeypatch, {})

    resp = c.get(f"/stream/{episode_id}.mp3")

    assert resp.status_code == 502
    assert fetched == []


def test_media_files_are_served_nosniff(client, cfg: Config) -> None:
    import os

    c, _ = client
    os.makedirs(cfg.media_dir, exist_ok=True)
    with open(os.path.join(cfg.media_dir, "ep7.mp3"), "wb") as f:
        f.write(b"ID3audio")

    resp = c.get("/media/ep7.mp3")

    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
