"""gpodder API v2 — the subset AntennaPod exercises, so a phone can sync
played/in-progress state with this appliance and pull its station list as
subscriptions.

Registered as a Flask blueprint only when GPODDER_USER and GPODDER_PASSWORD
are both set (see web.py:create_app); unset, none of this exists and the app
is byte-for-byte what it was before this module.

Row<->action mapping is pure — no DB, no Flask — so it is unit-testable on
its own; the blueprint functions below do I/O only and call into dj.py for
every write, the same functions the DJ itself uses for a Sonos listen. See
PRODUCT.md's Up Next ladder and db.py's own "no raw status UPDATEs anywhere
else" rule: a remote report has to land through those or rotation's
no-repeat guard and in_order/arc logic won't see it.

Verified against AntennaPod's own source (net/sync/service*), not assumed:
- Only PLAY is ever acted on inbound; NEW/DOWNLOAD/DELETE are no-ops there
  (EpisodeActionFilter.getRemoteActionsOverridingLocalActions).
- A PLAY action needs started>=0, position>0, total>0 together, or the
  fields are dropped entirely (EpisodeAction.readFromJsonObject) and the app
  writes a negative position (AntennaPod #5380).
- "Played" is expressed as a PLAY action whose position sits within
  smartMarkAsPlayedSecs (default 30s) of total, not as its own action type.
- Timestamps are "yyyy-MM-dd'T'HH:mm:ss" UTC, no trailing Z
  (EpisodeAction.writeToJsonObject/PATTERN_ISO_DATEFORMAT) — a remote action
  with a timestamp that doesn't parse loses to any local one, silently.
"""
from __future__ import annotations

import json
import logging
import secrets
from collections import deque
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, Response, jsonify, request

from config import Config
from db import Database, media_key_of
from dj import DJ

logger = logging.getLogger(__name__)

# gpodder's ISO variant: no 'Z', no offset — always UTC, always this shape.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _fmt_ts(iso_z: str | None) -> str:
    """played_at/resume_at are stored as '...Z'; gpodder wants the same UTC
    instant with no zone suffix at all."""
    if not iso_z:
        return datetime.now(timezone.utc).strftime(_TS_FORMAT)
    return iso_z.rstrip("Z")


# ── pure mappers: DB row -> gpodder action ──────────────────────────────

def played_action(episode, feed_url: str) -> dict | None:
    """A finished episode, in the shape AntennaPod's own first-sync upload
    uses: started == position == total == duration. None when there is no
    valid total to report — see dj.tick()'s duration_seconds backfill,
    which is what shrinks that set over time."""
    duration = episode["duration_seconds"]
    if not duration:
        return None
    return {
        "podcast": feed_url,
        "episode": episode["audio_url"],
        "guid": episode["guid"],
        "action": "play",
        "timestamp": _fmt_ts(episode["played_at"]),
        "started": duration,
        "position": duration,
        "total": duration,
    }


def resume_action(episode, feed_url: str) -> dict | None:
    """An in-progress episode. None when duration is unknown — a PLAY action
    needs all three of started/position/total or AntennaPod drops it."""
    duration = episode["duration_seconds"]
    position = episode["resume_seconds"]
    if not duration or position is None:
        return None
    return {
        "podcast": feed_url,
        "episode": episode["audio_url"],
        "guid": episode["guid"],
        "action": "play",
        "timestamp": _fmt_ts(episode["resume_at"]),
        "started": 0,
        "position": max(1, int(position)),
        "total": duration,
    }


def now_playing_action(now_playing: dict, feed_url: str, audio_url: str,
                       guid: str) -> dict | None:
    """The one track live on the speaker right now. Worth reporting
    separately from resume_action: tick() nulls resume_seconds the instant
    it seeks into a track (dj.py), so without this the currently-playing
    episode would report nothing at all until it's paused."""
    duration = now_playing.get("duration")
    if not duration:
        return None
    return {
        "podcast": feed_url,
        "episode": audio_url,
        "guid": guid,
        "action": "play",
        "timestamp": _fmt_ts(None),
        "started": 0,
        "position": max(1, int(now_playing["position"])),
        "total": duration,
    }


# ── the blueprint ────────────────────────────────────────────────────────

