from pathlib import PurePosixPath
from urllib.parse import urlparse
from ...shared.utils import IMAGE_EXT
from ..models import Item
from .base import SiteAdapter


def _thumb(e):
    if e.get("thumbnail"):
        return e["thumbnail"]
    th = e.get("thumbnails") or []
    return th[-1].get("url", "") if th else ""


class YtdlpAdapter(SiteAdapter):
    """Uses yt-dlp to scan single links, playlists, series and profile/channel pages."""

    def scan(self, url, limit=0, cookies=None):
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
                "ignoreerrors": False, "socket_timeout": 20}
        if limit and limit > 0:
            opts["playlistend"] = limit
        if cookies:
            opts["cookiefile"] = cookies
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
        if not info:
            raise RuntimeError("Nothing found at this link.")
        entries = []
        if info.get("_type") == "playlist" or info.get("entries") is not None:
            self._flatten(info, entries)
            if limit and limit > 0:
                entries = entries[:limit]
            series = info.get("title") or info.get("id") or ""
            items = []
            for i, e in enumerate(entries, 1):
                items.append(self._make(e, url, series=series, episode=i, uploader=info.get("uploader") or ""))
            return items
        return [self._make(info, url)]

    def _flatten(self, node, out):
        for e in node.get("entries") or []:
            if not e:
                continue
            if e.get("_type") == "playlist" or (e.get("entries") is not None):
                self._flatten(e, out)
            else:
                out.append(e)

    def _make(self, e, src_url, series="", episode=0, uploader=""):
        u = e.get("webpage_url") or e.get("url") or src_url
        if not str(u).startswith("http"):
            u = src_url
        ext = (e.get("ext") or PurePosixPath(urlparse(u).path).suffix.lstrip(".")).lower()
        kind = "image" if f".{ext}" in IMAGE_EXT else ("short" if "/shorts/" in u else "video")
        return Item(url=u, title=e.get("title") or e.get("id") or "untitled", site=self.site.name,
                    category=self.site.category, kind=kind, thumbnail=_thumb(e),
                    duration=e.get("duration") or 0, series=series if self.site.category == "drama" else
                    (series or e.get("uploader") or uploader),
                    episode=episode, uploader=e.get("uploader") or uploader)
