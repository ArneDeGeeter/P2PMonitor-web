"""
Cross-platform DreamBot window discovery and background capture.

Each OS uses a different method that does NOT require the window to be
in the foreground:

  macOS   – Quartz CGWindowListCreateImage / screencapture -l <id>
  Windows – PrintWindow via ctypes (renders the window off-screen)
  Linux   – mss screen-region grab (window must be visible; best-effort)

Window listing uses pywinctl on all platforms.
"""

import ctypes
import logging
import platform
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
SYSTEM = platform.system()   # "Darwin" | "Windows" | "Linux"


# ── Boot-time capability detection ──────────────────────────────────

def detect_capabilities() -> dict:
    """
    Probe every dependency and return a plain dict describing what will
    actually be used at runtime.  Called once on startup.

    Keys:
      window_listing  – "pywinctl" | "unavailable"
      capture_method  – human label of the method that will be used
      capture_bg_safe – True if the method works without foregrounding
      ocr             – "tesseract X.Y.Z" | "unavailable: <reason>"
      warnings        – list[str] of actionable install hints
    """
    import shutil

    result = {
        "window_listing":  "unavailable",
        "capture_method":  "unavailable",
        "capture_bg_safe": False,
        "ocr":             "unavailable",
        "warnings":        [],
    }
    warn = result["warnings"].append

    # ── Window listing ───────────────────────────────────────────────
    try:
        import pywinctl  # noqa: F401
        result["window_listing"] = "pywinctl"
    except ImportError:
        warn("pip install pywinctl   # required for window discovery")

    # ── Capture method ───────────────────────────────────────────────
    if SYSTEM == "Darwin":
        try:
            import Quartz  # noqa: F401
            result["capture_method"]  = "CGWindowListCreateImage  (Quartz)"
            result["capture_bg_safe"] = True
        except ImportError:
            # screencapture -l is always available on macOS
            result["capture_method"]  = "screencapture -l  (built-in)"
            result["capture_bg_safe"] = True
            warn("pip install pyobjc-framework-Quartz   # higher quality macOS capture")

    elif SYSTEM == "Windows":
        # PrintWindow is via ctypes — always available on Windows
        result["capture_method"]  = "PrintWindow  (ctypes)"
        result["capture_bg_safe"] = True

    elif SYSTEM == "Linux":
        if shutil.which("xwd"):
            result["capture_method"]  = "xwd  (x11-apps)"
            result["capture_bg_safe"] = True
        elif shutil.which("import") or shutil.which("magick"):
            cmd = "magick import" if shutil.which("magick") else "import"
            result["capture_method"]  = f"{cmd}  (ImageMagick)"
            result["capture_bg_safe"] = True
        else:
            try:
                import mss  # noqa: F401
                result["capture_method"]  = "mss  (screen region — window must be visible)"
                result["capture_bg_safe"] = False
                warn("sudo apt install x11-apps          # xwd: background-safe capture")
                warn("sudo apt install imagemagick       # fallback background-safe capture")
            except ImportError:
                warn("pip install mss                    # required for screen capture")
                warn("sudo apt install x11-apps          # xwd: background-safe capture")

    else:
        try:
            import mss  # noqa: F401
            result["capture_method"]  = "mss  (screen region — window must be visible)"
            result["capture_bg_safe"] = False
        except ImportError:
            warn("pip install mss                    # required for screen capture")

    # mss / Pillow warnings for any OS that might fall back
    try:
        import mss  # noqa: F401
    except ImportError:
        if "pip install mss" not in " ".join(result["warnings"]):
            warn("pip install mss                    # required as capture fallback")

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        warn("pip install Pillow                 # required for image processing")

    # ── OCR ─────────────────────────────────────────────────────────
    try:
        import pytesseract
        ver = pytesseract.get_tesseract_version()
        result["ocr"] = f"tesseract {ver}"
    except ImportError:
        warn("pip install pytesseract            # required for bank value OCR")
        result["ocr"] = "unavailable: pytesseract not installed"
    except Exception:
        if SYSTEM == "Darwin":
            hint = "brew install tesseract"
        elif SYSTEM == "Windows":
            hint = "https://github.com/UB-Mannheim/tesseract/wiki"
        else:
            hint = "sudo apt install tesseract-ocr"
        warn(f"{hint}   # tesseract binary not found")
        result["ocr"] = f"unavailable: tesseract binary missing  ({hint})"

    return result


# ── Window listing (all platforms via pywinctl) ──────────────────────

def _all_windows() -> list:
    try:
        import pywinctl as pwc
        return pwc.getAllWindows()
    except ImportError:
        return []
    except Exception as exc:
        log.debug("pywinctl getAllWindows error: %s", exc)
        return []


def _window_rect(win) -> Optional[tuple[int, int, int, int]]:
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
    titles = []
    for win in _all_windows():
        try:
            t = (win.title or "").strip()
            if t:
                titles.append(t)
        except Exception:
            pass
    return sorted(set(titles))


