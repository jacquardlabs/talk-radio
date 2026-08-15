"""Build RSS strings for tests. guid and audio URL derive from the title."""
from datetime import datetime
from email.utils import format_datetime


def slug(title: str) -> str:
    return title.lower().replace(" ", "-")


def rss(feed_title: str, items: list[tuple], feed_description: str | None = None) -> str:
    """items: (title, published), (title, published, itunes_duration), or
    (title, published, itunes_duration, description)."""
    parts = []
    for item in items:
        title, pub = item[0], item[1]
        duration = item[2] if len(item) > 2 else None
        description = item[3] if len(item) > 3 else None
        s = slug(title)
        dur_tag = f"<itunes:duration>{duration}</itunes:duration>" if duration else ""
        desc_tag = f"<description>{description}</description>" if description else ""
        parts.append(
            f"<item><title>{title}</title><guid>guid-{s}</guid>"
            f"<pubDate>{format_datetime(pub)}</pubDate>{dur_tag}{desc_tag}"
            f'<enclosure url="https://cdn.example.com/{s}.mp3" length="1" type="audio/mpeg"/>'
            f"</item>"
        )
    channel_desc = (f"<description>{feed_description}</description>"
                    if feed_description else "")
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        f"<channel><title>{feed_title}</title>{channel_desc}"
        f"{''.join(parts)}</channel></rss>"
    )
