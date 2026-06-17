"""
Background thread that periodically screenshots every open DreamBot window,
OCRs the paint overlay to extract Output / Input / Total bank values,
and stores them in bank_snapshots.

Auto-detection flow:
  1. Use Quartz to list windows whose title contains "DreamBot".
  2. Parse the account name from the title ("DreamBot X.Y - <name> - ...").
  3. Match that name against the p2p_account column in the DB.
  4. Capture the window with `screencapture -l <window_id>`.
  5. OCR the bottom-left area where the DreamBot paint lives.
  6. Parse "Output: X", "Input: X", "Total: X" lines.
  7. Insert a bank_snapshot row (deduplicates within 30 s).

Requires:
  pip install pyobjc-framework-Quartz Pillow pytesseract
  brew install tesseract
"""

import re
import threading
import time
import logging
from datetime import datetime
from typing import Optional

from . import db as _db
from .screenshotter import list_dreambot_windows, capture_region

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300  # 5 minutes between screenshot rounds


# ── GP string parser ────────────────────────────────────────────────

def _parse_gp(text: str) -> Optional[int]:
    """'51.3M' → 51_300_000 | '104K' → 104_000 | '2.04B' → 2_040_000_000"""
    text = re.sub(r"[,\s]", "", text).upper()
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


# ── OCR ─────────────────────────────────────────────────────────────

def _ocr_image(img) -> Optional[dict]:
    """
    Crop to the bottom-left area of the DreamBot window (where the script
    paint lives), invert + upscale for better tesseract accuracy, then
    extract Output / Input / Total values.
    """
    try:
        from PIL import ImageOps
        import pytesseract
    except ImportError:
        return None

    try:
        w, h = img.size

        # Title bar is ~28 px; game canvas is below.
        # Paint overlay sits in the bottom-left quarter of the game canvas.
        title_h = 28
        game_h = h - title_h
        crop = img.crop((0, title_h + game_h // 2, w // 2, h))

        gray = crop.convert("L")
        inverted = ImageOps.invert(gray)
        # 3× upscale — big wins for small pixel fonts
        big = inverted.resize((inverted.width * 3, inverted.height * 3))

        text = pytesseract.image_to_string(
            big,
            config=(
                "--psm 6 "
                "-c tessedit_char_whitelist="
                "0123456789KMBkmb.,+- "
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:"
            ),
        )
    except Exception as exc:
        log.debug("OCR error: %s", exc)
        return None

    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r"[Oo]utput\s*:?\s*([0-9.,]+[KMBkmb]?)", line)
        if m and "output_gp" not in result:
            result["output_gp"] = _parse_gp(m.group(1))

        m = re.search(r"[Ii]nput\s*:?\s*([0-9.,]+[KMBkmb]?)", line)
        if m and "input_gp" not in result:
            result["input_gp"] = _parse_gp(m.group(1))

        # "Total: 51.3M +1.9M" — capture first number (absolute)
        m = re.search(r"[Tt]otal\s*:?\s*([0-9.,]+[KMBkmb]?)", line)
        if m and "total_gp" not in result:
            result["total_gp"] = _parse_gp(m.group(1))

    return result if result.get("total_gp") else None


# ── Main scan loop ───────────────────────────────────────────────────

def _scan_once(conn) -> None:
    windows = list_dreambot_windows()
    if not windows:
        return

    # Build lookup: p2p_account name → account DB row
    db_accounts = conn.execute(
        "SELECT id, p2p_account FROM accounts WHERE p2p_account IS NOT NULL"
    ).fetchall()
    # Map lowercase for case-insensitive matching
    name_to_id = {row["p2p_account"].lower(): row["id"] for row in db_accounts}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for win in windows:
        wname = (win["account_name"] or "").lower()
        account_id = name_to_id.get(wname)
        if account_id is None:
            log.debug("DreamBot window '%s' has no linked account", win["account_name"])
            continue

        img = capture_region(win["left"], win["top"], win["width"], win["height"])
        if img is None:
            log.debug("Failed to capture window for '%s'", win["account_name"])
            continue

        values = _ocr_image(img)
        if not values or not values.get("total_gp"):
            log.debug("No bank value found in window for '%s'", win["account_name"])
            continue

        # Dedup: skip if a snapshot already exists within 30 s
        existing = conn.execute(
            """SELECT id FROM bank_snapshots
               WHERE account_id=?
               AND ABS(strftime('%s', recorded_at) - strftime('%s', ?)) < 30""",
            (account_id, now_str),
        ).fetchone()
        if existing:
            continue

        _db.insert_bank_snapshot(
            conn,
            account_id=account_id,
            recorded_at=now_str,
            total_gp=values["total_gp"],
            output_gp=values.get("output_gp"),
            input_gp=values.get("input_gp"),
            source="screenshot",
        )
        log.info(
            "Bank snapshot saved: %s  total=%s  output=%s  input=%s",
            win["account_name"],
            values["total_gp"],
            values.get("output_gp"),
            values.get("input_gp"),
        )


def start_bank_watcher(conn, interval: int = DEFAULT_INTERVAL) -> threading.Thread:
    def loop():
        # Small delay on startup so the app finishes initialising
        time.sleep(10)
        while True:
            try:
                _scan_once(conn)
            except Exception:
                log.exception("Bank watcher scan error")
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="bank-watcher")
    t.start()
    log.info("Bank watcher started (interval=%ds)", interval)
    return t
