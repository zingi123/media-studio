"""Website catalogue and URL detection for MediaStudio.

Supported: the social networks and movie/drama sites listed below, plus direct video/image/audio file links
(detected by file extension). If a site moves to a new address, add the new domain to its `domains` tuple.
"""
from dataclasses import dataclass
from urllib.parse import urlparse
from pathlib import PurePosixPath

from ..shared.utils import IMAGE_EXT, VIDEO_EXT, AUDIO_EXT


@dataclass(frozen=True)
class Site:
    name: str
    category: str
    domains: tuple
    homepage: str = ""
    features: str = ""


# ================= SOCIAL NETWORKS =================

SOCIAL = [
    Site("Facebook", "social",
         ("facebook.com", "fb.watch", "fb.com"),
         "https://www.facebook.com/",
         "Videos • Reels • Photos"),

    Site("Instagram", "social",
         ("instagram.com",),
         "https://www.instagram.com/",
         "Reels • Photos"),

    Site("Pinterest", "social",
         ("pinterest.com", "pin.it"),
         "https://www.pinterest.com/",
         "Profile • Search • Related"),

    Site("YouTube", "social",
         ("youtube.com", "youtu.be", "youtube-nocookie.com"),
         "https://www.youtube.com/",
         "Videos • Shorts • Playlists"),

    Site("TikTok", "social",
         ("tiktok.com",),
         "https://www.tiktok.com/",
         "Videos • Profiles"),

    Site("Douyin", "social",
         ("douyin.com", "iesdouyin.com"),
         "https://www.douyin.com/",
         "Videos • Profiles"),

    Site("Kwai", "social",
         ("kwai.com", "kwai-video.com"),
         "https://www.kwai.com/",
         "Videos • Profiles"),

    Site("SnackVideo", "social",
         ("snackvideo.com",),
         "https://www.snackvideo.com/",
         "Videos • Profiles"),

    Site("Kuaishou", "social",
         ("kuaishou.com",),
         "https://www.kuaishou.com/",
         "Videos • Profiles"),

    Site("Xiaohongshu", "social",
         ("xiaohongshu.com", "xhslink.com"),
         "https://www.xiaohongshu.com/",
         "RedNote • Notes • Photos • Videos"),

    Site("Medal.tv", "social",
         ("medal.tv",),
         "https://medal.tv/",
         "Gaming Clips"),

    Site("Threads", "social",
         ("threads.com", "threads.net"),
         "https://www.threads.com/",
         "Posts • Media"),

    Site("SUNO", "social",
         ("suno.com",),
         "https://suno.com/",
         "AI Music • Tracks"),

    Site("X / Twitter", "social",
         ("x.com", "twitter.com", "t.co"),
         "https://x.com/",
         "Posts • Videos • Images"),

    Site("Reddit", "social",
         ("reddit.com", "redd.it", "redditmedia.com"),
         "https://www.reddit.com/",
         "Posts • Videos • Images"),

    Site("Dailymotion", "social",
         ("dailymotion.com", "dai.ly"),
         "https://www.dailymotion.com/",
         "Videos • Channels"),

    Site("BiliBili", "social",
         ("bilibili.com", "bilibili.tv", "b23.tv"),
         "https://www.bilibili.com/",
         "Videos • Channels • Series"),
]


# ================= MOVIE & DRAMA (only these 4) =================

DRAMA = [
    Site("KHDiamond", "drama", ("khdiamond.net",),
         "https://khdiamond.net/"),

    Site("KiSSKH", "drama",
         ("kisskh.org", "kisskh.ovh", "kisskh.do", "kisskh.co"),
         "https://kisskh.org/"),

    Site("KHFullHD", "drama", ("khfullhd.net", "khfullhd.co"),
         "https://khfullhd.net/"),

    Site("Hongguo", "drama", ("hongguoduanju.com",),
         "https://hongguoduanju.com/"),
]


# ================= OTHER LINKS (direct media files, detected by extension) =================

DIRECT = Site(
    "Other_Sources",
    "direct",
    (),
    "",
    "Any link that ends in a video / image / audio file extension"
)

ALL_SITES = SOCIAL + DRAMA


# ================= URL DETECTOR =================

def detect(url: str):
    """Return the matching Site, DIRECT for a direct media file link, or None."""

    url = (url or "").strip()

    if not url:
        return None

    if "://" not in url:
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()

    if not host or "." not in host:
        return None

    for site in ALL_SITES:
        for domain in site.domains:
            if host == domain or host.endswith("." + domain):
                return site

    ext = PurePosixPath(parsed.path).suffix.lower()

    if ext in VIDEO_EXT or ext in IMAGE_EXT or ext in AUDIO_EXT:
        return DIRECT

    return None