"""Audio URI plumbing: matching, redirect resolution, download mode."""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import requests

AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac",
}


def guess_mime(url: str) -> str:
    path = urlparse(url).path.lower()
    return next((mime for ext, mime in AUDIO_MIME.items() if path.endswith(ext)), "audio/mpeg")


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


def resolve_audio_url(url: str, user_agent: str) -> str:
    """Follow the redirect chain to a final direct URL (HEAD, falling back
    to a 1-byte ranged GET for hosts that reject HEAD)."""
    headers = {"User-Agent": user_agent}
    try:
        resp = requests.head(url, headers=headers, allow_redirects=True, timeout=20)
        resp.raise_for_status()
        return resp.url
    except requests.RequestException:
        resp = requests.get(url, headers={**headers, "Range": "bytes=0-0"},
                            allow_redirects=True, timeout=20, stream=True)
        resp.raise_for_status()
        final = resp.url
        resp.close()
        return final


def download_episode(url: str, media_dir: str, episode_id: int, user_agent: str) -> str:
    os.makedirs(media_dir, exist_ok=True)
    ext = os.path.splitext(urlparse(url).path)[1] or ".mp3"
    filename = f"ep{episode_id}{ext}"
    dest = os.path.join(media_dir, filename)
    with requests.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=60) as resp:
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
# stream as unseekable.
_PASS_THROUGH = ("Content-Type", "Content-Length", "Content-Range",
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
    resp = requests.get(url, headers=headers, stream=True,
                        allow_redirects=True, timeout=30)
    resp.raise_for_status()
    forwarded = {k: resp.headers[k] for k in _PASS_THROUGH if k in resp.headers}
    forwarded.setdefault("Accept-Ranges", "bytes")
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
