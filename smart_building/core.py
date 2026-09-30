import sqlite3

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, transaction
from .security import login_required, role_required
from .services import PRIORITIES, WorkflowError, add_work_note, create_approval, ensure_automatic_readings, environmental_snapshot, start_maintenance, submit_completion, utc_now


bp = Blueprint("core", __name__)


def _int_arg(name, default=None):
    try:
        return int(request.args.get(name, default)) if request.args.get(name, default) not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _dashboard_environment(database, building_id):
    if building_id:
        return database.execute("SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1", (building_id,)).fetchone()
    return database.execute(
        """WITH latest AS (
               SELECT r.* FROM environmental_readings r
               JOIN (SELECT building_id, MAX(recorded_at) stamp FROM environmental_readings GROUP BY building_id) x
                 ON x.building_id=r.building_id AND x.stamp=r.recorded_at
           ) SELECT AVG(temperature) temperature, AVG(humidity) humidity, MAX(air_quality) air_quality,
                    SUM(energy_usage) energy_usage, MAX(recorded_at) recorded_at FROM latest"""
    ).fetchone()


def _dashboard_environment_conditions(database, building_id, environment):
    if not environment:
        return {}
    if building_id:
        return environmental_snapshot(database, building_id, environment)
    snapshots = [
        environmental_snapshot(database, row["id"])
        for row in database.execute("SELECT id FROM buildings WHERE active=1 ORDER BY id")
    ]
    priority = {"Normal": 0, "Fixed": 0, "Warning": 1, "Error": 2, "Offline": 2, "Critical": 3}
    combined = {}
    for metric in ("temperature", "humidity", "air_quality", "energy_usage"):
        choices = [snapshot[metric] for snapshot in snapshots if metric in snapshot]
        if not choices:
            continue
        worst = max(choices, key=lambda item: priority.get(item["condition"], 0)).copy()
        worst["value"] = environment[metric]
        combined[metric] = worst
    return combined


