from datetime import datetime
from flask import Blueprint, jsonify, request, redirect, url_for, flash, current_app
from ..db import (
    get_account, insert_bank_snapshot, list_bank_snapshots, delete_bank_snapshot,
)
from ..bank_watcher import scan_account, scan_status, ocr_debug, detect_bank_misreads
from ..screenshotter import list_all_window_titles, list_dreambot_windows, capture_window
from ..chart_utils import bucket_key

bank_bp = Blueprint("bank", __name__)


def _conn():
    return current_app.config["DB_CONN"]


def _downsample_for_chart(snaps: list[dict]) -> list[dict]:
    """
    Keep the last 24h at full resolution, average into hourly buckets for
    24h-7d ago, and into daily buckets beyond that — keeps the chart legible
    as snapshots accumulate (one every ~5 minutes) without losing recent detail.
    """
    if not snaps:
        return []

    buckets: dict = {}
    order: list = []
    for idx, s in enumerate(snaps):
        dt = datetime.strptime(s["recorded_at"], "%Y-%m-%d %H:%M:%S")
        key = bucket_key(dt, idx)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(s)

    def _avg(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values)) if values else None

    result = []
    for key in order:
        group = buckets[key]
        if key[0] == "raw":
            result.append(group[0])
        else:
            result.append({
                "recorded_at": key[1],
                "total_gp": _avg([g["total_gp"] for g in group]),
            })
    return result


@bank_bp.get("/accounts/<int:account_id>/bank-chart")
def bank_chart_data(account_id: int):
    snaps = list_bank_snapshots(_conn(), account_id)
    if not snaps:
        return jsonify({"labels": [], "datasets": []})

    snaps = _downsample_for_chart(snaps)
    labels = [s["recorded_at"] for s in snaps]
    total  = [s["total_gp"]  for s in snaps]

    datasets = [
        {
            "label": "Bank Total",
            "data": total,
            "borderColor": "#c8a951",
            "backgroundColor": "rgba(200,169,81,0.1)",
            "yAxisID": "y",
        },
    ]

    return jsonify({"labels": labels, "datasets": datasets})


@bank_bp.post("/accounts/<int:account_id>/bank-snapshots")
def add_bank_snapshot(account_id: int):
    f = request.form
    try:
        total_gp = int(float(f["total_gp"].replace(",", "")))
    except (ValueError, KeyError):
        flash("Invalid GP value.", "danger")
        return redirect(url_for("accounts.account_detail", account_id=account_id) + "#bank")

    recorded_at = f.get("recorded_at", "").strip()
    if recorded_at:
        # datetime-local inputs send "YYYY-MM-DDTHH:MM" (no seconds, 'T' separator);
        # normalize to match the "%Y-%m-%d %H:%M:%S" format used by automatic scans
        # so string ORDER BY recorded_at sorts manual + screenshot rows correctly.
        recorded_at = recorded_at.replace("T", " ")
        if len(recorded_at) == 16:  # "YYYY-MM-DD HH:MM"
            recorded_at += ":00"
    else:
        from datetime import datetime
        recorded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    insert_bank_snapshot(_conn(), account_id, recorded_at, total_gp, source="manual")
    flash("Bank snapshot added.", "success")
    return redirect(url_for("accounts.account_detail", account_id=account_id) + "#bank")


@bank_bp.post("/accounts/<int:account_id>/bank-snapshots/<int:snap_id>/delete")
def delete_bank_snapshot_route(account_id: int, snap_id: int):
    delete_bank_snapshot(_conn(), snap_id)
    flash("Snapshot deleted.", "success")
    return redirect(url_for("accounts.account_detail", account_id=account_id) + "#bank")


@bank_bp.get("/accounts/<int:account_id>/bank-misreads")
def detect_bank_misreads_route(account_id: int):
    """List stored snapshots that look like factor-of-10/100/1000 misreads (read-only)."""
    flagged = detect_bank_misreads(_conn(), account_id)
    return jsonify({"flagged": flagged, "count": len(flagged)})


@bank_bp.post("/accounts/<int:account_id>/bank-snapshots/delete-batch")
def delete_bank_snapshots_batch(account_id: int):
    """Delete a user-confirmed set of snapshot ids."""
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in data.get("ids", [])]
    for snap_id in ids:
        delete_bank_snapshot(_conn(), snap_id)
    return jsonify({"deleted_count": len(ids)})


@bank_bp.post("/accounts/<int:account_id>/bank-scan")
def force_scan(account_id: int):
    """Immediately screenshot + OCR this account's DreamBot window."""
    result = scan_account(_conn(), account_id)
    return jsonify(result)


@bank_bp.get("/bank-scan-status")
def get_scan_status():
    return jsonify(scan_status())


@bank_bp.get("/bank-scan-debug")
def scan_debug():
    """Return all visible window titles and which ones matched as DreamBot."""
    all_titles = list_all_window_titles()
    matched = list_dreambot_windows()
    return jsonify({
        "all_titles": all_titles,
        "dreambot_windows": [
            {"title": w["title"], "account_name": w["account_name"]}
            for w in matched
        ],
    })


@bank_bp.get("/accounts/<int:account_id>/bank-ocr-debug")
def ocr_debug_route(account_id: int):
    """Capture the window and return full OCR diagnostics (images + raw text per crop/PSM)."""
    from ..db import get_account
    row = get_account(current_app.config["DB_CONN"], account_id)
    if not row or not row["p2p_account"]:
        return jsonify({"error": "No P2P Monitor account linked."})

    target = row["p2p_account"].lower()
    windows = list_dreambot_windows()
    win = next((w for w in windows if w["account_name"].lower() == target), None)
    if win is None:
        all_titles = list_all_window_titles()
        return jsonify({
            "error": f"Window for '{row['p2p_account']}' not found.",
            "all_titles": all_titles,
        })

    img = capture_window(win)
    if img is None:
        return jsonify({"error": "Screen capture failed."})

    return jsonify(ocr_debug(img))
