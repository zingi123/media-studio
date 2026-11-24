"""App version + where updates come from. Bump __version__ before every release."""
__version__ = "1.0.2"

# GitHub repository that holds the Releases, as "owner/repo" (PUBLIC repo used only for Releases, e.g. MediaStudio-releases). CHANGE THIS.
GITHUB_REPO = "zingi123/MediaStudio-releases"

# Launcher compatibility: an update whose release.json says "min_launcher": N only installs on a MediaStudio.exe
# whose launcher number is >= N (otherwise the user must install the new Setup, because new libraries are needed).
LAUNCHER_API = 1
