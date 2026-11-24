"""
One adapter class per drama website.

IMPORTANT: none of these sites has been verified. Most short-drama apps use DRM, app-only APIs or login
walls. Each adapter currently tries yt-dlp's extractor / generic extractor and reports a clear error if
that fails. When you test a site and it works (or needs custom code), edit ONLY that site's class here and
change its `status` to "works" / "partial" / "unsupported".
"""
from ....downloader.url_detector import DRAMA
from ..ytdlp_adapter import YtdlpAdapter


class DramaAdapter(YtdlpAdapter):
    category = "drama"
    status = "untested"
    notes = "Not verified. May require login, use DRM, or block third-party downloaders."


def _make(site):
    # Per-site subclass so each site can be customised independently later.
    return type(f"{site.name}Adapter", (DramaAdapter,), {"name": site.name})


DRAMA_ADAPTERS = {s.name: _make(s) for s in DRAMA}
