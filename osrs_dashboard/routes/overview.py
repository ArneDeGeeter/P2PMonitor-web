from flask import Blueprint, render_template, current_app
from ..db import get_financial_overview

overview_bp = Blueprint("overview", __name__)


@overview_bp.get("/overview")
def financial_overview():
    data = get_financial_overview(current_app.config["DB_CONN"])
    return render_template("overview.html", data=data)
