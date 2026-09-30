import csv
import io
import sqlite3
from datetime import date, timedelta

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from .db import get_db, transaction
from .security import role_required
from .services import PRIORITIES, WorkflowError, assign_maintenance, create_maintenance, environmental_snapshot, evaluate_environment, next_code, review_approval, set_equipment_status, utc_now


bp = Blueprint("admin", __name__, url_prefix="/admin")

REPORT_TYPES = ("equipment", "maintenance", "environmental", "activity")
REPORT_COLUMNS = {
    "equipment": ("Equipment ID", "Equipment", "Type", "Building", "Location", "Status", "Last Updated"),
    "maintenance": ("Request ID", "Equipment", "Issue", "Priority", "Status", "Building", "Assigned Staff", "Created Date", "Completed Date"),
    "environmental": ("Date/Time", "Building", "Temperature", "Humidity", "Air Quality", "Energy Usage"),
    "activity": ("Date/Time", "Staff", "Action", "Related Record", "Detail"),
}


def _active_buildings(database):
    return database.execute("SELECT * FROM buildings WHERE active=1 ORDER BY name").fetchall()


@bp.route("/equipment/new", methods=("GET", "POST"))
@role_required("admin")
def equipment_create():
    database = get_db()
    if request.method == "POST":
        try:
            name, kind, location = (request.form.get(key, "").strip() for key in ("name", "type", "location"))
            building_id = int(request.form.get("building_id", 0)); status = request.form.get("status", "Normal")
            if not all((name, kind, location)) or status not in ("Normal", "Critical"): raise WorkflowError("Complete every field and select a valid initial status.")
            with transaction() as database:
                if not database.execute("SELECT 1 FROM buildings WHERE id=? AND active=1", (building_id,)).fetchone(): raise WorkflowError("Select an active building.")
                supplied = request.form.get("equipment_code", "").strip().upper()
                code = supplied or next_code(database, "equipment", "equipment_code", "EQ")
                cursor = database.execute("INSERT INTO equipment(equipment_code,name,type,building_id,location,status) VALUES (?,?,?,?,?,?)", (code,name,kind,building_id,location,"Normal"))
                if status == "Critical": set_equipment_status(database, cursor.lastrowid, "Critical", g.user["id"])
            flash("Equipment added.", "success"); return redirect(url_for("core.equipment_list"))
        except (ValueError, WorkflowError, sqlite3.IntegrityError) as error: flash("Equipment ID must be unique." if isinstance(error, sqlite3.IntegrityError) else str(error), "danger")
    return render_template("equipment_form.html", page_title="Add Equipment", equipment=None, buildings=_active_buildings(database))


@bp.route("/equipment/<int:equipment_id>/edit", methods=("GET", "POST"))
@role_required("admin")
def equipment_edit(equipment_id):
    database = get_db(); equipment = database.execute("SELECT * FROM equipment WHERE id=? AND active=1", (equipment_id,)).fetchone()
    if not equipment: abort(404)
    if request.method == "POST":
        try:
            name, kind, location = (request.form.get(key, "").strip() for key in ("name", "type", "location")); building_id=int(request.form.get("building_id",0))
            if not all((name,kind,location)): raise WorkflowError("Complete every field.")
            with transaction() as tx:
                tx.execute("UPDATE equipment SET name=?,type=?,building_id=?,location=?,updated_at=? WHERE id=?", (name,kind,building_id,location,utc_now(),equipment_id))
            flash("Equipment updated.", "success"); return redirect(url_for("core.equipment_detail", equipment_id=equipment_id))
        except (ValueError,WorkflowError,sqlite3.IntegrityError) as error: flash(str(error), "danger")
    return render_template("equipment_form.html", page_title="Edit Equipment", equipment=equipment, buildings=_active_buildings(database))


