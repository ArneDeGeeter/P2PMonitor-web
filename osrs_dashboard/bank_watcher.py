"""
Background thread that watches ~/.p2p_monitor/screenshots/ for new PNG files,
OCRs the DreamBot paint overlay to extract Output/Input/Total bank values,
and stores them in the bank_snapshots table.

Requires: pip install pytesseract Pillow
          brew install tesseract   (macOS)
"""

import re
import threading
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db as _db

log = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path.home() / ".p2p_monitor" / "screenshots"
_POLL_INTERVAL = 60  # seconds between scans

# Track files we've already processed (persists only in-process)
_processed: set[str] = set()
_lock = threading.Lock()


def _safe_name(p2p_account: str) -> str:
    """Mirror P2P Monitor's safe_account_name: replace non-alphanumeric (except _-) with _."""
    return re.sub(r"[^\w\-]", "_", p2p_account)


def _parse_gp(text: str) -> Optional[int]:
    """Convert '51.3M', '104K', '2.04B' → int GP."""
    text = text.strip().replace(",", "").upper()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([KMB]?)$", text)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2)
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    elif suffix == "B":
        value *= 1_000_000_000
    return int(value)


def _ocr_screenshot(path: Path) -> Optional[dict]:
    """
    Run tesseract on the screenshot and extract Output/Input/Total values.
    Returns dict with keys total_gp, output_gp, input_gp or None if extraction fails.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps
        import pytesseract
    except ImportError:
        return None

    try:
        img = Image.open(path).convert("RGB")

        # Crop to bottom-left quarter where the DreamBot paint lives.
        w, h = img.size
        crop = img.crop((0, h // 2, w // 2, h))

        # Boost contrast: scale to grayscale then invert so dark bg → white bg.
        gray = ImageOps.grayscale(crop)
        # Tesseract works best with dark text on white; our text is white on dark.
        inverted = ImageOps.invert(gray)
        # Upscale 2× for better OCR accuracy on small pixel fonts.
        big = inverted.resize((inverted.width * 2, inverted.height * 2), Image.NEAREST)

        text = pytesseract.image_to_string(
            big,
            config="--psm 6 -c tessedit_char_whitelist=0123456789KMBkmb.,:+- ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        )
    except Exception as exc:
        log.debug("OCR failed for %s: %s", path.name, exc)
        return None

    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r"[Oo]utput\s*[:\s]\s*([0-9.,]+[KMBkmb]?)", line)
        if m and "output_gp" not in result:
            result["output_gp"] = _parse_gp(m.group(1))

        m = re.search(r"[Ii]nput\s*[:\s]\s*([0-9.,]+[KMBkmb]?)", line)
        if m and "input_gp" not in result:
            result["input_gp"] = _parse_gp(m.group(1))

        # "Total: 51.3M +1.9M" — capture the first number (absolute total)
        m = re.search(r"[Tt]otal\s*[:\s]\s*([0-9.,]+[KMBkmb]?)", line)
        if m and "total_gp" not in result:
            result["total_gp"] = _parse_gp(m.group(1))

    return result if result.get("total_gp") else None


def _timestamp_from_filename(filename: str) -> str:
    """Extract ISO timestamp from 'account_20240615_143022.png'."""
    m = re.search(r"(\d{8})_(\d{6})\.png$", filename)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _scan_once(conn) -> None:
    if not SCREENSHOTS_DIR.exists():
        return

    # Build map: safe_p2p_name → account_id for accounts that have p2p_account set
    accounts = conn.execute(
        "SELECT id, p2p_account FROM accounts WHERE p2p_account IS NOT NULL"
    ).fetchall()
    name_map = {_safe_name(row["p2p_account"]): row["id"] for row in accounts}

    if not name_map:
        return

    for png in SCREENSHOTS_DIR.glob("*.png"):
        fname = png.name
        with _lock:
            if fname in _processed:
                continue

        # Find which account this screenshot belongs to
        account_id = None
        for safe_name, aid in name_map.items():
            if fname.startswith(safe_name + "_"):
                account_id = aid
                break

        if account_id is None:
            with _lock:
                _processed.add(fname)
            continue

        values = _ocr_screenshot(png)
        if values and values.get("total_gp"):
            recorded_at = _timestamp_from_filename(fname)
            # Avoid duplicates: skip if we already have a snapshot within 30 seconds of this timestamp
            existing = conn.execute(
                """SELECT id FROM bank_snapshots
                   WHERE account_id=? AND ABS(strftime('%s', recorded_at) - strftime('%s', ?)) < 30""",
                (account_id, recorded_at),
            ).fetchone()
            if not existing:
                _db.insert_bank_snapshot(
                    conn,
                    account_id=account_id,
                    recorded_at=recorded_at,
                    total_gp=values["total_gp"],
                    output_gp=values.get("output_gp"),
                    input_gp=values.get("input_gp"),
                    source="screenshot",
                )
                log.info(
                    "Bank snapshot: account_id=%d  total=%d  file=%s",
                    account_id, values["total_gp"], fname,
                )

        with _lock:
            _processed.add(fname)


def start_bank_watcher(conn, interval: int = _POLL_INTERVAL) -> threading.Thread:
    def loop():
        while True:
            try:
                _scan_once(conn)
            except Exception:
                log.exception("Bank watcher error")
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="bank-watcher")
    t.start()
    return t
