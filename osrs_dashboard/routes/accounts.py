from flask import (
    Blueprint, render_template, request, redirect, url_for,
    current_app, flash, jsonify,
)
from cryptography.fernet import InvalidToken
from ..crypto import derive_key, make_fernet
from ..db import (
    list_accounts, get_account, get_account_secrets,
    insert_account, update_account, delete_account,
    set_account_state, get_latest_snapshots, SKILLS,
)
from ..totp import get_current_code, seconds_remaining

accounts_bp = Blueprint("accounts", __name__)


def _conn():
    return current_app.config["DB_CONN"]


def _fernet():
    return current_app.config["FERNET"]


@accounts_bp.get("/unlock")
def unlock():
    return render_template("unlock.html")


@accounts_bp.post("/unlock")
def unlock_post():
    password = request.form.get("password", "")
    if not password:
        flash("Password required.", "danger")
        return render_template("unlock.html")
    salt = current_app.config["KDF_SALT"]
    key = derive_key(password, salt)
    fernet = make_fernet(key)

    conn = _conn()
    row = conn.execute(
        "SELECT login_email FROM accounts LIMIT 1"
    ).fetchone()

    if row and row["login_email"]:
        try:
            from ..crypto import decrypt
            decrypt(fernet, row["login_email"])
        except (InvalidToken, Exception):
            flash("Wrong password.", "danger")
            return render_template("unlock.html")

    current_app.config["FERNET"] = fernet
    return redirect(url_for("accounts.dashboard"))


@accounts_bp.get("/")
def dashboard():
    accounts = list_accounts(_conn())
    return render_template("dashboard.html", accounts=accounts)


@accounts_bp.get("/accounts/add")
def add_account():
    return render_template("add_account.html", account=None)


@accounts_bp.post("/accounts/add")
def add_account_post():
    f = request.form
    insert_account(
        _conn(), _fernet(),
        username=f["username"].strip(),
        login_email=f.get("login_email", "").strip() or None,
        password=f.get("password", "").strip() or None,
        bank_pin=f.get("bank_pin", "").strip() or None,
        totp_secret=f.get("totp_secret", "").strip() or None,
        proxy_url=f.get("proxy_url", "").strip() or None,
        state=f.get("state", "running"),
        notes=f.get("notes", "").strip() or None,
    )
    flash("Account added.", "success")
    return redirect(url_for("accounts.dashboard"))


@accounts_bp.get("/accounts/<int:account_id>")
def account_detail(account_id: int):
    conn = _conn()
    row = get_account(conn, account_id)
    if not row:
        flash("Account not found.", "danger")
        return redirect(url_for("accounts.dashboard"))

    snaps = get_latest_snapshots(conn, account_id, limit=2)
    latest = snaps[0] if snaps else None
    prev = snaps[1] if len(snaps) > 1 else None

    from ..db import compute_deltas
    deltas = compute_deltas(prev, latest) if (prev and latest) else {}

    from ..db import list_expenses
    expenses = list_expenses(conn, account_id)

    total_gbp = sum(e.amount for e in expenses if e.currency == "GBP")
    total_usd = sum(e.amount for e in expenses if e.currency == "USD")
    total_eur = sum(e.amount for e in expenses if e.currency == "EUR")
    total_gp = sum(e.amount for e in expenses if e.currency == "GP")

    return render_template(
        "account.html",
        row=row,
        account_id=account_id,
        latest=latest,
        deltas=deltas,
        expenses=expenses,
        total_gbp=total_gbp,
        total_usd=total_usd,
        total_eur=total_eur,
        total_gp=total_gp,
        skills=SKILLS,
    )


@accounts_bp.get("/accounts/<int:account_id>/edit")
def edit_account(account_id: int):
    secrets_dict = get_account_secrets(_conn(), _fernet(), account_id)
    if not secrets_dict:
        flash("Account not found.", "danger")
        return redirect(url_for("accounts.dashboard"))
    row = get_account(_conn(), account_id)
    return render_template("add_account.html", account=secrets_dict, account_id=account_id, row=row)


@accounts_bp.post("/accounts/<int:account_id>/edit")
def edit_account_post(account_id: int):
    f = request.form
    update_account(
        _conn(), _fernet(), account_id,
        username=f["username"].strip(),
        login_email=f.get("login_email", "").strip() or None,
        password=f.get("password", "").strip() or None,
        bank_pin=f.get("bank_pin", "").strip() or None,
        totp_secret=f.get("totp_secret", "").strip() or None,
        proxy_url=f.get("proxy_url", "").strip() or None,
        state=f.get("state", "running"),
        notes=f.get("notes", "").strip() or None,
    )
    flash("Account updated.", "success")
    return redirect(url_for("accounts.account_detail", account_id=account_id))


@accounts_bp.post("/accounts/<int:account_id>/state")
def set_state(account_id: int):
    state = request.form.get("state", "running")
    set_account_state(_conn(), account_id, state)
    return redirect(url_for("accounts.account_detail", account_id=account_id))


@accounts_bp.post("/accounts/<int:account_id>/delete")
def delete_account_route(account_id: int):
    delete_account(_conn(), account_id)
    flash("Account deleted.", "success")
    return redirect(url_for("accounts.dashboard"))


@accounts_bp.get("/accounts/<int:account_id>/credentials")
def credentials(account_id: int):
    secrets_dict = get_account_secrets(_conn(), _fernet(), account_id)
    totp_code = None
    totp_secs = None
    if secrets_dict.get("totp_secret"):
        try:
            totp_code = get_current_code(secrets_dict["totp_secret"])
            totp_secs = seconds_remaining()
        except Exception:
            pass
    secrets_dict["totp_code"] = totp_code
    secrets_dict["totp_secs"] = totp_secs
    return jsonify(secrets_dict)


@accounts_bp.get("/accounts/<int:account_id>/totp")
def totp_code(account_id: int):
    secrets_dict = get_account_secrets(_conn(), _fernet(), account_id)
    secret = secrets_dict.get("totp_secret")
    if not secret:
        return jsonify({"code": None, "seconds_remaining": None})
    try:
        code = get_current_code(secret)
        secs = seconds_remaining()
    except Exception:
        return jsonify({"code": "error", "seconds_remaining": 0})
    return jsonify({"code": code, "seconds_remaining": secs})
