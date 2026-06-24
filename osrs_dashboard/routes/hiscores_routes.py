import json
from datetime import datetime
from flask import Blueprint, current_app, redirect, url_for, flash, jsonify
from ..db import get_account, get_account_secrets, get_all_snapshots_for_chart, SKILLS
from ..hiscores import poll_account, poll_status
from ..chart_utils import bucket_key

hiscores_bp = Blueprint("hiscores", __name__)


def _conn():
    return current_app.config["DB_CONN"]


def _downsample_for_chart(snapshots: list[dict]) -> list[dict]:
    """
    Keep the last 24h at full resolution, average into hourly buckets for
    24h-7d ago, and into daily buckets beyond that — same grouping rule as
    the bank chart, applied per-skill.
    """
    if not snapshots:
        return []

    buckets: dict = {}
    order: list = []
    for idx, s in enumerate(snapshots):
        dt = datetime.strptime(s["polled_at"], "%Y-%m-%d %H:%M:%S")
        key = bucket_key(dt, idx)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(s)

    result = []
    for key in order:
        group = buckets[key]
        if key[0] == "raw":
            result.append(group[0])
        else:
            avg_skills = {}
            for skill in SKILLS:
                vals = [g["skills"].get(skill, 0) for g in group]
                avg_skills[skill] = round(sum(vals) / len(vals)) if vals else 0
            result.append({"polled_at": key[1], "skills": avg_skills})
    return result


@hiscores_bp.post("/accounts/<int:account_id>/refresh")
def refresh(account_id: int):
    conn = _conn()
    fernet = current_app.config["FERNET"]
    row = get_account(conn, account_id)
    if not row:
        flash("Account not found.", "danger")
        return redirect(url_for("accounts.dashboard"))

    proxy = None
    if row["proxy_url"] and fernet:
        try:
            from ..crypto import decrypt
            proxy = decrypt(fernet, row["proxy_url"])
        except Exception:
            pass

    ok = poll_account(row["username"], account_id, conn, proxy)
    if ok:
        flash("Hiscores refreshed.", "success")
    else:
        flash("Failed to fetch hiscores — account may be unranked or private.", "warning")
    return redirect(url_for("accounts.account_detail", account_id=account_id))


@hiscores_bp.get("/poll-status")
def get_poll_status():
    interval = current_app.config.get("POLL_INTERVAL", 3600)
    return jsonify(poll_status(interval))


@hiscores_bp.get("/accounts/<int:account_id>/chart-data")
def chart_data(account_id: int):
    snapshots = get_all_snapshots_for_chart(_conn(), account_id)
    if not snapshots:
        return jsonify({"labels": [], "datasets": []})

    snapshots = _downsample_for_chart(snapshots)
    labels = [s["polled_at"] for s in snapshots]

    # Overall total XP dataset
    overall_data = [s["skills"].get("overall", 0) for s in snapshots]
    datasets = []
    if any(v > 0 for v in overall_data):
        datasets.append({"label": "Total XP", "data": overall_data, "isOverall": True})

    # Per-skill datasets
    for skill in SKILLS:
        if skill == "overall":
            continue
        data = [s["skills"].get(skill, 0) for s in snapshots]
        if any(v > 0 for v in data):
            datasets.append({"label": skill.title(), "data": data, "isOverall": False})

    return jsonify({"labels": labels, "datasets": datasets})
