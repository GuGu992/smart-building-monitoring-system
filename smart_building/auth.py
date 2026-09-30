import re

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_db
from .services import utc_now


bp = Blueprint("auth", __name__)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user:
        return redirect(url_for("core.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("The username or password is incorrect.", "danger")
        elif not user["active"]:
            flash("This account is inactive. Please contact an administrator.", "warning")
        else:
            active_at = utc_now()
            database = get_db()
            database.execute("UPDATE users SET last_active_at=? WHERE id=?", (active_at, user["id"]))
            database.commit()
            session.clear()
            session["user_id"] = user["id"]
            session["last_active_touch"] = active_at
            next_url = request.args.get("next", "")
            if next_url.startswith("/") and not next_url.startswith("//") and not re.match(r"^/[\\/]", next_url):
                return redirect(next_url)
            return redirect(url_for("core.dashboard"))
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
