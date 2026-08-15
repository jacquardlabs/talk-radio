"""In-memory stand-in for sonos_ctl.SonosPlayer. Same surface, no Sonos."""
from __future__ import annotations

from sonos_ctl import TrackInfo


class FakeSonosPlayer:
    ip = "10.0.0.99"
    name = "Fake Room"

    def __init__(self) -> None:
        self.queue: list[dict[str, str]] = []  # {"uri","title","show"}
        self.index = 0                          # current queue index, 0-based
        self.state = "STOPPED"                  # PLAYING/PAUSED_PLAYBACK/STOPPED
        self.position = 0
        self.duration = 1800
        self.seeks: list[int] = []
        self.grouped = False
        self.volume = 50
        # Report this URI as the current track instead of the queue slot's own.
        # Sonos really does hand back a URI that is in no slot — mid-transition,
        # or after a removal renumbered the queue under it — and that is when
        # current_index() has to fall back to a guess.
        self.current_uri_override: str | None = None

    # ── SonosPlayer surface ───────────────────────────────────────────
    def transport_state(self) -> str:
        return self.state

    def current(self) -> TrackInfo | None:
        if not self.queue or self.index >= len(self.queue):
            return None
        item = self.queue[self.index]
        return TrackInfo(uri=self.current_uri_override or item["uri"],
                         queue_index=self.index,
                         position=self.position, duration=self.duration,
                         title=item["title"])

    def queue_uris(self) -> list[str]:
        return [item["uri"] for item in self.queue]

    def queue_length(self) -> int:
        return len(self.queue)

    def add_to_queue(self, uri: str, title: str, show: str,
                     index0: int | None = None) -> None:
        item = {"uri": uri, "title": title, "show": show}
        if index0 is None:
            self.queue.append(item)
        else:
            self.queue.insert(index0, item)

    def remove_from_queue(self, index0: int) -> None:
        del self.queue[index0]
        if index0 < self.index:
            self.index -= 1

    def clear_queue(self) -> None:
        self.queue = []
        self.index = 0
        self.state = "STOPPED"

    def play_from_queue(self, index0: int) -> None:
        self.index = index0
        self.position = 0
        self.state = "PLAYING"

    def play(self) -> None:
        self.state = "PLAYING"

    def pause(self) -> None:
        self.state = "PAUSED_PLAYBACK"

    def stop(self) -> None:
        self.state = "STOPPED"

    def next(self) -> None:
        if self.index + 1 < len(self.queue):
            self.play_from_queue(self.index + 1)
        else:
            self.state = "STOPPED"

    def seek_seconds(self, seconds: int) -> None:
        self.position = seconds
        self.seeks.append(seconds)

    def get_volume(self) -> int:
        return self.volume

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, int(value)))

    def group_all(self) -> None:
        self.grouped = True

    # ── test helpers ──────────────────────────────────────────────────
    def advance_to(self, index0: int) -> None:
        """Simulate Sonos having moved on (tracks before index0 finished)."""
        self.index = index0
        self.position = 0

    def listen(self, seconds: int = 600) -> None:
        """Simulate audio actually coming out of the speaker. A reported
        position is the only evidence tick() has that a track played, so a
        test that advances the needle without this is simulating a track Sonos
        refused — which the DJ now correctly declines to call heard."""
        self.position = seconds
        self.state = "PLAYING"

    def hijack(self, uris: list[str]) -> None:
        """Simulate someone starting Spotify: our queue is replaced."""
        self.queue = [{"uri": u, "title": "spotify", "show": ""} for u in uris]
        self.index = 0