@bp.post("/equipment/<int:equipment_id>/remove")
@role_required("admin")
def equipment_remove(equipment_id):
    try:
        with transaction() as database:
            dependency = database.execute("SELECT 1 FROM maintenance_requests WHERE equipment_id=? AND status!='Completed'", (equipment_id,)).fetchone() or database.execute("SELECT 1 FROM approval_requests WHERE equipment_id=? AND status='Pending'", (equipment_id,)).fetchone()
            if dependency: raise WorkflowError("Resolve active maintenance and approvals before removing this equipment.")
            database.execute("UPDATE equipment SET active=0,updated_at=? WHERE id=?", (utc_now(),equipment_id))
            database.execute("UPDATE alerts SET condition='Cleared',cleared_at=?,updated_at=? WHERE equipment_id=? AND condition='Active'",(utc_now(),utc_now(),equipment_id))
        flash("Equipment removed from active use; its history was preserved.", "success")
    except WorkflowError as error: flash(str(error), "danger")
    return redirect(url_for("core.equipment_list"))


@bp.post("/equipment/<int:equipment_id>/fault")
@role_required("admin")
def equipment_fault(equipment_id):
    try:
        with transaction() as database: set_equipment_status(database,equipment_id,"Critical",g.user["id"])
        flash("Fault simulated and alert created.", "success")
    except WorkflowError as error: flash(str(error), "danger")
    return redirect(url_for("core.equipment_detail",equipment_id=equipment_id))


@bp.route("/maintenance/new", methods=("GET","POST"))
@role_required("admin")
def maintenance_create():
    database=get_db()
    if request.method=="POST":
        try:
            target=request.form.get("target",""); target_kind,target_id=target.split(":",1); target_id=int(target_id)
            assigned=int(request.form["assigned_staff_id"]) if request.form.get("assigned_staff_id") else None
            kwargs={"equipment_id":target_id} if target_kind=="equipment" else {"sensor_id":target_id} if target_kind=="sensor" else None
            if kwargs is None: raise WorkflowError("Select equipment or a sensor.")
            with transaction() as tx:
                maintenance_id=create_maintenance(tx,actor_id=g.user["id"],issue=request.form.get("issue",""),priority=request.form.get("priority",""),assigned_staff_id=assigned,notes=request.form.get("notes",""),**kwargs)
            flash("Maintenance created.","success"); return redirect(url_for("core.maintenance_detail",maintenance_id=maintenance_id))
        except (ValueError,WorkflowError,sqlite3.IntegrityError) as error: flash(str(error),"danger")
    equipment=database.execute("SELECT e.*,b.name building_name FROM equipment e JOIN buildings b ON b.id=e.building_id WHERE e.active=1 ORDER BY e.equipment_code").fetchall()
    sensors=database.execute("SELECT s.*,b.name building_name FROM environmental_sensors s JOIN buildings b ON b.id=s.building_id WHERE s.active=1 ORDER BY s.sensor_code").fetchall()
    staff=database.execute("SELECT * FROM users WHERE role='staff' AND active=1 ORDER BY display_name").fetchall()
    prefill_sensor=request.args.get("sensor_id"); prefill_issue=""; prefill_notes=""
    if prefill_sensor:
        try:
            sensor=database.execute("SELECT s.*,b.name building_name FROM environmental_sensors s JOIN buildings b ON b.id=s.building_id WHERE s.id=? AND s.active=1",(int(prefill_sensor),)).fetchone()
        except (TypeError,ValueError):
            sensor=None
        if sensor:
            current=database.execute("SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1",(sensor["building_id"],)).fetchone()
            snapshot=environmental_snapshot(database,sensor["building_id"],current)
            metric=next((item for item in snapshot.values() if item["sensor"] and item["sensor"]["id"]==sensor["id"]),None)
            if metric:
                prefill_issue=f"{metric['label']} sensor: {metric['summary']}"
                current_text="No Reading" if metric["value"] is None else f"{metric['value']:g}{metric['unit']}"
                prefill_notes=f"Sensor: {sensor['sensor_code']} {sensor['sensor_type']}\nBuilding: {sensor['building_name']}\nLocation: {sensor['location']}\nCurrent reading: {current_text}\nExpected range: {metric['expected']}\nDetected: {metric['detected_at'] or metric['recorded_at']}"
    return render_template("maintenance_form.html",page_title="Add Maintenance",equipment=equipment,sensors=sensors,staff=staff,priorities=PRIORITIES,prefill_equipment=request.args.get("equipment_id"),prefill_sensor=prefill_sensor,prefill_issue=prefill_issue,prefill_notes=prefill_notes)


