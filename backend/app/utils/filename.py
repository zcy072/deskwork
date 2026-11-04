import re
from pathlib import Path


def normalize_upload_filename(raw: str) -> str:
    """Return a shell-safe basename for uploaded files."""
    name = Path(raw).name
    stem = Path(name).stem
    suffix = Path(name).suffix

    normalized_stem = re.sub(r"[\s()]+", "_", stem)
    normalized_stem = re.sub(r"_+", "_", normalized_stem).strip("_")

    if not normalized_stem:
        normalized_stem = "upload"

    normalized = f"{normalized_stem}{suffix}"

    if ".." in Path(normalized).parts or "/" in normalized or "\\" in normalized:
        return f"upload{suffix}"

    return normalized


def unique_upload_name(
    uploads_dir: Path,
    filename: str,
    reserved: set[str] | None = None,
) -> str:
    """Return a filename that does not collide with existing or reserved names."""
    taken = reserved or set()
    if filename not in taken and not (uploads_dir / filename).exists():
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in taken and not (uploads_dir / candidate).exists():
            return candidate
        counter += 1