@bp.route("/")
@login_required
def dashboard():
    database = get_db()
    with transaction() as database:
        ensure_automatic_readings(
            database,
            interval_seconds=current_app.config["ENVIRONMENT_READING_INTERVAL_SECONDS"],
        )
    buildings = database.execute("SELECT * FROM buildings WHERE active=1 ORDER BY name").fetchall()
    building_id = _int_arg("building_id")
    if building_id and not any(row["id"] == building_id for row in buildings):
        building_id = None
    equipment_where = "e.active=1" + (" AND e.building_id=?" if building_id else "")
    params = (building_id,) if building_id else ()
    counts = database.execute(
        f"SELECT COUNT(*) total, SUM(status='Normal') normal, SUM(status='Critical') critical, SUM(status='In Progress') in_progress FROM equipment e WHERE {equipment_where}", params
    ).fetchone()
    alert_where = "a.condition='Active'" + (" AND a.building_id=?" if building_id else "")
    alerts = database.execute(
        f"SELECT a.*,b.name building_name FROM alerts a JOIN buildings b ON b.id=a.building_id WHERE {alert_where} ORDER BY a.created_at DESC LIMIT 3", params
    ).fetchall()
    active_alert_count = database.execute(
        f"SELECT COUNT(*) FROM alerts a WHERE {alert_where}", params
    ).fetchone()[0]
    if g.user["role"] == "admin":
        pending_count = database.execute("SELECT COUNT(*) FROM approval_requests WHERE status='Pending'").fetchone()[0]
        recent_maintenance = database.execute(
            """SELECT m.*,COALESCE(e.equipment_code,s.sensor_code) asset_code,COALESCE(e.name,s.sensor_type) asset_name,
                      b.name building_name,COALESCE(e.location,s.location) asset_location
               FROM maintenance_requests m LEFT JOIN equipment e ON e.id=m.equipment_id
               LEFT JOIN environmental_sensors s ON s.id=m.sensor_id
               JOIN buildings b ON b.id=COALESCE(e.building_id,s.building_id)
               WHERE (? IS NULL OR b.id=?) ORDER BY m.created_at DESC""", (building_id, building_id)
        ).fetchall()
        activities = database.execute(
            "SELECT l.*,u.display_name FROM activity_logs l LEFT JOIN users u ON u.id=l.user_id ORDER BY l.created_at DESC LIMIT 3"
        ).fetchall()
        cards = {"pending": pending_count, "active_alerts": active_alert_count}
    else:
        cards = {
            "assigned": database.execute("SELECT COUNT(*) FROM maintenance_requests WHERE assigned_staff_id=? AND status!='Completed'", (g.user["id"],)).fetchone()[0],
            "in_progress": database.execute("SELECT COUNT(*) FROM maintenance_requests WHERE assigned_staff_id=? AND status='In Progress'", (g.user["id"],)).fetchone()[0],
            "pending": database.execute("SELECT COUNT(*) FROM approval_requests WHERE requested_by_user_id=? AND status='Pending'", (g.user["id"],)).fetchone()[0],
            "active_alerts": active_alert_count,
        }
        recent_maintenance = database.execute(
            """SELECT m.*,COALESCE(e.equipment_code,s.sensor_code) asset_code,COALESCE(e.name,s.sensor_type) asset_name,
                      b.name building_name,COALESCE(e.location,s.location) asset_location
               FROM maintenance_requests m LEFT JOIN equipment e ON e.id=m.equipment_id
               LEFT JOIN environmental_sensors s ON s.id=m.sensor_id JOIN buildings b ON b.id=COALESCE(e.building_id,s.building_id)
               WHERE m.assigned_staff_id=? ORDER BY m.created_at DESC LIMIT 3""", (g.user["id"],)
        ).fetchall()
        activities = []
    my_requests = database.execute(
        """SELECT a.*,COALESCE(e.equipment_code,s.sensor_code,m.request_code) asset_code
           FROM approval_requests a LEFT JOIN equipment e ON e.id=a.equipment_id LEFT JOIN environmental_sensors s ON s.id=a.sensor_id
           LEFT JOIN maintenance_requests m ON m.id=a.maintenance_id
           WHERE a.requested_by_user_id=? ORDER BY a.submitted_at DESC LIMIT 3""", (g.user["id"],)
    ).fetchall() if g.user["role"] == "staff" else []
    environment = _dashboard_environment(database, building_id)
    return render_template(
        "dashboard.html", page_title="Dashboard", buildings=buildings, building_id=building_id, counts=counts,
        environment=environment, environment_conditions=_dashboard_environment_conditions(database, building_id, environment), alerts=alerts, recent_maintenance=recent_maintenance,
        activities=activities, cards=cards, my_requests=my_requests,
    )


@bp.route("/equipment")
@login_required
def equipment_list():
    database = get_db()
    buildings = database.execute("SELECT * FROM buildings WHERE active=1 ORDER BY name").fetchall()
    building_id, status, query = _int_arg("building_id"), request.args.get("status", ""), request.args.get("q", "").strip()
    sql = """SELECT e.*,b.name building_name FROM equipment e JOIN buildings b ON b.id=e.building_id WHERE e.active=1"""
    params = []
    if building_id: sql += " AND e.building_id=?"; params.append(building_id)
    if status in ("Normal", "Critical", "In Progress"): sql += " AND e.status=?"; params.append(status)
    if query: sql += " AND (e.equipment_code LIKE ? OR e.name LIKE ?)"; params.extend((f"%{query}%", f"%{query}%"))
    rows = database.execute(sql + " ORDER BY e.equipment_code", params).fetchall()
    return render_template("equipment_list.html", page_title="Equipment", equipment=rows, buildings=buildings, building_id=building_id, status=status, query=query)