def list_dreambot_windows() -> list[dict]:
    """
    Return one dict per visible non-minimised DreamBot window.
    The dict always contains: title, account_name, left, top, width, height.
    OS-specific capture handles (window_id on macOS, hwnd on Windows) are
    added when available.
    """
    results = []

    # On macOS, prefer Quartz so we get CGWindowIDs for background capture.
    if SYSTEM == "Darwin":
        results = _list_darwin()
        if results:
            return results
        # Fall through to pywinctl if Quartz unavailable

    for win in _all_windows():
        try:
            if win.isMinimized or not win.isVisible:
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
        if not rect:
            continue
        left, top, width, height = rect
        if width < 10 or height < 10:
            continue

        entry = dict(title=title, account_name=account_name,
                     left=left, top=top, width=width, height=height)

        # Expose OS-specific handle for background capture
        if SYSTEM == "Windows":
            for attr in ("getHandle", "_hWnd"):
                try:
                    v = getattr(win, attr)
                    entry["hwnd"] = int(v() if callable(v) else v)
                    break
                except Exception:
                    pass
        elif SYSTEM == "Linux":
            for attr in ("getHandle", "_hWnd", "_window"):
                try:
                    v = getattr(win, attr)
                    raw = v() if callable(v) else v
                    # Xlib Window objects have an .id attribute
                    entry["xid"] = int(getattr(raw, "id", raw))
                    break
                except Exception:
                    pass

        results.append(entry)
        log.debug("Found DreamBot window: %r  account=%r", title, account_name)

    return results


# ── macOS: Quartz window listing ─────────────────────────────────────

def _list_darwin() -> list[dict]:
    try:
        import Quartz
    except ImportError:
        return []

    try:
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception as exc:
        log.debug("CGWindowListCopyWindowInfo error: %s", exc)
        return []

    results = []
    for w in wins:
        title = (w.get(Quartz.kCGWindowName) or "").strip()
        if "dreambot" not in title.lower():
            continue

        # Skip minimised / offscreen windows
        layer = w.get(Quartz.kCGWindowLayer, 0)
        if layer < 0:
            continue

        parts = [p.strip() for p in title.split(" - ")]
        account_name = parts[1] if len(parts) > 1 else None
        if not account_name:
            continue

        bounds = w.get(Quartz.kCGWindowBounds, {})
        left   = int(bounds.get("X", 0))
        top    = int(bounds.get("Y", 0))
        width  = int(bounds.get("Width", 0))
        height = int(bounds.get("Height", 0))
        wid    = w.get(Quartz.kCGWindowNumber)

        if width < 10 or height < 10 or not wid:
            continue

        results.append(dict(title=title, account_name=account_name,
                            left=left, top=top, width=width, height=height,
                            window_id=wid))
        log.debug("Quartz DreamBot window: %r  id=%d  account=%r", title, wid, account_name)

    return results


# ── Per-OS background capture ────────────────────────────────────────

def capture_window(win: dict):
    """
    Capture a DreamBot window without bringing it to the foreground.
    Returns a PIL Image or None.
    """
    if SYSTEM == "Darwin":
        wid = win.get("window_id")
        if wid:
            img = _capture_darwin_quartz(wid)
            if img:
                return img
        # Fallback: screencapture by window id (still background-safe)
        if wid:
            img = _capture_darwin_screencapture(wid)
            if img:
                return img

    if SYSTEM == "Windows":
        hwnd = win.get("hwnd")
        if hwnd:
            img = _capture_windows_printwindow(hwnd)
            if img:
                return img

    if SYSTEM == "Linux":
        xid = win.get("xid")
        if xid:
            img = _capture_linux_xwd(xid)
            if img:
                return img
            img = _capture_linux_imagemagick(xid)
            if img:
                return img

    # Fallback: mss screen-region grab (window must be visible on screen)
    log.debug("Falling back to mss region capture for '%s'", win.get("account_name"))
    return capture_region(win["left"], win["top"], win["width"], win["height"])


# macOS – CGWindowListCreateImage (best: no foregrounding, high quality) ──

def _capture_darwin_quartz(window_id: int):
    try:
        import Quartz
        from PIL import Image
    except ImportError:
        return None

    try:
        cg_img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow
            | Quartz.kCGWindowListExcludeDesktopElements,
            window_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        if not cg_img:
            return None

        w = Quartz.CGImageGetWidth(cg_img)
        h = Quartz.CGImageGetHeight(cg_img)
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        ctx = Quartz.CGBitmapContextCreate(
            None, w, h, 8, w * 4, cs,
            Quartz.kCGImageAlphaPremultipliedLast,
        )
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cg_img)

        data_ptr = Quartz.CGBitmapContextGetData(ctx)
        import ctypes as ct
        raw = (ct.c_uint8 * (w * h * 4)).from_address(data_ptr)
        img = Image.frombytes("RGBA", (w, h), bytes(raw))
        return img.convert("RGB")
    except Exception as exc:
        log.debug("CGWindowListCreateImage error: %s", exc)
        return None


