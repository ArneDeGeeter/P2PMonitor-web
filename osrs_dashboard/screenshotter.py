"""
Cross-platform DreamBot window discovery and capture.

  Window listing : pywinctl  (Windows / macOS / Linux)
  Screen capture : mss       (Windows / macOS / Linux)

Linux extra: sudo apt install xdotool   (needed by pywinctl on X11)
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)


def _all_windows() -> list:
    """Return every window pywinctl can see, or [] if not installed."""
    try:
        import pywinctl as pwc
        return pwc.getAllWindows()
    except ImportError:
        return []
    except Exception as exc:
        log.debug("pywinctl getAllWindows error: %s", exc)
        return []


def _window_rect(win) -> tuple[int, int, int, int] | None:
    """Extract (left, top, width, height) from a pywinctl window object."""
    try:
        r = win.rect
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except AttributeError:
        pass
    try:
        return win.left, win.top, win.width, win.height
    except Exception:
        return None


def list_all_window_titles() -> list[str]:
    """Return every visible window title — used for the debug diagnostic."""
    titles = []
    for win in _all_windows():
        try:
            t = win.title or ""
            if t.strip():
                titles.append(t)
        except Exception:
            pass
    return sorted(set(titles))


def list_dreambot_windows() -> list[dict]:
    """
    Return one dict per visible, non-minimised DreamBot window:
      { title, account_name, left, top, width, height }

    Searches all windows for titles containing 'dreambot' (case-insensitive)
    rather than relying on pywinctl's getWindowsWithTitle which behaves
    differently across OS versions.

    Title format: "DreamBot 4.x - accountname - script - ..."
    account_name is the second dash-separated token.
    """
    results = []
    for win in _all_windows():
        try:
            minimised = win.isMinimized
            visible   = win.isVisible
            if minimised or not visible:
                continue
        except Exception:
            pass

        title = (win.title or "").strip()
        if "dreambot" not in title.lower():
            continue

        parts = [p.strip() for p in title.split(" - ")]
        account_name = parts[1] if len(parts) > 1 else None
        if not account_name:
            continue

        rect = _window_rect(win)
        if rect is None:
            continue
        left, top, width, height = rect
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
        log.debug("Found DreamBot window: %r  account=%r", title, account_name)

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
