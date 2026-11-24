from .direct import DirectAdapter
from .ytdlp_adapter import YtdlpAdapter
from .drama_sites import DRAMA_ADAPTERS


def get_adapter(site):
    if site.category == "direct":
        return DirectAdapter(site)
    if site.name in DRAMA_ADAPTERS:
        return DRAMA_ADAPTERS[site.name](site)
    return YtdlpAdapter(site)