# macOS – screencapture -l fallback ─────────────────────────────────

def _capture_darwin_screencapture(window_id: int):
    try:
        from PIL import Image
    except ImportError:
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    path = Path(tmp.name)
    try:
        r = subprocess.run(
            ["screencapture", "-l", str(window_id), "-x", "-o", str(path)],
            capture_output=True, timeout=8,
        )
        if r.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            return None
        return Image.open(path).convert("RGB")
    except Exception as exc:
        log.debug("screencapture error: %s", exc)
        return None
    finally:
        path.unlink(missing_ok=True)


# Windows – PrintWindow via ctypes ────────────────────────────────────

def _capture_windows_printwindow(hwnd: int):
    """Render a window to a bitmap via PrintWindow — works for background windows."""
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        user32 = ctypes.windll.user32
        gdi32  = ctypes.windll.gdi32

        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right  - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = user32.GetDC(hwnd)
        mem_dc  = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap  = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        gdi32.SelectObject(mem_dc, bitmap)

        # PW_RENDERFULLCONTENT (2) works for hardware-accelerated (e.g. Java) windows
        ok = user32.PrintWindow(hwnd, mem_dc, 2)
        if not ok:
            # Retry without PW_RENDERFULLCONTENT
            ok = user32.PrintWindow(hwnd, mem_dc, 0)

        if ok:
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize",          ctypes.c_uint32),
                    ("biWidth",         ctypes.c_int32),
                    ("biHeight",        ctypes.c_int32),
                    ("biPlanes",        ctypes.c_uint16),
                    ("biBitCount",      ctypes.c_uint16),
                    ("biCompression",   ctypes.c_uint32),
                    ("biSizeImage",     ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed",       ctypes.c_uint32),
                    ("biClrImportant",  ctypes.c_uint32),
                ]
            bi = BITMAPINFOHEADER()
            bi.biSize      = ctypes.sizeof(BITMAPINFOHEADER)
            bi.biWidth     = w
            bi.biHeight    = -h   # negative = top-down
            bi.biPlanes    = 1
            bi.biBitCount  = 32
            buf = (ctypes.c_byte * (w * h * 4))()
            gdi32.GetDIBits(mem_dc, bitmap, 0, h, buf, ctypes.byref(bi), 0)
            img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
        else:
            img = None

        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        return img.convert("RGB") if img else None
    except Exception as exc:
        log.debug("PrintWindow error: %s", exc)
        return None


# Linux – xwd ────────────────────────────────────────────────────────

def _capture_linux_xwd(xid: int):
    """
    Capture a window by X11 ID using xwd (pre-installed on most distros).
    Works for background / partially occluded windows on X11 and XWayland.
    xwd outputs its own binary format; Pillow can open it directly.
    """
    try:
        import io
        from PIL import Image
    except ImportError:
        return None

    try:
        result = subprocess.run(
            ["xwd", "-id", str(xid), "-silent"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout:
            log.debug("xwd failed (exit %d): %s", result.returncode, result.stderr[:200])
            return None
        return Image.open(io.BytesIO(result.stdout)).convert("RGB")
    except FileNotFoundError:
        log.debug("xwd not found — install with: sudo apt install x11-apps")
        return None
    except Exception as exc:
        log.debug("xwd error: %s", exc)
        return None


# Linux – ImageMagick import ─────────────────────────────────────────

def _capture_linux_imagemagick(xid: int):
    """
    Capture a window by X11 ID using ImageMagick's `import` command.
    Also works for background windows on X11 / XWayland.
    Falls back to `magick` (ImageMagick 7) if `import` isn't found.
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    path = Path(tmp.name)

    for cmd in (["import", "-window", str(xid), str(path)],
                ["magick", "import", "-window", str(xid), str(path)]):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0 and path.exists() and path.stat().st_size > 0:
                img = Image.open(path).convert("RGB")
                path.unlink(missing_ok=True)
                return img
        except FileNotFoundError:
            continue
        except Exception as exc:
            log.debug("ImageMagick import error (%s): %s", cmd[0], exc)

    path.unlink(missing_ok=True)
    log.debug("ImageMagick not found — install with: sudo apt install imagemagick")
    return None


# ── Generic region capture (mss) ─────────────────────────────────────

def capture_region(left: int, top: int, width: int, height: int):
    """Capture a screen region with mss. Window must be visible."""
    try:
        import mss
        from PIL import Image
    except ImportError:
        log.debug("mss or Pillow not installed")
        return None
    try:
        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as exc:
        log.debug("capture_region error: %s", exc)
        return None
