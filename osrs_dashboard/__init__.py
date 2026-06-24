import os
import secrets
from flask import Flask, redirect, url_for
from .db import open_db, get_or_create_kdf_salt
from .hiscores import start_poll_loop
from .bank_watcher import start_bank_watcher
from .chart_utils import fmt_gp


def _print_capabilities() -> None:
    from .screenshotter import detect_capabilities
    cap = detect_capabilities()

    tick  = "\033[32m✓\033[0m"
    cross = "\033[31m✗\033[0m"
    warn  = "\033[33m⚠\033[0m"

    bg    = tick if cap["capture_bg_safe"] else warn
    ocr_ok = not cap["ocr"].startswith("unavailable")
    win_ok = cap["window_listing"] != "unavailable"

    print()
    print("\033[1;33m── OSRS Dashboard — Bank Screenshot ──────────────────\033[0m")
    print(f"  Window listing  : {tick if win_ok else cross} {cap['window_listing']}")
    print(f"  Capture method  : {bg} {cap['capture_method']}")
    print(f"  OCR             : {tick if ocr_ok else cross} {cap['ocr']}")
    if cap["warnings"]:
        print()
        for w in cap["warnings"]:
            print(f"  \033[33m⚠\033[0m  {w}")
    print("\033[1;33m──────────────────────────────────────────────────────\033[0m")
    print()


def create_app(db_path: str = "~/.osrs_dashboard.db", poll_interval: int = 3600) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.jinja_env.filters["fmt_gp"] = fmt_gp
    app.config["DB_PATH"] = db_path
    app.config["POLL_INTERVAL"] = poll_interval
    app.config["FERNET"] = None

    _print_capabilities()

    conn = open_db(db_path)
    app.config["DB_CONN"] = conn
    app.config["KDF_SALT"] = get_or_create_kdf_salt(conn)

    start_poll_loop(conn, poll_interval)
    start_bank_watcher(conn)

    from .routes.accounts import accounts_bp
    from .routes.expenses import expenses_bp
    from .routes.hiscores_routes import hiscores_bp
    from .routes.overview import overview_bp
    from .routes.p2p_logs import p2p_bp
    from .routes.bank import bank_bp

    app.register_blueprint(accounts_bp)
    app.register_blueprint(expenses_bp)
    app.register_blueprint(hiscores_bp)
    app.register_blueprint(overview_bp)
    app.register_blueprint(p2p_bp)
    app.register_blueprint(bank_bp)

    @app.before_request
    def require_unlock():
        from flask import request
        if request.endpoint in ("accounts.unlock", "accounts.unlock_post", "static"):
            return
        if app.config["FERNET"] is None:
            return redirect(url_for("accounts.unlock"))

    return app
