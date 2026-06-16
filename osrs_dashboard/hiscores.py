import time
import threading
import requests
from typing import Optional
from . import db as _db

STATS_URL = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.ws"
HEADERS = {"User-Agent": "Mozilla/5.0"}

_poll_lock = threading.Lock()
_last_poll_time: Optional[float] = None


def poll_account(
    username: str,
    account_id: int,
    conn,
    proxy_url: Optional[str] = None,
) -> bool:
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(
            STATS_URL,
            params={"player": username},
            headers=HEADERS,
            proxies=proxies,
            timeout=15,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        with _poll_lock:
            _db.insert_snapshot(conn, account_id, resp.text)
        return True
    except Exception:
        return False


def poll_all(conn) -> None:
    rows = conn.execute("SELECT id, username, proxy_url FROM accounts").fetchall()
    for row in rows:
        proxy = None
        if row["proxy_url"]:
            try:
                from flask import current_app
                fernet = current_app.config.get("FERNET")
                if fernet:
                    from .crypto import decrypt
                    proxy = decrypt(fernet, row["proxy_url"])
            except Exception:
                pass
        poll_account(row["username"], row["id"], conn, proxy)
        time.sleep(1)


def start_poll_loop(conn, interval: int = 3600) -> threading.Thread:
    def loop():
        global _last_poll_time
        while True:
            try:
                poll_all(conn)
                _last_poll_time = time.time()
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def poll_status(interval: int = 3600) -> dict:
    if _last_poll_time is None:
        return {"last_poll": None, "next_poll_secs": None}
    elapsed = time.time() - _last_poll_time
    remaining = max(0, interval - elapsed)
    mins = int(remaining // 60)
    secs = int(remaining % 60)
    return {
        "last_poll": time.strftime("%H:%M:%S", time.localtime(_last_poll_time)),
        "next_poll_secs": int(remaining),
        "next_poll_label": f"{mins}m {secs}s",
    }