@bp.post("/maintenance/<int:maintenance_id>/update")
@role_required("admin")
def maintenance_update(maintenance_id):
    try:
        staff_id=int(request.form.get("assigned_staff_id",0)); priority=request.form.get("priority"); issue=request.form.get("issue","").strip()
        if priority not in PRIORITIES or not issue: raise WorkflowError("Issue and valid priority are required.")
        with transaction() as database:
            database.execute("UPDATE maintenance_requests SET issue=?,priority=?,updated_at=? WHERE id=? AND status!='Completed'",(issue,priority,utc_now(),maintenance_id))
            current=database.execute("SELECT assigned_staff_id FROM maintenance_requests WHERE id=?",(maintenance_id,)).fetchone()
            if not current: raise WorkflowError("Maintenance was not found.")
            if current["assigned_staff_id"] != staff_id: assign_maintenance(database,maintenance_id,staff_id,g.user["id"])
        flash("Maintenance updated.","success")
    except (ValueError,WorkflowError) as error: flash(str(error),"danger")
    return redirect(url_for("core.maintenance_detail",maintenance_id=maintenance_id))


@bp.route("/approvals")
@role_required("admin")
def approvals():
    status=request.args.get("status",""); sql="""SELECT a.*,u.display_name,COALESCE(e.equipment_code,s.sensor_code,m.request_code) asset_code FROM approval_requests a JOIN users u ON u.id=a.requested_by_user_id LEFT JOIN equipment e ON e.id=a.equipment_id LEFT JOIN environmental_sensors s ON s.id=a.sensor_id LEFT JOIN maintenance_requests m ON m.id=a.maintenance_id WHERE 1=1"""; params=[]
    if status in ("Pending","Approved","Rejected"): sql+=" AND a.status=?"; params.append(status)
    rows=get_db().execute(sql+" ORDER BY a.submitted_at DESC",params).fetchall()
    return render_template("approvals.html",page_title="Approvals",approvals=rows,status=status)


@bp.route("/approvals/<int:approval_id>")
@role_required("admin")
def approval_detail(approval_id):
    row=get_db().execute("""SELECT a.*,u.display_name,u.staff_code,COALESCE(e.equipment_code,s.sensor_code,em.equipment_code,sm.sensor_code,m.request_code) asset_code,COALESCE(e.name,s.sensor_type,em.name,sm.sensor_type) asset_name,COALESCE(be.name,bs.name,bm.name) building_name,COALESCE(e.location,s.location,em.location,sm.location) asset_location FROM approval_requests a JOIN users u ON u.id=a.requested_by_user_id LEFT JOIN equipment e ON e.id=a.equipment_id LEFT JOIN environmental_sensors s ON s.id=a.sensor_id LEFT JOIN maintenance_requests m ON m.id=a.maintenance_id LEFT JOIN equipment em ON em.id=m.equipment_id LEFT JOIN environmental_sensors sm ON sm.id=m.sensor_id LEFT JOIN buildings be ON be.id=e.building_id LEFT JOIN buildings bs ON bs.id=s.building_id LEFT JOIN buildings bm ON bm.id=COALESCE(em.building_id,sm.building_id) WHERE a.id=?""",(approval_id,)).fetchone()
    if not row: abort(404)
    return render_template("approval_detail.html",page_title=row["request_code"],approval=row)


@bp.post("/approvals/<int:approval_id>/review")
@role_required("admin")
def approval_review(approval_id):
    try:
        with transaction() as database: review_approval(database,approval_id,g.user["id"],request.form.get("decision"),request.form.get("admin_comment",""))
        flash("Approval request reviewed.","success")
    except (WorkflowError,sqlite3.IntegrityError) as error: flash(str(error),"danger")
    return redirect(url_for("admin.approval_detail",approval_id=approval_id))


