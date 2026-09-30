import random
from datetime import datetime, timedelta, timezone


PRIORITIES = ("Low", "Medium", "High", "Critical")
EQUIPMENT_STATUSES = ("Normal", "Critical", "In Progress")
SENSOR_STATUSES = ("Normal", "Warning", "Error", "Offline", "Fixed")
ENVIRONMENT_METRICS = {
    "temperature": {"sensor_type": "Temperature", "label": "Temperature", "unit": "°C"},
    "humidity": {"sensor_type": "Humidity", "label": "Humidity", "unit": "%"},
    "air_quality": {"sensor_type": "Air Quality", "label": "Air Quality", "unit": " AQI"},
    "energy_usage": {"sensor_type": "Energy", "label": "Energy Usage", "unit": " kWh"},
}


class WorkflowError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def next_code(database, table, column, prefix):
    rows = database.execute(f"SELECT {column} FROM {table} WHERE {column} LIKE ?", (f"{prefix}-%",)).fetchall()
    highest = 0
    for row in rows:
        try:
            highest = max(highest, int(row[0].split("-")[-1]))
        except (ValueError, AttributeError):
            pass
    return f"{prefix}-{highest + 1:03d}"


def log_activity(database, user_id, action, entity_type, entity_id, description):
    database.execute(
        "INSERT INTO activity_logs(user_id,action,entity_type,entity_id,description) VALUES (?,?,?,?,?)",
        (user_id, action, entity_type, entity_id, description),
    )


def notify(database, user_id, event_type, entity_type, entity_id, message):
    if user_id:
        database.execute(
            "INSERT INTO notifications(recipient_user_id,event_type,entity_type,entity_id,message) VALUES (?,?,?,?,?)",
            (user_id, event_type, entity_type, entity_id, message),
        )


def notify_admins(database, event_type, entity_type, entity_id, message):
    for row in database.execute("SELECT id FROM users WHERE role='admin' AND active=1"):
        notify(database, row["id"], event_type, entity_type, entity_id, message)


def create_alert(database, alert_type, building_id, location, message, severity, equipment_id=None, sensor_id=None, current_value=None, limit_value=None):
    existing = database.execute(
        "SELECT * FROM alerts WHERE alert_type=? AND building_id=? AND equipment_id IS ? AND sensor_id IS ? AND condition='Active'",
        (alert_type, building_id, equipment_id, sensor_id),
    ).fetchone()
    if existing:
        database.execute(
            "UPDATE alerts SET message=?, severity=?, current_value=?, limit_value=?, updated_at=? WHERE id=?",
            (message, severity, current_value, limit_value, utc_now(), existing["id"]),
        )
        return existing["id"]
    code = next_code(database, "alerts", "alert_code", "ALT")
    cursor = database.execute(
        "INSERT INTO alerts(alert_code,alert_type,building_id,equipment_id,sensor_id,location,message,severity,current_value,limit_value) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (code, alert_type, building_id, equipment_id, sensor_id, location, message, severity, current_value, limit_value),
    )
    alert_id = cursor.lastrowid
    notify_admins(database, "alert", "alert", alert_id, message)
    if severity == "Critical":
        for row in database.execute("SELECT id FROM users WHERE role='staff' AND active=1"):
            notify(database, row["id"], "critical_alert", "alert", alert_id, message)
    return alert_id


def clear_alerts(database, *, alert_type=None, building_id=None, equipment_id=None, sensor_id=None):
    clauses = ["condition='Active'"]
    params = []
    for column, value in (("alert_type", alert_type), ("building_id", building_id), ("equipment_id", equipment_id), ("sensor_id", sensor_id)):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    database.execute(
        f"UPDATE alerts SET condition='Cleared', cleared_at=?, updated_at=? WHERE {' AND '.join(clauses)}",
        (utc_now(), utc_now(), *params),
    )