@bp.route("/equipment/<int:equipment_id>")
@login_required
def equipment_detail(equipment_id):
    database = get_db()
    equipment = database.execute("SELECT e.*,b.name building_name,b.location building_location FROM equipment e JOIN buildings b ON b.id=e.building_id WHERE e.id=?", (equipment_id,)).fetchone()
    if not equipment: abort(404)
    maintenance = database.execute(
        """SELECT m.*,u.display_name staff_name FROM maintenance_requests m LEFT JOIN users u ON u.id=m.assigned_staff_id
           WHERE m.equipment_id=? ORDER BY m.created_at DESC""", (equipment_id,)
    ).fetchall()
    pending_request = database.execute(
        "SELECT 1 FROM approval_requests WHERE requested_by_user_id=? AND equipment_id=? AND status='Pending'", (g.user["id"], equipment_id)
    ).fetchone()
    return render_template("equipment_detail.html", page_title=equipment["equipment_code"], equipment=equipment, maintenance=maintenance, pending_request=pending_request)


@bp.post("/equipment/<int:equipment_id>/request-maintenance")
@role_required("staff")
def request_equipment_maintenance(equipment_id):
    priority, reason = request.form.get("priority"), request.form.get("reason", "")
    try:
        with transaction() as database:
            if priority not in PRIORITIES: raise WorkflowError("Select a valid priority.")
            if database.execute("SELECT 1 FROM maintenance_requests WHERE equipment_id=? AND status IN ('Open','In Progress')", (equipment_id,)).fetchone():
                raise WorkflowError("This equipment already has active maintenance.")
            create_approval(database, user_id=g.user["id"], request_type="Create Maintenance", equipment_id=equipment_id, priority=priority, reason=reason)
        flash("Maintenance request submitted for Admin approval.", "success")
    except (WorkflowError, sqlite3.IntegrityError) as error:
        flash(str(error), "danger")
    return redirect(url_for("core.equipment_detail", equipment_id=equipment_id))


@bp.post("/equipment/<int:equipment_id>/request-status")
@role_required("staff")
def request_equipment_status(equipment_id):
    status, reason = request.form.get("requested_status"), request.form.get("reason", "")
    try:
        with transaction() as database:
            if status not in ("Normal", "Critical"): raise WorkflowError("Select Normal or Critical.")
            create_approval(database, user_id=g.user["id"], request_type="Equipment Status Change", equipment_id=equipment_id, requested_status=status, reason=reason)
        flash("Status change submitted for Admin approval.", "success")
    except (WorkflowError, sqlite3.IntegrityError) as error:
        flash(str(error), "danger")
    return redirect(url_for("core.equipment_detail", equipment_id=equipment_id))


@bp.route("/maintenance")
@login_required
def maintenance_list():
    database = get_db()
    buildings = database.execute("SELECT * FROM buildings WHERE active=1 ORDER BY name").fetchall()
    building_id, status, priority, query = _int_arg("building_id"), request.args.get("status", ""), request.args.get("priority", ""), request.args.get("q", "").strip()
    sql = """SELECT m.*,COALESCE(e.equipment_code,s.sensor_code) asset_code,COALESCE(e.name,s.sensor_type) asset_name,
                    b.name building_name,COALESCE(e.location,s.location) asset_location,u.display_name staff_name
             FROM maintenance_requests m LEFT JOIN equipment e ON e.id=m.equipment_id LEFT JOIN environmental_sensors s ON s.id=m.sensor_id
             JOIN buildings b ON b.id=COALESCE(e.building_id,s.building_id) LEFT JOIN users u ON u.id=m.assigned_staff_id WHERE 1=1"""
    params = []
    if g.user["role"] == "staff": sql += " AND m.assigned_staff_id=?"; params.append(g.user["id"])
    if building_id: sql += " AND b.id=?"; params.append(building_id)
    if status in ("Open", "In Progress", "Completed"): sql += " AND m.status=?"; params.append(status)
    if priority in PRIORITIES: sql += " AND m.priority=?"; params.append(priority)
    if query: sql += " AND (m.request_code LIKE ? OR COALESCE(e.equipment_code,s.sensor_code) LIKE ? OR m.issue LIKE ?)"; params.extend((f"%{query}%",)*3)
    rows = database.execute(sql + " ORDER BY m.created_at DESC", params).fetchall()
    return render_template("maintenance_list.html", page_title="Maintenance", maintenance=rows, buildings=buildings, building_id=building_id, status=status, priority=priority, query=query, priorities=PRIORITIES)


