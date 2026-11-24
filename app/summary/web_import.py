"""Khmer Movie Summary - Import from Web Link (no Qt, testable). Reuses the downloader adapters, yt-dlp, FFmpeg."""
import hashlib                                                        # បង្កើត id ខ្លីពី URL
from dataclasses import dataclass, field                              # រចនាសម្ព័ន្ធលទ្ធផលពិនិត្យ
from pathlib import Path
from urllib.parse import urlparse

from ..downloader.adapters import get_adapter                         # ប្រើ adapter ដែលមានស្រាប់ (មិនបង្កើតស្ទួន)
from ..downloader.adapters.ytdlp_adapter import YtdlpAdapter
from ..downloader.errors import friendly_error                        # សារកំហុសងាយយល់ (DRM, login, 403 ...)
from ..downloader.models import Item, Job
from ..downloader.url_detector import Site, detect
from ..shared.ffmpeg_util import probe                                # FFmpeg ដែលមានស្រាប់
from ..shared.utils import safe_name

OK, EPISODES, INVALID, ERROR = "ok", "episodes", "invalid", "error"   # ស្ថានភាពនៃការពិនិត្យ Link
_GENERIC = Site("Web", "social", (), "")                              # គេហទំព័រដែលមិនស្គាល់ -> ព្យាយាម extractor ទូទៅ


@dataclass
class LinkReport:
    status: str                                                       # ok | episodes | invalid | error
    message: str                                                      # សារបង្ហាញអ្នកប្រើ
    url: str = ""
    site: str = ""
    site_status: str = ""                                             # untested | works | partial | unsupported
    items: list = field(default_factory=list)                         # វីដេអូ/ភាគដែលរកឃើញ

    @property
    def usable(self):                                                 # អាចចុច Import បាន?
        return self.status in (OK, EPISODES) and bool(self.items)


def normalize_url(url):
    url = (url or "").strip()                                         # ដកចន្លោះ
    if url and "://" not in url:
        url = "https://" + url                                        # បន្ថែម https ប្រសិនបើភ្លេច
    return url


def check_link(url, cookies=None, scan=None):
    """Validate the URL and ask the site adapter what is there. Never downloads, never bypasses protection."""
    url = normalize_url(url)
    try:
        p = urlparse(url)
    except ValueError:
        p = None                                                      # URL ខូចទ្រង់ទ្រាយ
    if not p or p.scheme not in ("http", "https") or "." not in (p.hostname or ""):
        return LinkReport(INVALID, "Invalid link. Paste a full http(s) address.", url)
    site = detect(url) or _GENERIC                                    # ស្គាល់គេហទំព័រពីបញ្ជីដែលមានស្រាប់
    adapter = get_adapter(site) if site is not _GENERIC else YtdlpAdapter(site)
    scan = scan or (lambda u: adapter.scan(u, 0, cookies))            # scan អាចជំនួសបានសម្រាប់ test
    try:
        items = [i for i in scan(url) if i.kind != "image"]           # មិនយករូបភាព
    except ModuleNotFoundError as e:
        return LinkReport(ERROR, f"A required module is missing: {e.name}", url, site.name)
    except Exception as e:                                            # noqa - map every failure to a readable message
        return LinkReport(ERROR, friendly_error(e) + " You can use Import Video with a file instead.", url, site.name,
                          getattr(adapter, "status", ""))
    if not items:
        return LinkReport(ERROR, "No video found at this link. Use Import Video with a file instead.", url, site.name,
                          getattr(adapter, "status", ""))
    st = getattr(adapter, "status", "untested")
    note = " Site not verified yet." if st == "untested" else ""      # មិនអះអាងថាដំណើរការ ប្រសិនបើមិនទាន់ test
    if len(items) > 1:
        return LinkReport(EPISODES, f"{len(items)} episodes found. Choose one.{note}", url, site.name, st, items)
    return LinkReport(OK, f"Video found: {items[0].title}.{note} Download is not guaranteed until it finishes.",
                      url, site.name, st, items)


def work_dir_for(root, item):
    """summary/<name>_<id>   (the movie's work folder; the download goes into its source/ sub-folder)."""
    uid = hashlib.sha1(item.url.encode("utf-8")).hexdigest()[:8]      # id ថេរតាម URL -> Link ដដែលប្រើ folder ដដែល
    return Path(root) / f"{safe_name(item.title, 40)}_{uid}"


def source_dir_for(root, item):
    return work_dir_for(root, item) / "source"                        # ទីតាំងរក្សាទុកវីដេអូដែលបាន download


def make_job(item, root, quality="Best"):
    job = Job(item, str(source_dir_for(root, item)), quality, save_captions=False)   # ប្រើ Job ដែលមានស្រាប់
    job.flat = True                                                   # រក្សាទុកដោយផ្ទាល់ក្នុង source/ (គ្មាន sub-folder)
    return job


def finished_path(job):
    """Path of a finished job, or ValueError with the reason (Done / Duplicate = file already there)."""
    if job.status in ("Done", "Duplicate") and job.path:
        return job.path
    raise ValueError(job.error or f"Download {job.status.lower()}.")  # Failed / Cancelled


def validate_video(path):
    """The downloaded file must really contain a video stream (a page or an audio file is rejected)."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        raise ValueError("The downloaded file is missing or empty.")
    info = probe(p)                                                   # អានព័ត៌មានដោយ FFmpeg
    if not info["has_video"] or info["duration"] <= 0:
        raise ValueError("The downloaded file has no readable video. Try Import Video with a file instead.")
    return info
