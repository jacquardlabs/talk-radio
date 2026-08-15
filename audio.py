"""Audio URI plumbing: matching, redirect resolution, download mode, and the
guard every outbound fetch in this app goes through."""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlparse

import requests

AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac",
}


def guess_mime(url: str) -> str:
    path = urlparse(url).path.lower()
    return next((mime for ext, mime in AUDIO_MIME.items() if path.endswith(ext)), "audio/mpeg")


def audio_ext(url: str) -> str:
    """The extension an episode is saved under, from the audio table only.

    The URL is a feed's, so its path is written by a stranger: taken
    verbatim it let a feed drop ep7.html into the media directory, which
    /media then serves as HTML on this app's own origin. Anything outside
    the table is stored as .mp3 — a wrong extension only costs a guessed
    content type, an arbitrary one costs the origin."""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in AUDIO_MIME else ".mp3"


def normalize_uri(uri: str) -> str:
    p = urlparse(uri)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path}"


def uris_match(a: str, b: str) -> bool:
    """Exact/normalized match, falling back to path-only — some hosts append
    changing auth tokens, and Sonos rewrites schemes on queue items."""
    if a == b or normalize_uri(a) == normalize_uri(b):
        return True
    pa, pb = urlparse(a).path, urlparse(b).path
    return pa not in ("", "/") and pa == pb


# ── outbound fetch guard ─────────────────────────────────────────────
# Every URL this server fetches was written by a stranger. POST /feeds takes
# a feed URL from anyone who can reach the app, and the enclosure hrefs
# inside that feed become episode audio_urls, which /stream/<id>.mp3 fetches
# and hands back verbatim. docker-compose runs with network_mode: host, so
# an href of http://127.0.0.1:9200/_search names the *house server's* own
# loopback — the appliance would be reading internal HTTP on request.
#
# Checking the submitted URL alone does not close it: a perfectly public
# host is free to answer 302 Location: http://169.254.169.254/. So requests'
# redirect following stays off and the chain is walked here, one hop at a
# time, with every hop checked before it is fetched. Every fetch in this app
# that takes a feed-supplied URL goes through safe_request — the check
# belongs at the boundary, not at each call site.
SAFE_SCHEMES = frozenset({"http", "https"})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_REDIRECT_HOPS = 10


class UnsafeURL(Exception):
    """A URL this server must not fetch: a scheme outside http(s), or a host
    that resolves onto the machine itself or the LAN around it."""


# Carrier-grade NAT is neither `is_private` nor `is_global` to `ipaddress`,
# so it falls through every flag below and has to be named outright.
CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:  # ::ffff:127.0.0.1 is loopback in a v6 coat
        ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or ip in CGNAT)  # a v6 address is never `in` a v4 network


def _resolved_ips(host: str, port: int) -> list:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    # a v6 result can carry a zone ("fe80::1%en0"), which ip_address rejects
    return [ipaddress.ip_address(info[4][0].partition("%")[0]) for info in infos]


def check_url(url: str) -> None:
    """Raise UnsafeURL unless this names a public http(s) host.

    *Every* address the name resolves to has to be public: one A record
    pointing at 127.0.0.1 is enough to make the fetch an internal one.

    A name that will not resolve here is refused, not let through. This
    lookup and the socket's own are separate events, so a nameserver that
    answers SERVFAIL here is free to answer 127.0.0.1 there — letting the
    failure through hands an attacker the fetch for the cost of one bad
    reply. A flaky resolver costs a feed refresh, which retries; the other
    way costs the house server's loopback."""
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    if scheme not in SAFE_SCHEMES:
        raise UnsafeURL(f"scheme not allowed: {url}")
    try:
        host, port = parts.hostname, parts.port
    except ValueError as exc:  # a port that is not a number
        raise UnsafeURL(f"bad host in url: {url}") from exc
    if not host:
        raise UnsafeURL(f"no host in url: {url}")
    try:
        addresses = _resolved_ips(host, port or DEFAULT_PORTS[scheme])
    except OSError as exc:
        raise UnsafeURL(f"cannot resolve {host}") from exc
    for ip in addresses:
        if _is_internal(ip):
            raise UnsafeURL(f"{host} resolves to a non-public address ({ip})")