@bp.route("/maintenance/<int:maintenance_id>")
@login_required
def maintenance_detail(maintenance_id):
    database = get_db()
    maintenance = database.execute(
        """SELECT m.*,COALESCE(e.equipment_code,s.sensor_code) asset_code,COALESCE(e.name,s.sensor_type) asset_name,
                  b.name building_name,COALESCE(e.location,s.location) asset_location,u.display_name staff_name
           FROM maintenance_requests m LEFT JOIN equipment e ON e.id=m.equipment_id LEFT JOIN environmental_sensors s ON s.id=m.sensor_id
           JOIN buildings b ON b.id=COALESCE(e.building_id,s.building_id) LEFT JOIN users u ON u.id=m.assigned_staff_id WHERE m.id=?""", (maintenance_id,)
    ).fetchone()
    if not maintenance: abort(404)
    if g.user["role"] == "staff" and maintenance["assigned_staff_id"] != g.user["id"]: abort(403)
    updates = database.execute(
        "SELECT x.*,u.display_name FROM maintenance_updates x JOIN users u ON u.id=x.user_id WHERE x.maintenance_id=? ORDER BY x.created_at", (maintenance_id,)
    ).fetchall()
    staff = database.execute("SELECT id,staff_code,display_name FROM users WHERE role='staff' AND active=1 ORDER BY display_name").fetchall()
    pending_completion = database.execute("SELECT 1 FROM approval_requests WHERE maintenance_id=? AND status='Pending'", (maintenance_id,)).fetchone()
    return render_template("maintenance_detail.html", page_title=maintenance["request_code"], maintenance=maintenance, updates=updates, staff=staff, priorities=PRIORITIES, pending_completion=pending_completion)


@bp.post("/maintenance/<int:maintenance_id>/start")
@role_required("staff")
def maintenance_start(maintenance_id):
    try:
        with transaction() as database: start_maintenance(database, maintenance_id, g.user["id"])
        flash("Maintenance started.", "success")
    except WorkflowError as error: flash(str(error), "danger")
    return redirect(url_for("core.maintenance_detail", maintenance_id=maintenance_id))


@bp.post("/maintenance/<int:maintenance_id>/note")
@role_required("staff")
def maintenance_note(maintenance_id):
    try:
        with transaction() as database: add_work_note(database, maintenance_id, g.user["id"], request.form.get("note", ""))
        flash("Work note added.", "success")
    except WorkflowError as error: flash(str(error), "danger")
    return redirect(url_for("core.maintenance_detail", maintenance_id=maintenance_id))


@bp.post("/maintenance/<int:maintenance_id>/complete")
@role_required("staff")
def maintenance_complete(maintenance_id):
    try:
        with transaction() as database: submit_completion(database, maintenance_id, g.user["id"], request.form.get("note", ""))
        flash("Completion submitted for Admin approval.", "success")
    except (WorkflowError, sqlite3.IntegrityError) as error: flash(str(error), "danger")
    return redirect(url_for("core.maintenance_detail", maintenance_id=maintenance_id))


@bp.route("/environment")
@login_required
def environment():
    database = get_db()
    buildings = database.execute("SELECT * FROM buildings WHERE active=1 ORDER BY name").fetchall()
    building_id = _int_arg("building_id", buildings[0]["id"] if buildings else None)
    if not any(row["id"] == building_id for row in buildings): building_id = buildings[0]["id"] if buildings else None
    if building_id:
        with transaction() as database:
            ensure_automatic_readings(
                database,
                building_id,
                current_app.config["ENVIRONMENT_READING_INTERVAL_SECONDS"],
            )
    current = database.execute("SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1", (building_id,)).fetchone() if building_id else None
    readings = database.execute("SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 24", (building_id,)).fetchall() if building_id else []
    conditions = environmental_snapshot(database, building_id, current) if building_id else {}
    sensor_rows = [conditions[key] for key in ("temperature", "humidity", "air_quality", "energy_usage") if key in conditions]
    for item in sensor_rows:
        item["maintenance"] = database.execute(
            "SELECT * FROM maintenance_requests WHERE sensor_id=? AND status!='Completed' ORDER BY created_at DESC LIMIT 1",
            (item["sensor"]["id"],),
        ).fetchone() if item["sensor"] else None
    problems = [item for item in sensor_rows if item["problem"]][:3]
    sensor_counts = {
        "total": len(sensor_rows),
        "normal": sum(not item["problem"] for item in sensor_rows),
        "problem": sum(item["condition"] in ("Warning", "Critical", "Error") for item in sensor_rows),
        "offline": sum(item["condition"] == "Offline" for item in sensor_rows),
    }
    return render_template(
        "environment.html", page_title="Environment", buildings=buildings, building_id=building_id,
        current=current, readings=list(reversed(readings)), conditions=conditions, sensor_rows=sensor_rows,
        problems=problems, sensor_counts=sensor_counts,
    )