def create_blueprint(db: Database, dj: DJ, cfg: Config) -> Blueprint:
    bp = Blueprint("gpodder", __name__)
    user = cfg.gpodder_user
    password = cfg.gpodder_password
    # In-process only: a session outlives nothing but this run, which is
    # fine — AntennaPod re-authenticates whenever a request 401s. Flask's
    # own `session` needs a secret_key create_app never sets, and adding one
    # for a single-credential appliance would be a second thing to protect.
    # Bounded: AntennaPod logs in again whenever it likes, and a set that
    # only grows is a leak on a box that stays up for months. The oldest
    # token falls out; its holder gets a 401 and logs in again.
    sessions: deque[str] = deque(maxlen=16)

    def _check_basic() -> bool:
        auth = request.authorization
        return bool(auth and auth.username == user
                   and secrets.compare_digest(auth.password or "", password))

    def _check_cookie() -> bool:
        token = request.cookies.get("sessionid")
        return bool(token and token in sessions)

    def require_auth(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not (_check_basic() or _check_cookie()):
                return Response(status=401)
            return fn(*a, **kw)
        return wrapper

    def _feeds_for_subscriptions():
        """Enabled, non-news feeds — see the module docstring on why news is
        excluded."""
        return [f for f in db.list_feeds() if f["enabled"] and not f["is_news"]]

    def _resolve_episode(podcast: str, episode: str, guid: str | None):
        """guid is only unique within a feed (UNIQUE(feed_id, guid)), so a
        podcast URL that doesn't resolve to a known feed can't use it —
        fall straight to the unscoped media_key match."""
        feed = db.get_feed_by_url(podcast) if podcast else None
        feed_id = feed["id"] if feed else None
        if guid and feed_id is not None:
            row = db.find_episode_by_guid(feed_id, guid)
            if row is not None:
                return row
        return db.find_episode_by_media_key(media_key_of(episode), feed_id)

    @bp.post("/api/2/auth/<username>/login.json")
    def login(username: str):
        if not _check_basic():
            return Response(status=401)
        token = secrets.token_urlsafe(32)
        sessions.append(token)
        resp = jsonify({})
        resp.set_cookie("sessionid", token, httponly=True, samesite="Lax")
        return resp

    @bp.post("/api/2/auth/<username>/logout.json")
    def logout(username: str):
        token = request.cookies.get("sessionid")
        if token in sessions:
            sessions.remove(token)
        return Response(status=200)

    @bp.get("/api/2/devices/<username>.json")
    @require_auth
    def list_devices(username: str):
        raw = db.kv_get("gpodder_devices")
        devices = json.loads(raw) if raw else {}
        return jsonify([
            {"id": did, "caption": d.get("caption", did),
             "type": d.get("type", "mobile"), "subscriptions": len(_feeds_for_subscriptions())}
            for did, d in devices.items()
        ])

    @bp.post("/api/2/devices/<username>/<device_id>.json")
    @require_auth
    def upsert_device(username: str, device_id: str):
        body = request.get_json(silent=True) or {}
        raw = db.kv_get("gpodder_devices")
        devices = json.loads(raw) if raw else {}
        devices[device_id] = {"caption": body.get("caption", device_id),
                              "type": body.get("type", "mobile")}
        db.kv_set("gpodder_devices", json.dumps(devices))
        return Response(status=200)

    def _subscription_payload(since: str | None):
        feeds = _feeds_for_subscriptions()
        # No removal tracking exists (feeds don't record when they were
        # disabled/deleted), so every sync after the first re-offers the
        # full add list. AntennaPod treats a re-add of an existing
        # subscription as a no-op — see SyncService — so this is inert
        # rather than wrong; a since-aware remove list is future work if a
        # feed churns often enough to matter.
        return {"add": [f["url"] for f in feeds], "remove": [],
                "timestamp": int(datetime.now(timezone.utc).timestamp())}

    @bp.get("/api/2/subscriptions/<username>/<device_id>.json")
    @require_auth
    def get_subscriptions(username: str, device_id: str):
        return jsonify(_subscription_payload(request.args.get("since")))

    @bp.post("/api/2/subscriptions/<username>/<device_id>.json")
    @require_auth
    def post_subscriptions(username: str, device_id: str):
        # Accepted and discarded: subscriptions are radio -> phone only
        # (PRODUCT.md — the phone knows nothing about enabled/category_id/
        # rotation_enabled), so there is nothing to apply. AntennaPod still
        # expects a well-formed response to a POST it made.
        return jsonify({"timestamp": int(datetime.now(timezone.utc).timestamp()),
                        "update_urls": []})

    @bp.get("/subscriptions/<username>/<device_id>.json")
    @require_auth
    def simple_subscriptions(username: str, device_id: str):
        return jsonify([f["url"] for f in _feeds_for_subscriptions()])

    @bp.get("/api/2/episodes/<username>.json")
    @require_auth
    def get_episode_actions(username: str):
        # gpodder's `since` is an epoch integer (seconds), unlike the
        # bare-UTC strings the action payloads themselves carry.
        raw = request.args.get("since")
        try:
            since_epoch = int(raw) if raw else 0
        except ValueError:
            since_epoch = 0
        floor = datetime.fromtimestamp(max(0, since_epoch), tz=timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        actions: list[dict] = []
        for row in db.episodes_played_since(floor):
            a = played_action(row, row["feed_url"])
            if a is not None:
                actions.append(a)
        for row in db.episodes_with_resume_since(floor):
            a = resume_action(row, row["feed_url"])
            if a is not None:
                actions.append(a)
        status = dj.status()
        now_playing = status.get("now_playing")
        if now_playing is not None:
            episode = db.get_episode(now_playing["episode_id"])
            feed = db.get_feed(episode["feed_id"]) if episode else None
            if episode is not None and feed is not None:
                a = now_playing_action(now_playing, feed["url"],
                                       episode["audio_url"], episode["guid"])
                if a is not None:
                    actions.append(a)
        return jsonify({"actions": actions,
                        "timestamp": int(datetime.now(timezone.utc).timestamp())})

    @bp.post("/api/2/episodes/<username>.json")
    @require_auth
    def post_episode_actions(username: str):
        body = request.get_json(silent=True) or []
        for item in body if isinstance(body, list) else []:
            action = item.get("action")
            if action != "play":
                # AntennaPod itself never acts on a received new/download/
                # delete (see module docstring) — we hold ourselves to the
                # same rule rather than inventing meaning for what its own
                # sync engine treats as inert.
                continue
            position = item.get("position")
            total = item.get("total")
            if position is None or not isinstance(position, int) or position <= 0:
                continue
            episode = _resolve_episode(item.get("podcast", ""),
                                       item.get("episode", ""), item.get("guid"))
            if episode is None:
                logger.info("gpodder: unresolved episode action for %r",
                           item.get("episode"))
                continue
            dj.apply_remote_action(episode["id"], position,
                                   total if isinstance(total, int) and total > 0 else None)
        return jsonify({"timestamp": int(datetime.now(timezone.utc).timestamp()),
                        "update_urls": []})

    return bp