def set_equipment_status(database, equipment_id, status, actor_id=None):
    if status not in EQUIPMENT_STATUSES:
        raise WorkflowError("Invalid equipment status.")
    equipment = database.execute(
        "SELECT e.*, b.name building_name FROM equipment e JOIN buildings b ON b.id=e.building_id WHERE e.id=? AND e.active=1", (equipment_id,)
    ).fetchone()
    if not equipment:
        raise WorkflowError("Equipment was not found.")
    if status == "Normal":
        active_work = database.execute(
            "SELECT 1 FROM maintenance_requests WHERE equipment_id=? AND status IN ('Open','In Progress')", (equipment_id,)
        ).fetchone()
        if active_work:
            raise WorkflowError("Equipment cannot return to Normal while maintenance is active.")
    database.execute("UPDATE equipment SET status=?,updated_at=? WHERE id=?", (status, utc_now(), equipment_id))
    if status == "Critical":
        create_alert(database, "Equipment Fault", equipment["building_id"], equipment["location"], f"{equipment['name']} requires attention.", "Critical", equipment_id=equipment_id)
    elif status == "Normal":
        clear_alerts(database, equipment_id=equipment_id)
    if actor_id and status != equipment["status"]:
        log_activity(database, actor_id, "status_changed", "equipment", equipment_id, f"{equipment['equipment_code']} changed from {equipment['status']} to {status}")


def set_sensor_status(database, sensor_id, status, actor_id=None):
    if status not in SENSOR_STATUSES:
        raise WorkflowError("Invalid sensor status.")
    sensor = database.execute("SELECT * FROM environmental_sensors WHERE id=? AND active=1", (sensor_id,)).fetchone()
    if not sensor:
        raise WorkflowError("Sensor was not found.")
    stamp = utc_now()
    database.execute("UPDATE environmental_sensors SET status=?,updated_at=?,last_seen=? WHERE id=?", (status, stamp, stamp, sensor_id))
    if status in ("Error", "Offline"):
        alert_type = "Sensor Error" if status == "Error" else "Sensor Offline"
        clear_alerts(database, alert_type="Sensor Offline" if status == "Error" else "Sensor Error", sensor_id=sensor_id)
        create_alert(database, alert_type, sensor["building_id"], sensor["location"], f"{sensor['sensor_code']} is {status.lower()}.", "Critical", sensor_id=sensor_id)
    elif status in ("Normal", "Warning", "Fixed"):
        clear_alerts(database, alert_type="Sensor Error", sensor_id=sensor_id)
        clear_alerts(database, alert_type="Sensor Offline", sensor_id=sensor_id)
    if actor_id and status != sensor["status"]:
        log_activity(database, actor_id, "status_changed", "sensor", sensor_id, f"{sensor['sensor_code']} changed from {sensor['status']} to {status}")


def create_maintenance(database, *, actor_id, issue, priority, equipment_id=None, sensor_id=None, assigned_staff_id=None, notes=""):
    if priority not in PRIORITIES or not issue.strip():
        raise WorkflowError("Issue and a valid priority are required.")
    if bool(equipment_id) == bool(sensor_id):
        raise WorkflowError("Choose exactly one equipment item or sensor.")
    if assigned_staff_id:
        staff = database.execute("SELECT * FROM users WHERE id=? AND role='staff' AND active=1", (assigned_staff_id,)).fetchone()
        if not staff:
            raise WorkflowError("Assigned Staff account is not active.")
    target_column, target_id = ("equipment_id", equipment_id) if equipment_id else ("sensor_id", sensor_id)
    existing = database.execute(
        f"SELECT request_code FROM maintenance_requests WHERE {target_column}=? AND status IN ('Open','In Progress')", (target_id,)
    ).fetchone()
    if existing:
        raise WorkflowError(f"{existing['request_code']} already covers this item.")
    if equipment_id:
        target = database.execute("SELECT * FROM equipment WHERE id=? AND active=1", (equipment_id,)).fetchone()
        if not target:
            raise WorkflowError("Equipment was not found.")
        if target["status"] == "Normal":
            set_equipment_status(database, equipment_id, "Critical", actor_id)
    else:
        target = database.execute("SELECT * FROM environmental_sensors WHERE id=? AND active=1", (sensor_id,)).fetchone()
        if not target:
            raise WorkflowError("Sensor was not found.")
        if target["status"] in ("Normal", "Fixed"):
            set_sensor_status(database, sensor_id, "Error", actor_id)
    code = next_code(database, "maintenance_requests", "request_code", "MR")
    cursor = database.execute(
        "INSERT INTO maintenance_requests(request_code,equipment_id,sensor_id,issue,initial_notes,priority,assigned_staff_id,created_by_user_id) VALUES (?,?,?,?,?,?,?,?)",
        (code, equipment_id, sensor_id, issue.strip(), notes.strip(), priority, assigned_staff_id, actor_id),
    )
    maintenance_id = cursor.lastrowid
    log_activity(database, actor_id, "created", "maintenance", maintenance_id, f"Created {code}")
    if assigned_staff_id:
        notify(database, assigned_staff_id, "assignment", "maintenance", maintenance_id, f"{code} was assigned to you.")
    return maintenance_id


