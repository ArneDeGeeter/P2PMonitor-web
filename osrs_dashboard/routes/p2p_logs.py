import json
import os
from pathlib import Path
from flask import Blueprint, jsonify, current_app
from ..db import get_account

p2p_bp = Blueprint("p2p", __name__)

P2P_BASE = Path.home() / ".p2p_monitor" / "history"

# Icons / colours per event type shown in the UI
EVENT_META = {
    "script_event": {"icon": "bi-play-circle",     "color": "#c8a951"},
    "levelup":      {"icon": "bi-arrow-up-circle", "color": "#5cb85c"},
    "drop":         {"icon": "bi-bag-fill",        "color": "#17a2b8"},
    "death":        {"icon": "bi-skull",           "color": "#d9534f"},
    "error":        {"icon": "bi-exclamation-triangle", "color": "#f0ad4e"},
    "task":         {"icon": "bi-list-task",       "color": "#a0a0c0"},
    "slayer_task":  {"icon": "bi-crosshair",       "color": "#c8a951"},
    "slayer_complete": {"icon": "bi-check-circle", "color": "#5cb85c"},
    "slayer_skip":  {"icon": "bi-skip-forward",    "color": "#a0a0c0"},
    "quest":        {"icon": "bi-map",             "color": "#9b59b6"},
    "quest_started":{"icon": "bi-map",             "color": "#9b59b6"},
    "chat":         {"icon": "bi-chat-dots",       "color": "#a0a0c0"},
}
DEFAULT_META = {"icon": "bi-dot", "color": "#a0a0c0"}


def _conn():
    return current_app.config["DB_CONN"]


def read_p2p_history(p2p_account: str, limit: int = 200) -> list[dict]:
    path = P2P_BASE / p2p_account / "history.jsonl"
    if not path.exists():
        return []

    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            # skip scan records — they have no time/value
            if entry.get("type") == "scan":
                continue
            events.append(entry)

    # return most recent `limit` entries, newest first
    return list(reversed(events[-limit:]))


@p2p_bp.get("/accounts/<int:account_id>/p2p-logs")
def p2p_logs(account_id: int):
    row = get_account(_conn(), account_id)
    if not row or not row["p2p_account"]:
        return jsonify({"error": "No P2P Monitor account linked.", "events": []})

    limit = 200
    events = read_p2p_history(row["p2p_account"], limit=limit)

    enriched = []
    for e in events:
        meta = EVENT_META.get(e.get("type", ""), DEFAULT_META)
        enriched.append({
            "time":     e.get("time", ""),
            "type":     e.get("type", ""),
            "value":    e.get("value", ""),
            "activity": e.get("activity", ""),
            "icon":     meta["icon"],
            "color":    meta["color"],
        })

    p2p_path = str(P2P_BASE / row["p2p_account"] / "history.jsonl")
    exists = os.path.exists(p2p_path)

    return jsonify({
        "p2p_account": row["p2p_account"],
        "log_path": p2p_path,
        "log_exists": exists,
        "events": enriched,
    })


@p2p_bp.get("/p2p-accounts")
def list_p2p_accounts():
    """Return folder names found under ~/.p2p_monitor/history/ for autocomplete."""
    if not P2P_BASE.exists():
        return jsonify([])
    names = [d.name for d in P2P_BASE.iterdir() if d.is_dir()]
    return jsonify(sorted(names))
