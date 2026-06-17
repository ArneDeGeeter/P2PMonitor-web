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


# ── Scan state ──────────────────────────────────────────────────────

_last_scan: Optional[float] = None
_scan_interval: int = DEFAULT_INTERVAL


def scan_status() -> dict:
    """Return last scan time and seconds until next automatic scan."""
    if _last_scan is None:
        return {"last_scan": None, "next_scan_secs": None, "interval": _scan_interval}
    elapsed = time.time() - _last_scan
    remaining = max(0, _scan_interval - elapsed)
    return {
        "last_scan": datetime.fromtimestamp(_last_scan).strftime("%H:%M:%S"),
        "next_scan_secs": int(remaining),
        "interval": _scan_interval,
    }


# ── Per-account scan (also used by force-scan route) ────────────────

def scan_account(conn, account_id: int) -> dict:
    """
    Immediately screenshot + OCR the DreamBot window for one account.
    Returns {"ok": bool, "total_gp": int|None, "error": str|None}.
    """
    row = conn.execute(
        "SELECT p2p_account FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    if not row or not row["p2p_account"]:
        return {"ok": False, "total_gp": None, "error": "No P2P Monitor account linked."}

    target = row["p2p_account"].lower()
    windows = list_dreambot_windows()
    win = next((w for w in windows if w["account_name"].lower() == target), None)

    if win is None:
        return {"ok": False, "total_gp": None, "error": f"DreamBot window for '{row['p2p_account']}' not found (is it open and not minimised?)."}

    img = capture_region(win["left"], win["top"], win["width"], win["height"])
    if img is None:
        return {"ok": False, "total_gp": None, "error": "Screen capture failed. Check screen recording permissions."}

    values = _ocr_image(img)
    if not values or not values.get("total_gp"):
        return {"ok": False, "total_gp": None, "error": "OCR found no bank value in the window. Is the script paint visible?"}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = conn.execute(
        """SELECT id FROM bank_snapshots
           WHERE account_id=?
           AND ABS(strftime('%s', recorded_at) - strftime('%s', ?)) < 30""",
        (account_id, now_str),
    ).fetchone()
    if not existing:
        _db.insert_bank_snapshot(
            conn,
            account_id=account_id,
            recorded_at=now_str,
            total_gp=values["total_gp"],
            output_gp=values.get("output_gp"),
            input_gp=values.get("input_gp"),
            source="screenshot",
        )

    return {"ok": True, "total_gp": values["total_gp"],
            "output_gp": values.get("output_gp"), "input_gp": values.get("input_gp"),
            "error": None}


# ── Main scan loop ───────────────────────────────────────────────────

def _scan_once(conn) -> None:
    global _last_scan

    windows = list_dreambot_windows()
    if not windows:
        _last_scan = time.time()
        return

    db_accounts = conn.execute(
        "SELECT id, p2p_account FROM accounts WHERE p2p_account IS NOT NULL"
    ).fetchall()
    name_to_id = {row["p2p_account"].lower(): row["id"] for row in db_accounts}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for win in windows:
        wname = (win["account_name"] or "").lower()
        account_id = name_to_id.get(wname)
        if account_id is None:
            continue

        img = capture_region(win["left"], win["top"], win["width"], win["height"])
        if img is None:
            continue

        values = _ocr_image(img)
        if not values or not values.get("total_gp"):
            continue

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
            "Bank snapshot: %s  total=%s  output=%s  input=%s",
            win["account_name"], values["total_gp"],
            values.get("output_gp"), values.get("input_gp"),
        )

    _last_scan = time.time()


def start_bank_watcher(conn, interval: int = DEFAULT_INTERVAL) -> threading.Thread:
    global _scan_interval
    _scan_interval = interval

    def loop():
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
