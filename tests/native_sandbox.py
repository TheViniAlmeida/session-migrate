"""Private-root masks shared by the offline native test fixtures."""

from __future__ import annotations

from pathlib import Path

PRIVATE_ROOTS = ("/home", "/storage", "/root", "/run", "/tmp")


def private_root_masks(filesystem_root: Path = Path("/")) -> list[str]:
    """Mask every existing private root without creating paths in a read-only root.

    ``filesystem_root`` selects a synthetic root for the sandbox regression. A
    dangling symlink is still present and must not silently lose its mask.
    """
    masks = []
    for name in PRIVATE_ROOTS:
        try:
            (filesystem_root / name.lstrip("/")).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise RuntimeError("private-root presence could not be verified") from None
        masks.extend(("--tmpfs", name))
    return masks
