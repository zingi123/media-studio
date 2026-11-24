from ..models import Item


class SiteAdapter:
    """One adapter per website. Subclass and override `scan` / `ydl_options` for site-specific logic."""
    name = "generic"
    category = "social"
    status = "untested"   # untested | works | partial | unsupported  (update only after real testing)
    notes = ""

    def __init__(self, site):
        self.site = site

    def scan(self, url, limit=0, cookies=None) -> list:
        raise NotImplementedError

    def ydl_options(self, item: Item) -> dict:
        """Extra yt-dlp options for downloading this item (headers, referer, ...)."""
        return {}
