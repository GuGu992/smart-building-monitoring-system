import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import abort, current_app, g, redirect, request, session, url_for

from .db import get_db


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def login_required(view):
    @wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        return view(**kwargs)
    return wrapped


def role_required(role):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(**kwargs):
            if g.user["role"] != role:
                abort(403)
            return view(**kwargs)
        return wrapped
    return decorator


def status_class(status):
    return {
        "Normal": "success", "Completed": "success", "Approved": "success", "Fixed": "success", "Available": "success", "Active": "danger",
        "In Progress": "primary", "Busy": "primary", "Pending": "warning", "Warning": "warning", "Open": "warning",
        "Critical": "danger", "Error": "danger", "Rejected": "danger", "Offline": "secondary", "Inactive": "secondary",
        "Cleared": "secondary", "Off Duty": "secondary",
    }.get(status, "secondary")


def local_time(value, fmt="%d %b %Y, %H:%M"):
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            local_zone = ZoneInfo(current_app.config["APP_TIMEZONE"])
        except ZoneInfoNotFoundError:
            # Myanmar has used UTC+06:30 year-round since 1945. This keeps the
            # local prototype portable on Windows installations without tzdata.
            local_zone = timezone(timedelta(hours=6, minutes=30))
        return parsed.astimezone(local_zone).strftime(fmt)
    except (ValueError, TypeError):
        return str(value)


def init_security(app):
    @app.before_request
    def load_user_and_check_csrf():
        user_id = session.get("user_id")
        g.user = None
        if user_id:
            g.user = get_db().execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
            if g.user is None:
                session.clear()
            else:
                now = datetime.now(timezone.utc)
                try:
                    last_touch = datetime.fromisoformat(session.get("last_active_touch", "").replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    last_touch = None
                if last_touch is None or now - last_touch >= timedelta(minutes=5):
                    stamp = now.isoformat()
                    database = get_db()
                    database.execute("UPDATE users SET last_active_at=? WHERE id=?", (stamp, user_id))
                    database.commit()
                    session["last_active_touch"] = stamp
        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                abort(400, description="The form expired. Please refresh and try again.")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.context_processor
    def shared_context():
        notifications = []
        unread = 0
        if g.user:
            database = get_db()
            notifications = database.execute(
                "SELECT * FROM notifications WHERE recipient_user_id=? ORDER BY created_at DESC LIMIT 5", (g.user["id"],)
            ).fetchall()
            unread = database.execute(
                "SELECT COUNT(*) FROM notifications WHERE recipient_user_id=? AND read_at IS NULL", (g.user["id"],)
            ).fetchone()[0]
        return {"csrf_token": csrf_token, "notifications": notifications, "unread_notifications": unread}

    app.jinja_env.filters["status_class"] = status_class
    app.jinja_env.filters["localtime"] = local_time
