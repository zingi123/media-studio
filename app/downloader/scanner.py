from .adapters import get_adapter
from .errors import friendly_error
from .url_detector import detect


class ScanError(Exception):
    pass


def scan_url(url, limit=0, cookies=None, progress=None, cancel=None):
    site = detect(url)
    if site is None:
        raise ScanError("Unsupported or unrecognised link: " + url)
    try:
        return get_adapter(site).scan(url, limit=limit, cookies=cookies or None)
    except Exception as e:  # noqa
        raise ScanError(f"{site.name}: {friendly_error(e)}") from e