@bp.route("/staff")
@role_required("admin")
def staff_list():
    q=request.args.get("q","").strip(); active=request.args.get("active",""); sql="""SELECT u.*,SUM(CASE WHEN m.status!='Completed' THEN 1 ELSE 0 END) assigned_tasks,(SELECT COUNT(*) FROM approval_requests a WHERE a.requested_by_user_id=u.id AND a.status='Pending') pending_requests FROM users u LEFT JOIN maintenance_requests m ON m.assigned_staff_id=u.id WHERE u.role='staff'"""; params=[]
    if q: sql+=" AND (u.staff_code LIKE ? OR u.display_name LIKE ? OR u.username LIKE ?)"; params.extend((f"%{q}%",)*3)
    if active in ("0","1"): sql+=" AND u.active=?"; params.append(int(active))
    rows=get_db().execute(sql+" GROUP BY u.id ORDER BY u.staff_code",params).fetchall()
    return render_template("staff_list.html",page_title="Staff Management",staff=rows,q=q,active=active)


@bp.route("/staff/new",methods=("GET","POST"))
@role_required("admin")
def staff_create():
    if request.method=="POST":
        try:
            staff_code=request.form.get("staff_code","").strip().upper(); username=request.form.get("username","").strip(); name=request.form.get("display_name","").strip(); password=request.form.get("password","")
            if not all((staff_code,username,name)) or len(password)<8: raise WorkflowError("Complete all fields; the temporary password needs at least 8 characters.")
            with transaction() as database:
                database.execute("INSERT INTO users(staff_code,username,password_hash,display_name,role,active) VALUES (?,?,?,?, 'staff',?)",(staff_code,username,generate_password_hash(password),name,1 if request.form.get("active") else 0))
            flash("Staff account created.","success"); return redirect(url_for("admin.staff_list"))
        except (WorkflowError,sqlite3.IntegrityError) as error: flash("Staff ID and username must be unique." if isinstance(error,sqlite3.IntegrityError) else str(error),"danger")
    return render_template("staff_form.html",page_title="Add Staff")


@bp.route("/staff/<int:staff_id>",methods=("GET","POST"))
@role_required("admin")
def staff_detail(staff_id):
    database=get_db(); staff=database.execute("SELECT * FROM users WHERE id=? AND role='staff'",(staff_id,)).fetchone()
    if not staff: abort(404)
    if request.method=="POST":
        try:
            action=request.form.get("action")
            with transaction() as tx:
                if action=="profile":
                    name=request.form.get("display_name","").strip(); username=request.form.get("username","").strip()
                    if not name or not username: raise WorkflowError("Name and username are required.")
                    tx.execute("UPDATE users SET display_name=?,username=?,updated_at=? WHERE id=?",(name,username,utc_now(),staff_id))
                elif action=="toggle":
                    if staff["active"] and tx.execute("SELECT 1 FROM maintenance_requests WHERE assigned_staff_id=? AND status!='Completed'",(staff_id,)).fetchone(): raise WorkflowError("Reassign unfinished tasks before deactivating this account.")
                    tx.execute("UPDATE users SET active=?,updated_at=? WHERE id=?",(0 if staff["active"] else 1,utc_now(),staff_id))
                elif action=="password":
                    password=request.form.get("password","")
                    if len(password)<8: raise WorkflowError("Password must contain at least 8 characters.")
                    tx.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?",(generate_password_hash(password),utc_now(),staff_id))
                else: raise WorkflowError("Unknown action.")
            flash("Staff account updated.","success"); return redirect(url_for("admin.staff_detail",staff_id=staff_id))
        except (WorkflowError,sqlite3.IntegrityError) as error: flash("Username must be unique." if isinstance(error,sqlite3.IntegrityError) else str(error),"danger")
    tasks=database.execute("SELECT * FROM maintenance_requests WHERE assigned_staff_id=? AND status!='Completed' ORDER BY created_at DESC",(staff_id,)).fetchall(); approvals=database.execute("SELECT status,COUNT(*) count FROM approval_requests WHERE requested_by_user_id=? GROUP BY status",(staff_id,)).fetchall(); activity=database.execute("SELECT * FROM activity_logs WHERE user_id=? ORDER BY created_at DESC LIMIT 8",(staff_id,)).fetchall()
    return render_template("staff_detail.html",page_title=staff["display_name"],staff=staff,tasks=tasks,approval_counts={r['status']:r['count'] for r in approvals},activity=activity)