def assign_maintenance(database, maintenance_id, staff_id, actor_id):
    maintenance = database.execute("SELECT * FROM maintenance_requests WHERE id=? AND status!='Completed'", (maintenance_id,)).fetchone()
    staff = database.execute("SELECT * FROM users WHERE id=? AND role='staff' AND active=1", (staff_id,)).fetchone()
    if not maintenance or not staff:
        raise WorkflowError("Maintenance or Staff record was not found.")
    database.execute("UPDATE maintenance_requests SET assigned_staff_id=?,updated_at=? WHERE id=?", (staff_id, utc_now(), maintenance_id))
    notify(database, staff_id, "assignment", "maintenance", maintenance_id, f"{maintenance['request_code']} was assigned to you.")
    log_activity(database, actor_id, "assigned", "maintenance", maintenance_id, f"Assigned {maintenance['request_code']} to {staff['display_name']}")


def start_maintenance(database, maintenance_id, staff_id):
    maintenance = database.execute("SELECT * FROM maintenance_requests WHERE id=?", (maintenance_id,)).fetchone()
    if not maintenance or maintenance["assigned_staff_id"] != staff_id or maintenance["status"] != "Open":
        raise WorkflowError("Only the assigned Staff member can start open maintenance.")
    database.execute("UPDATE maintenance_requests SET status='In Progress',updated_at=? WHERE id=?", (utc_now(), maintenance_id))
    if maintenance["equipment_id"]:
        set_equipment_status(database, maintenance["equipment_id"], "In Progress")
    database.execute(
        "INSERT INTO maintenance_updates(maintenance_id,user_id,note,old_status,new_status) VALUES (?,?,?,?,?)",
        (maintenance_id, staff_id, "Started maintenance.", "Open", "In Progress"),
    )
    user = database.execute("SELECT display_name FROM users WHERE id=?", (staff_id,)).fetchone()
    log_activity(database, staff_id, "started", "maintenance", maintenance_id, f"{user['display_name']} started {maintenance['request_code']}")


def add_work_note(database, maintenance_id, staff_id, note):
    maintenance = database.execute("SELECT * FROM maintenance_requests WHERE id=?", (maintenance_id,)).fetchone()
    if not maintenance or maintenance["assigned_staff_id"] != staff_id or maintenance["status"] != "In Progress":
        raise WorkflowError("Work notes can only be added to your in-progress maintenance.")
    if not note.strip():
        raise WorkflowError("Enter a work note.")
    database.execute("INSERT INTO maintenance_updates(maintenance_id,user_id,note) VALUES (?,?,?)", (maintenance_id, staff_id, note.strip()))
    log_activity(database, staff_id, "note_added", "maintenance", maintenance_id, f"Added a work note to {maintenance['request_code']}")


def create_approval(database, *, user_id, request_type, reason, equipment_id=None, sensor_id=None, maintenance_id=None, priority=None, requested_status=None):
    if not reason.strip():
        raise WorkflowError("A reason is required.")
    if request_type == "Create Maintenance":
        column, target_id = ("equipment_id", equipment_id) if equipment_id else ("sensor_id", sensor_id)
        target_table = "equipment" if equipment_id else "environmental_sensors"
        if not target_id or not database.execute(f"SELECT 1 FROM {target_table} WHERE id=? AND active=1", (target_id,)).fetchone():
            raise WorkflowError("The selected item is not active.")
        if database.execute(f"SELECT 1 FROM maintenance_requests WHERE {column}=? AND status IN ('Open','In Progress')", (target_id,)).fetchone():
            raise WorkflowError("This item already has active maintenance.")
        if database.execute(f"SELECT 1 FROM approval_requests WHERE request_type='Create Maintenance' AND {column}=? AND status='Pending'", (target_id,)).fetchone():
            raise WorkflowError("A maintenance request for this item is already pending.")
    elif request_type == "Equipment Status Change":
        if not equipment_id or requested_status not in ("Normal", "Critical"):
            raise WorkflowError("Choose an active equipment item and a valid status.")
        if database.execute("SELECT 1 FROM approval_requests WHERE request_type='Equipment Status Change' AND equipment_id=? AND status='Pending'", (equipment_id,)).fetchone():
            raise WorkflowError("A status request for this equipment is already pending.")
    code = next_code(database, "approval_requests", "request_code", "APR")
    cursor = database.execute(
        "INSERT INTO approval_requests(request_code,requested_by_user_id,request_type,equipment_id,sensor_id,maintenance_id,requested_priority,requested_status,reason) VALUES (?,?,?,?,?,?,?,?,?)",
        (code, user_id, request_type, equipment_id, sensor_id, maintenance_id, priority, requested_status, reason.strip()),
    )
    approval_id = cursor.lastrowid
    notify_admins(database, "approval", "approval", approval_id, f"New {request_type.lower()} request {code}.")
    log_activity(database, user_id, "submitted", "approval", approval_id, f"Submitted {code}")
    return approval_id


