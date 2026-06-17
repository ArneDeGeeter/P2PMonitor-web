from flask import Blueprint, jsonify, request, redirect, url_for, flash, current_app
from ..db import (
    get_account, insert_bank_snapshot, list_bank_snapshots, delete_bank_snapshot,
)
from ..bank_watcher import scan_account, scan_status
from ..screenshotter import list_all_window_titles, list_dreambot_windows

bank_bp = Blueprint("bank", __name__)


def _conn():
    return current_app.config["DB_CONN"]


@bank_bp.get("/accounts/<int:account_id>/bank-chart")
def bank_chart_data(account_id: int):
    snaps = list_bank_snapshots(_conn(), account_id)
    if not snaps:
        return jsonify({"labels": [], "datasets": []})

    labels = [s["recorded_at"] for s in snaps]
    total  = [s["total_gp"]  for s in snaps]
    output = [s["output_gp"] for s in snaps]
    inp    = [s["input_gp"]  for s in snaps]

    datasets = [
        {
            "label": "Bank Total",
            "data": total,
            "borderColor": "#c8a951",
            "backgroundColor": "rgba(200,169,81,0.1)",
            "yAxisID": "y",
        },
    ]
    if any(v is not None for v in output):
        datasets.append({
            "label": "Output",
            "data": output,
            "borderColor": "#5cb85c",
            "backgroundColor": "transparent",
            "yAxisID": "y",
        })
    if any(v is not None for v in inp):
        datasets.append({
            "label": "Input",
            "data": inp,
            "borderColor": "#d9534f",
            "backgroundColor": "transparent",
            "yAxisID": "y",
        })

    return jsonify({"labels": labels, "datasets": datasets})


@bank_bp.post("/accounts/<int:account_id>/bank-snapshots")
def add_bank_snapshot(account_id: int):
    f = request.form
    try:
        total_gp = int(float(f["total_gp"].replace(",", "")))
    except (ValueError, KeyError):
        flash("Invalid GP value.", "danger")
        return redirect(url_for("accounts.account_detail", account_id=account_id) + "#bank")

    output_gp = None
    input_gp = None
    try:
        if f.get("output_gp", "").strip():
            output_gp = int(float(f["output_gp"].replace(",", "")))
        if f.get("input_gp", "").strip():
            input_gp = int(float(f["input_gp"].replace(",", "")))
    except ValueError:
        pass

    recorded_at = f.get("recorded_at", "").strip()
    if not recorded_at:
        from datetime import datetime
        recorded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    insert_bank_snapshot(_conn(), account_id, recorded_at, total_gp, output_gp, input_gp, source="manual")
    flash("Bank snapshot added.", "success")
    return redirect(url_for("accounts.account_detail", account_id=account_id) + "#bank")


@bank_bp.post("/accounts/<int:account_id>/bank-snapshots/<int:snap_id>/delete")
def delete_bank_snapshot_route(account_id: int, snap_id: int):
    delete_bank_snapshot(_conn(), snap_id)
    flash("Snapshot deleted.", "success")
    return redirect(url_for("accounts.account_detail", account_id=account_id) + "#bank")


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
