"""Flask routes. Thin: parse/validate input, call dj/db/feeds, return
{"ok", "error"} JSON the dashboard turns into flash messages."""
from __future__ import annotations

import logging
import mimetypes
import re

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, stream_with_context)

# Python's mimetypes table predates .webmanifest; without this the manifest
# is served as application/octet-stream and browsers ignore it
mimetypes.add_type("application/manifest+json", ".webmanifest")

import audio
import feeds as feeds_mod
import sonos_ctl
from config import Config
from db import Database
from dj import DJ

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
logger = logging.getLogger(__name__)
EPISODES_PAGE_SIZE = 25


def _episode_json(e, feed_title: str) -> dict:
    return {"id": e["id"], "title": e["title"], "published_at": e["published_at"],
            "status": e["status"], "show": feed_title,
            "pinned": bool(e["pinned"]),
            # Non-null means the title carries a part marker, so the client
            # knows to offer "Queue series" without having to reimplement the
            # parsing in JavaScript. series_key, matching what the action
            # actually groups on.
            "arc": feeds_mod.series_key(e["title"]),
            "failures": e["failure_count"],
            "description": e["description"]}


def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        return 1


def create_app(db: Database, dj: DJ, cfg: Config) -> Flask:
    app = Flask(__name__)

    def result(error: str | None = None):
        return jsonify({"ok": error is None, "error": error})

    def call_player(fn):
        """Run a DJ transport method and turn any exception a cached-but-dead
        player raises (speaker powered off, network blip) into a graceful
        error response instead of a 500 — dj.status() already degrades this
        way, transport controls must too."""
        try:
            return result(fn())
        except Exception:
            logger.exception("player action failed")
            invalidate = getattr(dj.get_player, "invalidate", None)
            if invalidate:
                invalidate()
            return result("Speaker unreachable")

    @app.get("/")
    def index():
        return render_template("board.html")

    @app.get("/stations")
    def stations_page():
        return render_template("stations.html")

    @app.get("/api/status")
    def api_status():
        return jsonify(dj.status())

    @app.get("/api/speakers")
    def api_speakers():
        return jsonify({"speakers": sonos_ctl.discover_speakers()})

    @app.post("/api/speaker")
    def api_speaker():
        ip = ((request.get_json(silent=True) or {}).get("ip") or "").strip()
        if not ip:
            return result("missing ip")
        db.kv_set("speaker_ip", ip)
        return result()

    @app.post("/player/<action>")
    def player_action(action: str):
        actions = {
            "play": dj.play,
            "pause": dj.pause,
            "restart": lambda: dj.seek_abs(0),
            "back_15": lambda: dj.seek_rel(-15),
            "fwd_30": lambda: dj.seek_rel(30),
            "defer": dj.defer_current,
            "skip_later": dj.skip_later,
            "skip_done": dj.skip_done,
            "stop": dj.stop_off_air,
            "group_all": dj.group_all,
        }
        fn = actions.get(action)
        if fn is None:
            return result(f"unknown action: {action}"), 404
        return call_player(fn)

    @app.post("/player/seek")
    def player_seek():
        body = request.get_json(silent=True) or {}
        try:
            seconds = int(body["seconds"])
        except (KeyError, TypeError, ValueError):
            return result("seconds (int) required")
        return call_player(lambda: dj.seek_abs(seconds))

    @app.post("/player/volume")
    def player_volume():
        body = request.get_json(silent=True) or {}
        try:
            value = int(body["volume"])
        except (KeyError, TypeError, ValueError):
            return result("volume (int) required")
        return call_player(lambda: dj.set_volume(value))

    @app.get("/api/podcasts/search")
    def api_podcast_search():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"results": []})
        try:
            results = feeds_mod.search_podcasts(q, cfg.user_agent)
        except Exception:
            logger.exception("podcast search failed for %r", q)
            return jsonify({"results": [], "error": "Podcast search unavailable"})
        return jsonify({"results": results})

    @app.post("/feeds")
    def add_feed():
        body = request.get_json(silent=True) or request.form
        url = (body.get("url") or "").strip()
        if not url:
            return result("url required")
        include = body.get("include") or "latest"
        if include not in feeds_mod.INCLUDE_MODES:
            return result(f"include must be one of {feeds_mod.INCLUDE_MODES}")
        is_news = str(body.get("is_news", "")).lower() in ("1", "true", "on", "yes")
        try:
            last_n = int(body.get("count") or 0) or None
        except (TypeError, ValueError):
            last_n = None
        try:
            feeds_mod.add_feed(db, cfg, url, is_news, include, last_n)
        except Exception as exc:
            return result(f"could not add feed: {exc}")
        return result()

    @app.post("/feeds/<int:feed_id>/<action>")
    def feed_action(feed_id: int, action: str):
        feed = db.get_feed(feed_id)
        if feed is None:
            return result("no such feed"), 404
        if action == "delete":
            db.delete_feed(feed_id)
        elif action == "toggle":
            db.toggle_feed(feed_id)
        elif action == "news":
            db.set_feed_news(feed_id, not feed["is_news"])
        elif action == "unarchive":
            db.unarchive_feed(feed_id)
        elif action == "playback":
            db.toggle_feed_playback(feed_id)
        else:
            return result(f"unknown action: {action}"), 404
        return result()

    @app.post("/categories")
    def add_category():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return result("name required")
        try:
            db.add_category(name)
        except Exception as exc:
            return result(f"could not add category: {exc}")
        return result()

    @app.post("/categories/<int:category_id>/<action>")
    def category_action(category_id: int, action: str):
        category = db.get_category(category_id)
        if category is None:
            return result("no such category"), 404
        if action == "toggle":
            db.toggle_category_rotation(category_id)
        elif action == "delete":
            db.delete_category(category_id)
        elif action == "rename":
            body = request.get_json(silent=True) or {}
            name = (body.get("name") or "").strip()
            if not name:
                return result("name required")
            try:
                db.rename_category(category_id, name)
            except Exception as exc:
                return result(f"could not rename category: {exc}")
        else:
            return result(f"unknown action: {action}"), 404
        return result()

    @app.post("/feeds/<int:feed_id>/category")
    def set_feed_category(feed_id: int):
        if db.get_feed(feed_id) is None:
            return result("no such feed"), 404
        body = request.get_json(silent=True) or {}
        raw = body.get("category_id")
        try:
            category_id = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return result("category_id must be an integer or null")
        try:
            db.set_feed_category(feed_id, category_id)
        except Exception as exc:
            return result(f"could not set category: {exc}")
        return result()

    @app.get("/api/feeds/<int:feed_id>/episodes")
    def api_feed_episodes(feed_id: int):
        feed = db.get_feed(feed_id)
        if feed is None:
            return jsonify({"error": "no such feed"}), 404
        q = (request.args.get("q") or "").strip() or None
        page = _page_arg()
        total = db.count_episodes_for_feed(feed_id, q)
        episodes = db.episodes_for_feed_page(feed_id, page, EPISODES_PAGE_SIZE, q)
        return jsonify({
            "episodes": [_episode_json(e, feed["title"]) for e in episodes],
            "page": page, "page_size": EPISODES_PAGE_SIZE, "total": total,
        })

    @app.get("/api/episodes/search")
    def api_episode_search():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"episodes": [], "page": 1,
                            "page_size": EPISODES_PAGE_SIZE, "total": 0})
        page = _page_arg()
        total = db.count_search_episodes(q)
        episodes = db.search_episodes(q, page, EPISODES_PAGE_SIZE)
        return jsonify({
            "episodes": [_episode_json(e, e["feed_title"]) for e in episodes],
            "page": page, "page_size": EPISODES_PAGE_SIZE, "total": total,
        })

    @app.post("/episodes/<int:episode_id>/release")
    def release_episode(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        db.release_episode(episode_id)
        return result()

    @app.post("/episodes/release")
    def release_episodes_bulk():
        body = request.get_json(silent=True) or {}
        try:
            ids = [int(i) for i in (body.get("ids") or [])]
        except (TypeError, ValueError):
            return result("ids must be a list of integers")
        db.release_episodes(ids)
        return result()

    @app.post("/episodes/<int:episode_id>/play_next")
    def play_episode_next(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.play_episode(episode_id, "next"))

    @app.post("/episodes/<int:episode_id>/play_now")
    def play_episode_now(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.play_episode(episode_id, "now"))

    @app.post("/episodes/<int:episode_id>/play_last")
    def play_episode_last(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.play_episode(episode_id, "last"))

    @app.post("/episodes/<int:episode_id>/pin")
    def pin_episode(episode_id: int):
        """Toggle whether this row survives "Refresh all". Body may carry
        {"pinned": bool} to set it outright; without one this flips whatever
        is stored, which is what a tapped pin wants."""
        episode = db.get_episode(episode_id)
        if episode is None:
            return result("no such episode"), 404
        body = request.get_json(silent=True) or {}
        want = body.get("pinned")
        return result(dj.pin_episode(
            episode_id, not episode["pinned"] if want is None else bool(want)))

    @app.post("/queue/refresh")
    def refresh_queue():
        return call_player(dj.refresh_queue)

    @app.post("/episodes/<int:episode_id>/queue_arc")
    def queue_arc(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.queue_arc(episode_id))

    @app.get("/api/episodes/<int:episode_id>/arc")
    def api_episode_arc(episode_id: int):
        """What "Queue series" would enqueue, so the button can name the count
        before the user commits to it."""
        episode = db.get_episode(episode_id)
        if episode is None:
            return jsonify({"error": "no such episode"}), 404
        feed = db.get_feed(episode["feed_id"])
        parts = dj.arc_parts(episode)
        return jsonify({"parts": [_episode_json(e, feed["title"] if feed else "")
                                  for e in parts]})

    @app.post("/queue/reorder")
    def reorder_queue():
        """to_position is an index within Up Next, not the Sonos queue —
        finished tracks stay on the queue, so the client's view and the raw
        indices diverge."""
        body = request.get_json(silent=True) or {}
        try:
            episode_id = int(body["episode_id"])
            to_position = int(body["to_position"])
        except (KeyError, TypeError, ValueError):
            return result("episode_id and to_position (ints) required")
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.move_in_queue(episode_id, to_position))

    @app.post("/episodes/<int:episode_id>/drop")
    def drop_episode(episode_id: int):
        if db.get_episode(episode_id) is None:
            return result("no such episode"), 404
        return call_player(lambda: dj.drop_from_queue(episode_id))

    @app.post("/schedules")
    def add_schedule():
        body = request.get_json(silent=True) or {}
        time_str = (body.get("time") or "").strip()
        days = body.get("days") or []
        if not TIME_RE.match(time_str):
            return result("time must be HH:MM")
        try:
            day_ints = sorted({int(d) for d in days})
        except (TypeError, ValueError):
            day_ints = []
        if not day_ints or not all(0 <= d <= 6 for d in day_ints):
            return result("pick at least one day")
        db.add_schedule(time_str, day_ints)
        return result()

    @app.post("/schedules/<int:schedule_id>/<action>")
    def schedule_action(schedule_id: int, action: str):
        if not any(s["id"] == schedule_id for s in db.list_schedules()):
            return result("no such schedule"), 404
        if action == "toggle":
            db.toggle_schedule(schedule_id)
        elif action == "delete":
            db.delete_schedule(schedule_id)
        else:
            return result(f"unknown action: {action}"), 404
        return result()

    @app.get("/media/<path:filename>")
    def media(filename: str):
        # conditional=True serves HTTP Range requests — what makes Sonos
        # seeking work on local files in download mode
        return send_from_directory(cfg.media_dir, filename, conditional=True)

    @app.get("/stream/<int:episode_id>.mp3")
    def stream(episode_id: int):
        """Pipe a CDN episode through to Sonos.

        For hosts whose signed URLs run past what Sonos will store (see
        audio.SONOS_URI_LIMIT) the speaker gets this address instead. Range
        headers pass both ways so seeking still works, and the body streams —
        a full episode is tens of megabytes and must never be buffered whole.
        """
        episode = db.get_episode(episode_id)
        if episode is None:
            return result("no such episode"), 404
        try:
            upstream, headers = audio.open_upstream(
                episode["audio_url"], cfg.user_agent, request.headers.get("Range"))
        except Exception:
            logger.exception("upstream fetch failed for episode %s", episode_id)
            return result("could not fetch that episode"), 502

        def pump():
            try:
                for chunk in upstream.iter_content(chunk_size=1 << 16):
                    yield chunk
            finally:
                upstream.close()

        return Response(stream_with_context(pump()), status=upstream.status_code,
                        headers=headers)

    return app