@bp.route("/sensors/<int:sensor_id>")
@login_required
def sensor_detail(sensor_id):
    database = get_db()
    sensor = database.execute(
        "SELECT s.*,b.name building_name FROM environmental_sensors s JOIN buildings b ON b.id=s.building_id WHERE s.id=?",
        (sensor_id,),
    ).fetchone()
    if not sensor:
        abort(404)
    current = database.execute(
        "SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1", (sensor["building_id"],)
    ).fetchone()
    conditions = environmental_snapshot(database, sensor["building_id"], current)
    metric = next((item for item in conditions.values() if item["sensor"] and item["sensor"]["id"] == sensor_id), None)
    if not metric:
        abort(404)
    maintenance = database.execute(
        "SELECT * FROM maintenance_requests WHERE sensor_id=? AND status!='Completed' ORDER BY created_at DESC LIMIT 1", (sensor_id,)
    ).fetchone()
    readings = database.execute(
        "SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 12", (sensor["building_id"],)
    ).fetchall()
    return render_template(
        "sensor_detail.html", page_title=sensor["sensor_code"], sensor=sensor, metric=metric,
        maintenance=maintenance, readings=readings,
    )


@bp.post("/sensors/<int:sensor_id>/request-maintenance")
@role_required("staff")
def sensor_request_maintenance(sensor_id):
    try:
        priority = request.form.get("priority")
        if priority not in PRIORITIES: raise WorkflowError("Select a valid priority.")
        with transaction() as database:
            create_approval(database, user_id=g.user["id"], request_type="Create Maintenance", sensor_id=sensor_id, priority=priority, reason=request.form.get("reason", ""))
        flash("Sensor maintenance request submitted.", "success")
    except (WorkflowError, sqlite3.IntegrityError) as error: flash(str(error), "danger")
    return redirect(url_for("core.environment", building_id=request.form.get("building_id")))


@bp.route("/alerts")
@login_required
def alerts():
    database = get_db()
    buildings = database.execute("SELECT * FROM buildings WHERE active=1 ORDER BY name").fetchall()
    building_id = _int_arg("building_id")
    severity = request.args.get("severity", "")
    condition = request.args.get("condition")
    alert_type = request.args.get("alert_type", "")
    if condition is None or condition not in ("", "Active", "Cleared"):
        condition = "Active"
    sql = "SELECT a.*,b.name building_name FROM alerts a JOIN buildings b ON b.id=a.building_id WHERE 1=1"; params=[]
    if building_id: sql += " AND a.building_id=?"; params.append(building_id)
    if severity in ("Warning", "Critical"): sql += " AND a.severity=?"; params.append(severity)
    if condition in ("Active", "Cleared"): sql += " AND a.condition=?"; params.append(condition)
    if alert_type: sql += " AND a.alert_type=?"; params.append(alert_type)
    rows = database.execute(sql + " ORDER BY a.created_at DESC", params).fetchall()
    types = database.execute("SELECT DISTINCT alert_type FROM alerts ORDER BY alert_type").fetchall()
    return render_template("alerts.html", page_title="Alerts", alerts=rows, buildings=buildings, types=types, building_id=building_id, severity=severity, condition=condition, alert_type=alert_type)