def safe_request(method: str, url: str, headers: dict[str, str], timeout: int,
                 stream: bool = False) -> requests.Response:
    """One guarded http(s) request, redirects walked by hand.

    Returns the response that ends the chain — its .url is the final URL,
    which is what resolve_audio_url hands to Sonos. A streamed response is
    the caller's to close; the intermediate ones are closed here."""
    for _ in range(MAX_REDIRECT_HOPS + 1):
        check_url(url)
        fetch = requests.head if method == "HEAD" else requests.get
        resp = fetch(url, headers=headers, timeout=timeout, stream=stream,
                     allow_redirects=False)
        location = resp.headers.get("Location")
        if resp.status_code not in REDIRECT_CODES or not location:
            return resp
        resp.close()
        url = urljoin(url, location)  # a Location may be relative
    raise UnsafeURL(f"more than {MAX_REDIRECT_HOPS} redirects: {url}")


def resolve_audio_url(url: str, user_agent: str) -> str:
    """Follow the redirect chain to a final direct URL (HEAD, falling back
    to a 1-byte ranged GET for hosts that reject HEAD)."""
    headers = {"User-Agent": user_agent}
    try:
        resp = safe_request("HEAD", url, headers, timeout=20)
        resp.raise_for_status()
        return resp.url
    except requests.RequestException:
        # UnsafeURL is not a RequestException, so a refused hop stays refused
        # rather than being retried as a GET.
        resp = safe_request("GET", url, {**headers, "Range": "bytes=0-0"},
                            timeout=20, stream=True)
        resp.raise_for_status()
        final = resp.url
        resp.close()
        return final


def download_episode(url: str, media_dir: str, episode_id: int, user_agent: str) -> str:
    os.makedirs(media_dir, exist_ok=True)
    filename = f"ep{episode_id}{audio_ext(url)}"
    dest = os.path.join(media_dir, filename)
    with safe_request("GET", url, {"User-Agent": user_agent}, timeout=60,
                      stream=True) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return filename


def delete_local(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def media_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/media/{filename}"


# Sonos stores a queue item's URI in a fixed 1024-byte field and silently keeps
# only the first 1024 bytes — no error, the item just enqueues with a truncated
# URI. BBC's signed CloudFront URLs run past 2000 characters (the `fu` parameter
# alone is ~1000), so what Sonos actually fetches has half its Signature missing
# and comes back 403. The episode never plays and Sonos moves on to the next
# track. Trimming the query is not an option: the signature covers every
# parameter, so dropping any of them 403s just the same. Anything this long has
# to be fetched through us instead.
SONOS_URI_LIMIT = 1024


def stream_url(base_url: str, episode_id: int) -> str:
    """Our own proxy URL for one episode — short, stable, and identifying.

    Stable matters twice over: it stays under the limit above, and because it
    carries the episode id it is the same string every time. CDN URLs are not —
    they carry per-resolve expiry tokens, so two copies of one episode differ
    and the queue can only be matched by guesswork."""
    return f"{base_url.rstrip('/')}/stream/{episode_id}.mp3"


# Headers worth passing back to Sonos from the upstream response. Content-Range
# and Accept-Ranges are what make seeking work; without them Sonos treats the
# stream as unseekable. Content-Type is deliberately absent — see open_upstream.
_PASS_THROUGH = ("Content-Length", "Content-Range",
                 "Accept-Ranges", "Last-Modified", "ETag")


def open_upstream(url: str, user_agent: str, range_header: str | None):
    """Open the CDN response for proxying. Returns (requests.Response, headers
    to forward). The caller must close the response.

    The redirect is followed here, at play time, rather than reused from
    staging: signed CDN links expire, and a listener who seeks an hour in would
    otherwise be handing Sonos a dead signature."""
    headers = {"User-Agent": user_agent}
    if range_header:
        headers["Range"] = range_header
    resp = safe_request("GET", url, headers, timeout=30, stream=True)
    resp.raise_for_status()
    forwarded = {k: resp.headers[k] for k in _PASS_THROUGH if k in resp.headers}
    forwarded.setdefault("Accept-Ranges", "bytes")
    # The upstream's own Content-Type never comes back out. This route serves
    # a feed's bytes on our own origin, so a host declaring text/html would be
    # handing script the run of the API; what this proxies is audio or
    # nothing. nosniff stops a browser promoting the body back to HTML.
    forwarded["Content-Type"] = guess_mime(url)
    forwarded["X-Content-Type-Options"] = "nosniff"
    return resp, forwarded


def detect_base_url(speaker_ip: str, port: int) -> str:
    """The server's LAN IP as the speaker sees it: open a UDP socket toward
    the speaker and read the local address. No packets are sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((speaker_ip, 1400))
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    return f"http://{local_ip}:{port}"
