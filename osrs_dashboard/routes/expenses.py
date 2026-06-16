from flask import Blueprint, request, redirect, url_for, current_app, flash
from ..db import insert_expense, delete_expense

expenses_bp = Blueprint("expenses", __name__)


def _conn():
    return current_app.config["DB_CONN"]


@expenses_bp.post("/accounts/<int:account_id>/expenses")
def add_expense(account_id: int):
    f = request.form
    try:
        amount = float(f["amount"])
    except (ValueError, KeyError):
        flash("Invalid amount.", "danger")
        return redirect(url_for("accounts.account_detail", account_id=account_id) + "#expenses")

    insert_expense(
        _conn(),
        account_id=account_id,
        category=f.get("category", "misc"),
        currency=f.get("currency", "GBP"),
        amount=amount,
        date_of=f.get("date_of", ""),
        type=f.get("type", "expense"),
        notes=f.get("notes", "").strip() or None,
    )
    flash("Expense added.", "success")
    return redirect(url_for("accounts.account_detail", account_id=account_id) + "#expenses")


@expenses_bp.post("/accounts/<int:account_id>/expenses/<int:expense_id>/delete")
def delete_expense_route(account_id: int, expense_id: int):
    delete_expense(_conn(), expense_id)
    flash("Expense deleted.", "success")
    return redirect(url_for("accounts.account_detail", account_id=account_id) + "#expenses")
