"""
Cross-platform DreamBot window discovery and capture.

  Window listing : pywinctl  (Windows / macOS / Linux)
  Screen capture : mss       (Windows / macOS / Linux)

Linux extra: sudo apt install xdotool   (needed by pywinctl on X11)
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)


def list_dreambot_windows() -> list[dict]:
    """
    Return one dict per visible, non-minimised DreamBot window:
      { title, account_name, left, top, width, height }

    Title format: "DreamBot 4.x - accountname - script - ..."
    account_name is the second dash-separated token.
    """
    try:
        import pywinctl as pwc
    except ImportError:
        log.debug("pywinctl not installed — window discovery unavailable")
        return []

    try:
        candidates = pwc.getWindowsWithTitle("DreamBot")
    except Exception as exc:
        log.debug("pywinctl error: %s", exc)
        return []

    results = []
    for win in candidates:
        try:
            if win.isMinimized or not win.isVisible:
                continue
        except Exception:
            pass

        title = win.title or ""
        parts = [p.strip() for p in title.split(" - ")]
        account_name = parts[1] if len(parts) > 1 else None
        if not account_name:
            continue

        try:
            rect = win.rect          # (left, top, right, bottom)
            left, top = rect.left, rect.top
            width  = rect.right  - rect.left
            height = rect.bottom - rect.top
        except AttributeError:
            try:
                left, top     = win.left, win.top
                width, height = win.width, win.height
            except Exception:
                continue

        if width < 10 or height < 10:
            continue

        results.append({
            "title":        title,
            "account_name": account_name,
            "left":         left,
            "top":          top,
            "width":        width,
            "height":       height,
        })

    return results


def capture_region(left: int, top: int, width: int, height: int):
    """
    Capture an arbitrary screen region and return a PIL Image, or None.
    Uses mss which works on Windows, macOS, and Linux (X11/Wayland with
    xdg-desktop-portal or xdotool).
    """
    try:
        import mss
        from PIL import Image
    except ImportError:
        log.debug("mss or Pillow not installed — capture unavailable")
        return None

    try:
        with mss.mss() as sct:
            region = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(region)
            # mss returns BGRA raw bytes; convert to RGB PIL Image
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as exc:
        log.debug("capture_region error: %s", exc)
        return None
