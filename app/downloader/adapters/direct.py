from pathlib import PurePosixPath
from urllib.parse import urlparse, unquote
from ...shared.utils import IMAGE_EXT
from ..models import Item
from .base import SiteAdapter


class DirectAdapter(SiteAdapter):
    """Direct links to a media file (.mp4, .jpg ...). Images are downloaded without yt-dlp."""
    status = "works"

    def scan(self, url, limit=0, cookies=None):
        path = PurePosixPath(urlparse(url).path)
        kind = "image" if path.suffix.lower() in IMAGE_EXT else "video"
        return [Item(url=url, title=unquote(path.stem) or "file", site=self.site.name,
                     category="direct", kind=kind, thumbnail=url if kind == "image" else "")]