@bp.route("/alerts/<int:alert_id>")
@login_required
def alert_detail(alert_id):
    database = get_db()
    alert = database.execute(
        """SELECT a.*,b.name building_name,e.equipment_code,e.name equipment_name,s.sensor_code,s.sensor_type
           FROM alerts a JOIN buildings b ON b.id=a.building_id LEFT JOIN equipment e ON e.id=a.equipment_id
           LEFT JOIN environmental_sensors s ON s.id=a.sensor_id WHERE a.id=?""", (alert_id,)
    ).fetchone()
    if not alert: abort(404)
    if g.user["role"] == "admin" and not alert["admin_seen"]:
        database.execute("UPDATE alerts SET admin_seen=1,updated_at=? WHERE id=?", (utc_now(), alert_id)); database.commit()
    return render_template("alert_detail.html", page_title=alert["alert_code"], alert=alert)


@bp.route("/my-requests")
@role_required("staff")
def my_requests():
    rows = get_db().execute(
        """SELECT a.*,COALESCE(e.equipment_code,s.sensor_code,m.request_code) asset_code
           FROM approval_requests a LEFT JOIN equipment e ON e.id=a.equipment_id LEFT JOIN environmental_sensors s ON s.id=a.sensor_id
           LEFT JOIN maintenance_requests m ON m.id=a.maintenance_id WHERE a.requested_by_user_id=? ORDER BY a.submitted_at DESC""", (g.user["id"],)
    ).fetchall()
    return render_template("my_requests.html", page_title="My Requests", approvals=rows)


@bp.route("/notifications/<int:notification_id>")
@login_required
def notification_open(notification_id):
    database = get_db()
    notice = database.execute("SELECT * FROM notifications WHERE id=? AND recipient_user_id=?", (notification_id, g.user["id"])).fetchone()
    if not notice: abort(404)
    database.execute("UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=?", (utc_now(), notification_id)); database.commit()
    endpoints = {"alert": ("core.alert_detail", "alert_id"), "maintenance": ("core.maintenance_detail", "maintenance_id"), "approval": (("admin.approval_detail" if g.user["role"] == "admin" else "core.my_requests"), "approval_id")}
    endpoint, arg = endpoints.get(notice["entity_type"], ("core.dashboard", None))
    return redirect(url_for(endpoint, **({arg: notice["entity_id"]} if arg and endpoint != "core.my_requests" else {})))


@bp.route("/settings", methods=("GET", "POST"))
@login_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            with transaction() as database:
                if action == "profile":
                    name = request.form.get("display_name", "").strip()
                    if not name: raise WorkflowError("Display name is required.")
                    if g.user["role"] == "admin":
                        username = request.form.get("username", "").strip()
                        if not username: raise WorkflowError("Username is required.")
                        database.execute("UPDATE users SET display_name=?,username=?,updated_at=? WHERE id=?", (name, username, utc_now(), g.user["id"]))
                    else:
                        database.execute("UPDATE users SET display_name=?,updated_at=? WHERE id=?", (name, utc_now(), g.user["id"]))
                elif action == "password":
                    if not check_password_hash(g.user["password_hash"], request.form.get("current_password", "")): raise WorkflowError("Current password is incorrect.")
                    new = request.form.get("new_password", ""); confirm = request.form.get("confirm_password", "")
                    if len(new) < 8: raise WorkflowError("New password must contain at least 8 characters.")
                    if new != confirm: raise WorkflowError("New passwords do not match.")
                    database.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (generate_password_hash(new), utc_now(), g.user["id"]))
                elif action == "work_status":
                    if g.user["role"] != "staff": raise WorkflowError("Only Staff can change a work status.")
                    work_status = request.form.get("work_status", "")
                    if work_status not in ("Available", "Busy", "Off Duty"): raise WorkflowError("Select a valid work status.")
                    database.execute("UPDATE users SET work_status=?,updated_at=? WHERE id=?", (work_status, utc_now(), g.user["id"]))
                else: raise WorkflowError("Unknown settings action.")
            flash("Settings saved.", "success")
        except (WorkflowError, sqlite3.IntegrityError) as error: flash("That username is already used." if isinstance(error, sqlite3.IntegrityError) else str(error), "danger")
        return redirect(url_for("core.settings"))
    return render_template("settings.html", page_title="Settings")