def _report_filters(args):
    today = date.today()
    default_start, default_end = today - timedelta(days=30), today
    report_type = args.get("report_type", "equipment")
    if report_type not in REPORT_TYPES:
        report_type = "equipment"
    errors = []
    try:
        start = date.fromisoformat(args.get("start_date") or default_start.isoformat())
        end = date.fromisoformat(args.get("end_date") or default_end.isoformat())
        if start > end:
            raise ValueError
    except (TypeError, ValueError):
        start, end = default_start, default_end
        errors.append("Enter a valid date range with From Date on or before To Date.")
    try:
        building_id = int(args["building_id"]) if args.get("building_id") else None
        if building_id is not None and building_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        building_id = None
        errors.append("Select a valid building.")
    return report_type, building_id, start.isoformat(), end.isoformat(), " ".join(errors)


def _report_data(database, report_type, building_id, start_date, end_date):
    params = [start_date, end_date]
    building_clause = ""
    if building_id:
        building_clause = " AND b.id=?"
        params.append(building_id)
    if report_type == "equipment":
        sql = """SELECT e.equipment_code AS 'Equipment ID',e.name AS 'Equipment',e.type AS 'Type',
                        b.name AS 'Building',e.location AS 'Location',e.status AS 'Status',e.updated_at AS 'Last Updated'
                 FROM equipment e JOIN buildings b ON b.id=e.building_id
                 WHERE date(COALESCE(e.updated_at,e.created_at)) BETWEEN ? AND ?""" + building_clause + " ORDER BY e.equipment_code"
    elif report_type == "maintenance":
        sql = """SELECT m.request_code AS 'Request ID',COALESCE(e.equipment_code,s.sensor_code) AS 'Equipment',
                        m.issue AS 'Issue',m.priority AS 'Priority',m.status AS 'Status',b.name AS 'Building',
                        COALESCE(u.display_name,'Unassigned') AS 'Assigned Staff',m.created_at AS 'Created Date',
                        m.completed_at AS 'Completed Date'
                 FROM maintenance_requests m LEFT JOIN equipment e ON e.id=m.equipment_id
                 LEFT JOIN environmental_sensors s ON s.id=m.sensor_id
                 JOIN buildings b ON b.id=COALESCE(e.building_id,s.building_id)
                 LEFT JOIN users u ON u.id=m.assigned_staff_id
                 WHERE date(m.created_at) BETWEEN ? AND ?""" + building_clause + " ORDER BY m.created_at DESC"
    elif report_type == "environmental":
        sql = """SELECT r.recorded_at AS 'Date/Time',b.name AS 'Building',r.temperature AS 'Temperature',
                        r.humidity AS 'Humidity',r.air_quality AS 'Air Quality',r.energy_usage AS 'Energy Usage'
                 FROM environmental_readings r JOIN buildings b ON b.id=r.building_id
                 WHERE date(r.recorded_at) BETWEEN ? AND ?""" + building_clause + " ORDER BY r.recorded_at DESC"
    else:
        sql = """WITH activity_context AS (
                    SELECT l.*,
                      CASE l.entity_type
                        WHEN 'equipment' THEN (SELECT building_id FROM equipment WHERE id=l.entity_id)
                        WHEN 'sensor' THEN (SELECT building_id FROM environmental_sensors WHERE id=l.entity_id)
                        WHEN 'maintenance' THEN (SELECT COALESCE(e.building_id,s.building_id) FROM maintenance_requests m LEFT JOIN equipment e ON e.id=m.equipment_id LEFT JOIN environmental_sensors s ON s.id=m.sensor_id WHERE m.id=l.entity_id)
                        WHEN 'approval' THEN (SELECT COALESCE(e.building_id,s.building_id,me.building_id,ms.building_id) FROM approval_requests a LEFT JOIN equipment e ON e.id=a.equipment_id LEFT JOIN environmental_sensors s ON s.id=a.sensor_id LEFT JOIN maintenance_requests m ON m.id=a.maintenance_id LEFT JOIN equipment me ON me.id=m.equipment_id LEFT JOIN environmental_sensors ms ON ms.id=m.sensor_id WHERE a.id=l.entity_id)
                      END AS related_building_id,
                      CASE l.entity_type
                        WHEN 'equipment' THEN (SELECT equipment_code FROM equipment WHERE id=l.entity_id)
                        WHEN 'sensor' THEN (SELECT sensor_code FROM environmental_sensors WHERE id=l.entity_id)
                        WHEN 'maintenance' THEN (SELECT request_code FROM maintenance_requests WHERE id=l.entity_id)
                        WHEN 'approval' THEN (SELECT request_code FROM approval_requests WHERE id=l.entity_id)
                      END AS related_record
                    FROM activity_logs l
                 )
                 SELECT a.created_at AS 'Date/Time',COALESCE(u.username,'System') AS 'Staff',
                   CASE a.action WHEN 'started' THEN 'Started Maintenance' WHEN 'note_added' THEN 'Added Work Note'
                     WHEN 'status_changed' THEN 'Status Changed' WHEN 'created' THEN 'Created Maintenance'
                     WHEN 'assigned' THEN 'Assigned Maintenance' WHEN 'submitted' THEN 'Submitted Request'
                     WHEN 'reviewed' THEN 'Reviewed Request' ELSE a.action END AS 'Action',
                   COALESCE(a.related_record,'—') AS 'Related Record',a.description AS 'Detail'
                 FROM activity_context a LEFT JOIN users u ON u.id=a.user_id
                 LEFT JOIN buildings b ON b.id=a.related_building_id
                 WHERE date(a.created_at) BETWEEN ? AND ?""" + building_clause + " ORDER BY a.created_at DESC"
    return database.execute(sql, params).fetchall()


