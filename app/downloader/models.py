import threading
import uuid
from dataclasses import dataclass, field


@dataclass
class Item:
    url: str
    title: str
    site: str                 # folder/display name
    category: str             # social | drama | direct
    kind: str = "video"       # video | short | image
    thumbnail: str = ""
    duration: float = 0
    series: str = ""
    episode: int = 0
    uploader: str = ""
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)


class Job:
    """One download. Status: Ready, Queued, Downloading, Paused, Done, Failed, Cancelled, Duplicate."""

    def __init__(self, item: Item, root: str, quality: str, save_captions=True, allow_duplicate=False):
        self.item = item
        self.root = root
        self.quality = quality
        self.save_captions = save_captions
        self.allow_duplicate = allow_duplicate
        self.flat = False          # True = save files directly in root (no site/series sub-folders)
        self.status = "Queued"
        self.progress = 0
        self.speed = 0.0
        self.eta = None
        self.path = ""
        self.size = 0
        self.error = ""
        self.cancel_flag = False
        self.run_evt = threading.Event()
        self.run_evt.set()