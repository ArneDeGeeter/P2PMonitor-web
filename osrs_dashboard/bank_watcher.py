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

def _prepare_crops(img):
    """
    Yield (label, image) pairs to try for OCR, from most specific to full image.
    The DreamBot paint is white text on a dark overlay in the bottom-left,
    but exact position varies by script and window scaling.
    """
    w, h = img.size
    # 1. Bottom-left third — where P2P Master AI puts Output/Input/Total
    yield "bottom-left-third", img.crop((0, h * 2 // 3, w // 2, h))
    # 2. Bottom-left half — wider vertical range
    yield "bottom-left-half",  img.crop((0, h // 2,     w // 2, h))
    # 3. Left half of the full window — catches any vertical position
    yield "left-half",         img.crop((0, 0,           w // 2, h))
    # 4. Full image — last resort
    yield "full",              img


def _process_crop(crop):
    """Invert (white text on dark → dark text on light) and upscale 3×."""
    from PIL import ImageOps, ImageFilter
    gray     = crop.convert("L")
    inverted = ImageOps.invert(gray)
    # Sharpen before upscaling helps pixel fonts
    sharpened = inverted.filter(ImageFilter.SHARPEN)
    return sharpened.resize((sharpened.width * 3, sharpened.height * 3), resample=0)


def _extract_values(text: str) -> dict:
    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r"[Oo0]utput\s*[:\s]\s*([0-9.,]+\s*[KMBkmb]?)", line)
        if m and "output_gp" not in result:
            result["output_gp"] = _parse_gp(m.group(1))
        m = re.search(r"[Ii1]nput\s*[:\s]\s*([0-9.,]+\s*[KMBkmb]?)", line)
        if m and "input_gp" not in result:
            result["input_gp"] = _parse_gp(m.group(1))
        # "Total: 51.3M" or "T0tal: ..." (OCR typo)
        m = re.search(r"[Tt][Oo0]tal\s*[:\s]\s*([0-9.,]+\s*[KMBkmb]?)", line)
        if m and "total_gp" not in result:
            result["total_gp"] = _parse_gp(m.group(1))
    return result


def _ocr_image(img) -> Optional[dict]:
    """Try progressively broader crops until we find Output/Input/Total values."""
    try:
        import pytesseract
    except ImportError:
        return None

    # PSM 6 = uniform block of text; PSM 4 = single column; PSM 11 = sparse text
    psm_modes = ["6", "4", "11"]

    for label, crop in _prepare_crops(img):
        try:
            processed = _process_crop(crop)
        except Exception as exc:
            log.debug("Crop processing failed (%s): %s", label, exc)
            continue

        for psm in psm_modes:
            try:
                text = pytesseract.image_to_string(
                    processed,
                    config=f"--psm {psm} --oem 1",
                )
            except Exception as exc:
                log.debug("Tesseract failed (crop=%s psm=%s): %s", label, psm, exc)
                continue

            result = _extract_values(text)
            if result.get("total_gp"):
                log.debug("OCR success: crop=%s psm=%s  values=%s", label, psm, result)
                return result

    log.debug("OCR found nothing in any crop/PSM combination")
    return None


def ocr_debug(img) -> dict:
    """
    Run OCR on every crop+PSM combination and return full diagnostics.
    Used by the debug route — never called in the normal scan loop.
    """
    import base64, io
    try:
        import pytesseract
    except ImportError:
        return {"error": "pytesseract not installed"}

    def img_to_b64(i):
        buf = io.BytesIO()
        i.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    attempts = []
    for label, crop in _prepare_crops(img):
        try:
            processed = _process_crop(crop)
        except Exception as exc:
            attempts.append({"crop": label, "error": str(exc)})
            continue

        for psm in ["6", "4", "11"]:
            try:
                text = pytesseract.image_to_string(processed, config=f"--psm {psm} --oem 1")
            except Exception as exc:
                attempts.append({"crop": label, "psm": psm, "error": str(exc)})
                continue

            values = _extract_values(text)
            attempts.append({
                "crop":     label,
                "psm":      psm,
                "text":     text.strip(),
                "values":   values,
                "success":  bool(values.get("total_gp")),
                "crop_img": img_to_b64(crop),
            })
            if values.get("total_gp"):
                break  # found it — no need to try more PSMs for this crop

    return {"attempts": attempts, "full_img": img_to_b64(img)}


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
