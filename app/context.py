from dataclasses import dataclass
from .shared.db import DB
from .shared.settings import Settings
from .downloader.download_manager import DownloadManager


@dataclass
class Context:
    settings: Settings
    db: DB
    downloads: DownloadManager
