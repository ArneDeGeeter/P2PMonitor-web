from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, jsonify, current_app
from ..db import get_financial_overview, get_all_accounts_snapshots_for_chart, SKILLS

overview_bp = Blueprint("overview", __name__)

_PERIOD_LABELS = {
    "1h": "last 1 hour",
    "24h": "last 24 hours",
    "7d": "last 7 days",
    "30d": "last 30 days",
    "3m": "last 3 months",
    "all": "all time",
}


def _since_dt(period: str) -> str:
    now = datetime.utcnow()
    offsets = {
        "1h": timedelta(hours=1),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "3m": timedelta(days=90),
    }
    if period in offsets:
        return (now - offsets[period]).strftime("%Y-%m-%d %H:%M:%S")
    return "2000-01-01 00:00:00"


@overview_bp.get("/overview")
def financial_overview():
    conn = current_app.config["DB_CONN"]
    data = get_financial_overview(conn)

    from_dt = request.args.get("from", "").strip()
    to_dt = request.args.get("to", "").strip()
    period = request.args.get("period", "30d") if not (from_dt and to_dt) else "custom"
    xp_label = (
        f"{from_dt} → {to_dt}" if period == "custom"
        else _PERIOD_LABELS.get(period, "last 30 days")
    )

    return render_template(
        "overview.html",
        data=data,
        period=period,
        from_dt=from_dt,
        to_dt=to_dt,
        xp_label=xp_label,
    )


@overview_bp.get("/overview/xp-chart-data")
def xp_chart_data():
    from_dt = request.args.get("from", "").strip()
    to_dt = request.args.get("to", "").strip()
    period = request.args.get("period", "30d") if not (from_dt and to_dt) else "custom"

    if period == "custom" and from_dt and to_dt:
        since_str = from_dt
        until_str = to_dt + " 23:59:59"
    else:
        since_str = _since_dt(period)
        until_str = None

    conn = current_app.config["DB_CONN"]
    snapshots = get_all_accounts_snapshots_for_chart(conn, since_str, until_str)

    if not snapshots:
        return jsonify({"labels": [], "datasets": []})

    labels = [s["polled_at"] for s in snapshots]

    datasets = []
    overall_data = [s["skills"].get("overall", 0) for s in snapshots]
    overall_gained = [s["skills_gained"].get("overall", 0) for s in snapshots]
    if any(v > 0 for v in overall_data):
        datasets.append({
            "label": "Total XP",
            "data": overall_data,
            "gained_data": overall_gained,
            "isOverall": True,
        })

    for skill in SKILLS:
        if skill == "overall":
            continue
        data = [s["skills"].get(skill, 0) for s in snapshots]
        gained = [s["skills_gained"].get(skill, 0) for s in snapshots]
        if any(v > 0 for v in data):
            datasets.append({
                "label": skill.title(),
                "data": data,
                "gained_data": gained,
                "isOverall": False,
            })

    return jsonify({"labels": labels, "datasets": datasets})
