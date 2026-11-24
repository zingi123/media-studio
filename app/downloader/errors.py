def friendly_error(exc: Exception) -> str:
    """Turn extractor/network errors into a message a beginner can act on."""
    m = str(exc)
    low = m.lower()
    if "drm" in low:
        return "This content is DRM-protected and cannot be downloaded."
    if "unsupported url" in low:
        return "This site/page is not supported by the extractor (or the link is not a media page)."
    if any(k in low for k in ("login", "sign in", "cookies", "private", "age-restricted", "age restricted")):
        return ("This content needs a logged-in account. Export your browser cookies to a cookies.txt file "
                "and set it in Settings.")
    if any(k in low for k in ("429", "too many requests", "rate")):
        return "The site is rate-limiting requests. Wait a while and retry."
    if "403" in low or "forbidden" in low:
        return "The site blocked the request (403). It may block third-party downloaders."
    if "404" in low or "not found" in low:
        return "Page not found (404). Check the link."
    if "ffmpeg" in low:
        return "FFmpeg is required for this download. See Settings > Tools."
    for pre in ("ERROR: ", "[generic] "):
        m = m.replace(pre, "")
    return m.strip()[:300] or "Unknown error"
