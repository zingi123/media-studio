from pathlib import Path
from ..shared.utils import safe_name


def target_dir(root, item) -> Path:
    root = Path(root)
    if item.category == "drama":
        return root / item.site / safe_name(item.series or "Unknown_Series")
    if item.category == "direct":
        return root / "Other_Sources"
    kind = {"short": "Shorts", "image": "Images"}.get(item.kind, "Videos")
    return root / item.site / kind


def base_name(item) -> str:
    """File name without extension. Episodes become Episode_01, others keep their title."""
    if item.category == "drama" and item.episode:
        return f"Episode_{item.episode:02d}"
    return safe_name(item.title, 120)