@bp.route("/reports")
@role_required("admin")
def reports():
    report_type, building_id, start, end, error = _report_filters(request.args)
    database = get_db()
    if building_id and not database.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        building_id = None
        error = (error + " Select a valid building.").strip()
    if error:
        flash(error, "warning")
    rows = _report_data(database, report_type, building_id, start, end)
    buildings = database.execute("SELECT * FROM buildings ORDER BY active DESC,name").fetchall()
    return render_template("reports.html",page_title="Reports",rows=rows,columns=REPORT_COLUMNS[report_type],report_type=report_type,start_date=start,end_date=end,building_id=building_id,buildings=buildings)


@bp.route("/reports/export")
@role_required("admin")
def report_export():
    report_type, building_id, start, end, _error = _report_filters(request.args)
    database = get_db()
    if building_id and not database.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        building_id = None
    rows=_report_data(database,report_type,building_id,start,end); output=io.StringIO(newline=""); writer=csv.writer(output)
    writer.writerow(REPORT_COLUMNS[report_type])
    writer.writerows([tuple(row) for row in rows])
    return Response(output.getvalue(),mimetype="text/csv",headers={"Content-Disposition":f"attachment; filename={report_type}-report.csv"})


@bp.route("/system-settings",methods=("GET","POST"))
@role_required("admin")
def system_settings():
    if request.method=="POST":
        try:
            action=request.form.get("action")
            with transaction() as database:
                if action=="add_building":
                    name=request.form.get("name","").strip(); location=request.form.get("location","").strip()
                    if not name or not location: raise WorkflowError("Building name and location are required.")
                    cursor=database.execute("INSERT INTO buildings(name,location) VALUES (?,?)",(name,location))
                    database.execute("INSERT INTO environmental_thresholds VALUES (?,?,?,?,?,?,?,?,?)",(cursor.lastrowid,18,27,35,65,100,150,350,450))
                    for sensor_type,sensor_location in (("Temperature","Central Hall"),("Humidity","Central Hall"),("Air Quality","Main Corridor"),("Energy","Electrical Room")):
                        code=next_code(database,"environmental_sensors","sensor_code","SNS")
                        database.execute("INSERT INTO environmental_sensors(sensor_code,sensor_type,building_id,location) VALUES (?,?,?,?)",(code,sensor_type,cursor.lastrowid,sensor_location))
                elif action=="deactivate_building":
                    building_id=int(request.form.get("building_id",0))
                    dependency=database.execute("""SELECT 1 FROM maintenance_requests m LEFT JOIN equipment e ON e.id=m.equipment_id LEFT JOIN environmental_sensors s ON s.id=m.sensor_id WHERE COALESCE(e.building_id,s.building_id)=? AND m.status!='Completed'""",(building_id,)).fetchone()
                    pending=database.execute("""SELECT 1 FROM approval_requests a LEFT JOIN equipment e ON e.id=a.equipment_id LEFT JOIN environmental_sensors s ON s.id=a.sensor_id LEFT JOIN maintenance_requests m ON m.id=a.maintenance_id LEFT JOIN equipment me ON me.id=m.equipment_id LEFT JOIN environmental_sensors ms ON ms.id=m.sensor_id WHERE COALESCE(e.building_id,s.building_id,me.building_id,ms.building_id)=? AND a.status='Pending'""",(building_id,)).fetchone()
                    if dependency or pending: raise WorkflowError("Resolve active maintenance and approvals before removing this building.")
                    database.execute("UPDATE buildings SET active=0 WHERE id=?",(building_id,)); database.execute("UPDATE equipment SET active=0,updated_at=? WHERE building_id=?",(utc_now(),building_id)); database.execute("UPDATE environmental_sensors SET active=0,updated_at=? WHERE building_id=?",(utc_now(),building_id)); database.execute("UPDATE alerts SET condition='Cleared',cleared_at=?,updated_at=? WHERE building_id=? AND condition='Active'",(utc_now(),utc_now(),building_id))
                elif action=="thresholds":
                    values=[float(request.form[k]) for k in ("temperature_min","temperature_max","humidity_min","humidity_max","aqi_warning","aqi_critical","energy_warning","energy_critical")]; building_id=int(request.form["building_id"])
                    if values[0]>=values[1] or values[2]>=values[3] or values[4]>=values[5] or values[6]>=values[7]: raise WorkflowError("Each warning/minimum value must be below its maximum/critical value.")
                    database.execute("UPDATE environmental_thresholds SET temperature_min=?,temperature_max=?,humidity_min=?,humidity_max=?,aqi_warning=?,aqi_critical=?,energy_warning=?,energy_critical=? WHERE building_id=?",(*values,building_id))
                    latest=database.execute("SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1",(building_id,)).fetchone()
                    if latest: evaluate_environment(database,building_id,latest["temperature"],latest["humidity"],latest["air_quality"],latest["energy_usage"])
                else: raise WorkflowError("Unknown settings action.")
            flash("System settings saved.","success")
        except (ValueError,WorkflowError,sqlite3.IntegrityError) as error: flash("Building names must be unique." if isinstance(error,sqlite3.IntegrityError) else str(error),"danger")
        return redirect(url_for("admin.system_settings",building_id=request.form.get("building_id")))
    database=get_db(); buildings=database.execute("SELECT * FROM buildings ORDER BY active DESC,name").fetchall(); selected=int(request.args.get("building_id",buildings[0]["id"] if buildings else 0)); thresholds=database.execute("SELECT * FROM environmental_thresholds WHERE building_id=?",(selected,)).fetchone()
    return render_template("system_settings.html",page_title="Building & Threshold Settings",buildings=buildings,selected=selected,thresholds=thresholds)