def submit_completion(database, maintenance_id, staff_id, note):
    maintenance = database.execute("SELECT * FROM maintenance_requests WHERE id=?", (maintenance_id,)).fetchone()
    if not maintenance or maintenance["assigned_staff_id"] != staff_id or maintenance["status"] != "In Progress":
        raise WorkflowError("Only the assigned Staff member can submit in-progress maintenance.")
    pending = database.execute("SELECT 1 FROM approval_requests WHERE maintenance_id=? AND request_type='Complete Maintenance' AND status='Pending'", (maintenance_id,)).fetchone()
    if pending:
        raise WorkflowError("Completion is already waiting for approval.")
    return create_approval(database, user_id=staff_id, request_type="Complete Maintenance", maintenance_id=maintenance_id, reason=note)


def review_approval(database, approval_id, admin_id, decision, comment=""):
    approval = database.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    if not approval or approval["status"] != "Pending":
        raise WorkflowError("This request has already been reviewed or no longer exists.")
    if decision not in ("Approved", "Rejected"):
        raise WorkflowError("Invalid review decision.")
    if decision == "Rejected" and not comment.strip():
        raise WorkflowError("Add an Admin comment when rejecting a request.")
    if decision == "Approved":
        if approval["request_type"] == "Create Maintenance":
            create_maintenance(
                database, actor_id=admin_id, issue=approval["reason"], priority=approval["requested_priority"],
                equipment_id=approval["equipment_id"], sensor_id=approval["sensor_id"],
            )
        elif approval["request_type"] == "Complete Maintenance":
            maintenance = database.execute("SELECT * FROM maintenance_requests WHERE id=?", (approval["maintenance_id"],)).fetchone()
            if not maintenance or maintenance["status"] != "In Progress":
                raise WorkflowError("Maintenance is no longer in progress.")
            database.execute("UPDATE maintenance_requests SET status='Completed',completed_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), maintenance["id"]))
            database.execute(
                "INSERT INTO maintenance_updates(maintenance_id,user_id,note,old_status,new_status) VALUES (?,?,?,?,?)",
                (maintenance["id"], admin_id, "Completion approved. " + comment.strip(), "In Progress", "Completed"),
            )
            if maintenance["equipment_id"]:
                database.execute("UPDATE equipment SET status='Normal',updated_at=? WHERE id=?", (utc_now(), maintenance["equipment_id"]))
                clear_alerts(database, equipment_id=maintenance["equipment_id"])
            else:
                repair_sensor(database, maintenance["sensor_id"], admin_id)
        else:
            if database.execute("SELECT 1 FROM maintenance_requests WHERE equipment_id=? AND status IN ('Open','In Progress')", (approval["equipment_id"],)).fetchone():
                raise WorkflowError("Equipment status cannot be changed while maintenance is active.")
            set_equipment_status(database, approval["equipment_id"], approval["requested_status"], admin_id)
    database.execute(
        "UPDATE approval_requests SET status=?,reviewed_by_user_id=?,reviewed_at=?,admin_comment=? WHERE id=? AND status='Pending'",
        (decision, admin_id, utc_now(), comment.strip(), approval_id),
    )
    notify(database, approval["requested_by_user_id"], "approval_decision", "approval", approval_id, f"{approval['request_code']} was {decision.lower()}.")
    log_activity(database, admin_id, "reviewed", "approval", approval_id, f"{decision} {approval['request_code']}")


def _metric_condition(metric, value, thresholds):
    if metric == "temperature":
        minimum, maximum = thresholds["temperature_min"], thresholds["temperature_max"]
        expected = f"{minimum:g}–{maximum:g}°C"
        if value < minimum:
            return "Warning", "Low Temperature", minimum, expected
        if value > maximum:
            return "Warning", "High Temperature", maximum, expected
        return "Normal", "Normal", None, expected
    if metric == "humidity":
        minimum, maximum = thresholds["humidity_min"], thresholds["humidity_max"]
        expected = f"{minimum:g}–{maximum:g}%"
        if value < minimum:
            return "Warning", "Humidity Low", minimum, expected
        if value > maximum:
            return "Warning", "Humidity High", maximum, expected
        return "Normal", "Normal", None, expected
    if metric == "air_quality":
        if value >= thresholds["aqi_critical"]:
            return "Critical", "Poor Air Quality", thresholds["aqi_critical"], f"Warning {thresholds['aqi_warning']:g} AQI"
        if value >= thresholds["aqi_warning"]:
            return "Warning", "Poor Air Quality", thresholds["aqi_warning"], f"Warning {thresholds['aqi_warning']:g} AQI"
        return "Normal", "Good", None, f"Warning {thresholds['aqi_warning']:g} AQI"
    if value >= thresholds["energy_critical"]:
        return "Critical", "High Energy Usage", thresholds["energy_critical"], f"Warning {thresholds['energy_warning']:g} kWh"
    if value >= thresholds["energy_warning"]:
        return "Warning", "High Energy Usage", thresholds["energy_warning"], f"Warning {thresholds['energy_warning']:g} kWh"
    return "Normal", "Normal", None, f"Warning {thresholds['energy_warning']:g} kWh"


def environmental_snapshot(database, building_id, reading=None):
    thresholds = database.execute("SELECT * FROM environmental_thresholds WHERE building_id=?", (building_id,)).fetchone()
    if reading is None:
        reading = database.execute("SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1", (building_id,)).fetchone()
    sensors = {
        row["sensor_type"]: row
        for row in database.execute("SELECT * FROM environmental_sensors WHERE building_id=? AND active=1", (building_id,))
    }
    if not thresholds:
        return {}
    result = {}
    for metric, details in ENVIRONMENT_METRICS.items():
        sensor = sensors.get(details["sensor_type"])
        value = reading[metric] if reading else None
        condition, summary, limit, expected = _metric_condition(metric, value, thresholds) if value is not None else ("Offline", "No reading", None, "No data")
        hardware_status = sensor["status"] if sensor else "Offline"
        if hardware_status == "Offline":
            display_condition, summary, display_value = "Offline", "Sensor Offline", None
        elif hardware_status == "Error":
            display_condition, summary, display_value = "Error", "Sensor Error", value
        elif hardware_status == "Fixed":
            display_condition, summary, display_value = "Fixed", "Repair completed", value
        else:
            display_condition, display_value = condition, value
        alert = None
        if sensor:
            alert = database.execute(
                "SELECT * FROM alerts WHERE sensor_id=? AND condition='Active' ORDER BY created_at DESC LIMIT 1",
                (sensor["id"],),
            ).fetchone()
        result[metric] = {
            **details,
            "value": display_value,
            "condition": display_condition,
            "summary": summary,
            "limit": limit,
            "expected": expected,
            "problem": display_condition in ("Warning", "Critical", "Error", "Offline"),
            "sensor": sensor,
            "detected_at": alert["created_at"] if alert else (sensor["updated_at"] if sensor else None),
            "recorded_at": reading["recorded_at"] if reading else None,
        }
    return result


def evaluate_environment(database, building_id, temperature, humidity, air_quality, energy_usage):
    thresholds = database.execute("SELECT * FROM environmental_thresholds WHERE building_id=?", (building_id,)).fetchone()
    building = database.execute("SELECT * FROM buildings WHERE id=? AND active=1", (building_id,)).fetchone()
    if not thresholds or not building:
        raise WorkflowError("Building thresholds were not found.")
    raw_values = {"temperature": temperature, "humidity": humidity, "air_quality": air_quality, "energy_usage": energy_usage}
    sensors = {
        row["sensor_type"]: row
        for row in database.execute("SELECT * FROM environmental_sensors WHERE building_id=? AND active=1", (building_id,))
    }
    for metric, value in raw_values.items():
        sensor = sensors.get(ENVIRONMENT_METRICS[metric]["sensor_type"])
        if not sensor:
            continue
        metric_alert_types = {
            "temperature": ("Low Temperature", "High Temperature"),
            "humidity": ("Humidity Low", "Humidity High"),
            "air_quality": ("Poor Air Quality",),
            "energy_usage": ("High Energy Usage",),
        }[metric]
        condition, alert_type, limit, _expected = _metric_condition(metric, value, thresholds)
        hardware_problem = sensor["status"] in ("Error", "Offline")
        for possible_type in metric_alert_types:
            database.execute(
                "UPDATE alerts SET condition='Cleared',cleared_at=?,updated_at=? WHERE alert_type=? AND building_id=? AND sensor_id IS NULL AND equipment_id IS NULL AND condition='Active'",
                (utc_now(), utc_now(), possible_type, building_id),
            )
            if condition == "Normal" or hardware_problem or possible_type != alert_type:
                clear_alerts(database, alert_type=possible_type, sensor_id=sensor["id"])
        if hardware_problem:
            continue
        new_sensor_status = "Warning" if condition in ("Warning", "Critical") else "Normal"
        database.execute(
            "UPDATE environmental_sensors SET status=?,last_seen=?,updated_at=? WHERE id=?",
            (new_sensor_status, utc_now(), utc_now(), sensor["id"]),
        )
        if condition != "Normal":
            message = f"{ENVIRONMENT_METRICS[metric]['label']} reading is outside the configured limit."
            create_alert(
                database, alert_type, building_id, sensor["location"], message, condition,
                sensor_id=sensor["id"], current_value=value, limit_value=limit,
            )


def simulate_reading(database, building_id, scenario="auto", rng=None, recorded_at=None):
    thresholds = database.execute("SELECT * FROM environmental_thresholds WHERE building_id=?", (building_id,)).fetchone()
    if not thresholds:
        raise WorkflowError("Thresholds must be configured first.")
    previous = database.execute(
        "SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1", (building_id,)
    ).fetchone()
    sequence = database.execute("SELECT COUNT(*) + 1 FROM environmental_readings WHERE building_id=?", (building_id,)).fetchone()[0]
    rng = rng or random.Random(f"environment:{building_id}:{sequence}")
    generated = {
        "temperature": (previous["temperature"] if previous else 24) + rng.uniform(-.4, .4),
        "humidity": (previous["humidity"] if previous else 52) + rng.uniform(-2, 2),
        "air_quality": (previous["air_quality"] if previous else 50) + rng.uniform(-5, 5),
        "energy_usage": (previous["energy_usage"] if previous else 300) + rng.uniform(-10, 10),
    }
    generated["temperature"] = min(40, max(8, generated["temperature"]))
    generated["humidity"] = min(95, max(15, generated["humidity"]))
    generated["air_quality"] = min(300, max(5, generated["air_quality"]))
    generated["energy_usage"] = min(700, max(80, generated["energy_usage"]))
    if scenario in ("auto", "normal"):
        generated["temperature"] = min(thresholds["temperature_max"] - .5, max(thresholds["temperature_min"] + .5, generated["temperature"]))
        generated["humidity"] = min(thresholds["humidity_max"] - 1, max(thresholds["humidity_min"] + 1, generated["humidity"]))
        generated["air_quality"] = min(thresholds["aqi_warning"] - 5, max(15, generated["air_quality"]))
        generated["energy_usage"] = min(thresholds["energy_warning"] - 10, max(100, generated["energy_usage"]))
    if scenario == "high_temp": generated["temperature"] = thresholds["temperature_max"] + 4
    elif scenario == "low_temp": generated["temperature"] = thresholds["temperature_min"] - 4
    elif scenario == "high_humidity": generated["humidity"] = thresholds["humidity_max"] + 12
    elif scenario == "poor_air": generated["air_quality"] = thresholds["aqi_critical"] + 10
    elif scenario == "high_energy": generated["energy_usage"] = thresholds["energy_critical"] + 25
    elif scenario not in ("auto", "normal"):
        raise WorkflowError("Unknown simulation scenario.")
    if scenario == "auto" and sequence % 12 == 0:
        generated["air_quality"] = thresholds["aqi_warning"] + 5
    active_sensor_work = database.execute(
        """SELECT s.sensor_type FROM environmental_sensors s
           JOIN maintenance_requests m ON m.sensor_id=s.id
           WHERE s.building_id=? AND s.status='Warning'
             AND m.status IN ('Open','In Progress')""",
        (building_id,),
    ).fetchall()
    sensor_metric = {details["sensor_type"]: metric for metric, details in ENVIRONMENT_METRICS.items()}
    for row in active_sensor_work:
        metric = sensor_metric[row["sensor_type"]]
        if previous:
            generated[metric] = previous[metric]
    values = tuple(round(generated[metric], 1) for metric in ("temperature", "humidity", "air_quality", "energy_usage"))
    database.execute(
        "INSERT INTO environmental_readings(building_id,temperature,humidity,air_quality,energy_usage,recorded_at) VALUES (?,?,?,?,?,?)",
        (building_id, *values, recorded_at or utc_now()),
    )
    if scenario == "auto" and sequence % 80 == 0:
        sensor = database.execute("SELECT id FROM environmental_sensors WHERE building_id=? AND active=1 ORDER BY id LIMIT 1", (building_id,)).fetchone()
        if sensor:
            set_sensor_status(database, sensor["id"], "Offline")
    elif scenario == "auto" and sequence % 40 == 0:
        sensor = database.execute("SELECT id FROM environmental_sensors WHERE building_id=? AND active=1 ORDER BY id LIMIT 1", (building_id,)).fetchone()
        if sensor:
            set_sensor_status(database, sensor["id"], "Error")
    evaluate_environment(database, building_id, *values)
    return values


def ensure_automatic_readings(database, building_id=None, interval_seconds=300, now=None):
    now = now or datetime.now(timezone.utc)
    if building_id:
        building_ids = [building_id]
    else:
        building_ids = [row["id"] for row in database.execute("SELECT id FROM buildings WHERE active=1")]
    generated = []
    for selected_id in building_ids:
        latest = database.execute(
            "SELECT recorded_at FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1",
            (selected_id,),
        ).fetchone()
        due = latest is None
        if latest:
            try:
                recorded = datetime.fromisoformat(str(latest["recorded_at"]).replace("Z", "+00:00"))
                if recorded.tzinfo is None:
                    recorded = recorded.replace(tzinfo=timezone.utc)
                due = now - recorded >= timedelta(seconds=max(0, interval_seconds))
            except (TypeError, ValueError):
                due = True
        if due:
            simulate_reading(database, selected_id, "auto", recorded_at=now.isoformat())
            generated.append(selected_id)
    return generated


def repair_sensor(database, sensor_id, actor_id=None):
    sensor = database.execute("SELECT * FROM environmental_sensors WHERE id=? AND active=1", (sensor_id,)).fetchone()
    if not sensor:
        raise WorkflowError("Sensor was not found.")
    thresholds = database.execute("SELECT * FROM environmental_thresholds WHERE building_id=?", (sensor["building_id"],)).fetchone()
    previous = database.execute(
        "SELECT * FROM environmental_readings WHERE building_id=? ORDER BY recorded_at DESC LIMIT 1", (sensor["building_id"],)
    ).fetchone()
    values = {
        "temperature": previous["temperature"] if previous else 24,
        "humidity": previous["humidity"] if previous else 52,
        "air_quality": previous["air_quality"] if previous else 50,
        "energy_usage": previous["energy_usage"] if previous else 300,
    }
    target_metric = next(key for key, details in ENVIRONMENT_METRICS.items() if details["sensor_type"] == sensor["sensor_type"])
    stable_values = {
        "temperature": (thresholds["temperature_min"] + thresholds["temperature_max"]) / 2,
        "humidity": (thresholds["humidity_min"] + thresholds["humidity_max"]) / 2,
        "air_quality": max(0, thresholds["aqi_warning"] - 20),
        "energy_usage": max(0, thresholds["energy_warning"] - 30),
    }
    values[target_metric] = stable_values[target_metric]
    stamp = utc_now()
    database.execute(
        "UPDATE environmental_sensors SET status='Normal',last_seen=?,updated_at=? WHERE id=?", (stamp, stamp, sensor_id)
    )
    database.execute(
        "INSERT INTO environmental_readings(building_id,temperature,humidity,air_quality,energy_usage,recorded_at) VALUES (?,?,?,?,?,?)",
        (sensor["building_id"], values["temperature"], values["humidity"], values["air_quality"], values["energy_usage"], stamp),
    )
    clear_alerts(database, sensor_id=sensor_id)
    evaluate_environment(database, sensor["building_id"], values["temperature"], values["humidity"], values["air_quality"], values["energy_usage"])
    if actor_id:
        log_activity(database, actor_id, "status_changed", "sensor", sensor_id, f"{sensor['sensor_code']} returned to Normal after repair")
