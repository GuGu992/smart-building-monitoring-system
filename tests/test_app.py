from smart_building.db import get_db
from datetime import datetime, timedelta, timezone

from smart_building.services import ensure_automatic_readings, evaluate_environment, repair_sensor, set_sensor_status, simulate_reading


def test_login_paths_and_inactive_user(app, client, auth):
    assert b"Smart Building Monitoring" in client.get("/login").data
    response = auth.login(password="wrong")
    assert b"incorrect" in response.data
    response = auth.login("staff01", "Staff@123")
    assert b"My work overview" in response.data
    auth.logout()
    with app.app_context():
        get_db().execute("UPDATE users SET active=0 WHERE username='staff01'")
        get_db().commit()
    response = auth.login("staff01", "Staff@123")
    assert b"inactive" in response.data


def test_csrf_and_role_permissions(client, auth):
    response = client.post("/login", data={"username": "admin", "password": "Admin@123"})
    assert response.status_code == 400 and b"form expired" in response.data
    auth.login("staff01", "Staff@123")
    assert client.get("/admin/staff").status_code == 403
    assert client.get("/admin/reports").status_code == 403


def test_admin_creates_building_and_equipment(app, client, auth):
    auth.login()
    token = auth.token("/admin/system-settings")
    response = client.post("/admin/system-settings", data={"csrf_token": token, "action": "add_building", "name": "Library", "location": "North Campus"}, follow_redirects=True)
    assert b"System settings saved" in response.data
    token = auth.token("/admin/equipment/new")
    response = client.post("/admin/equipment/new", data={"csrf_token": token, "equipment_code": "LIB-01", "name": "Library AC", "type": "HVAC", "building_id": 3, "location": "Floor 1", "status": "Critical"}, follow_redirects=True)
    assert b"LIB-01" in response.data
    with app.app_context():
        database = get_db()
        assert database.execute("SELECT status FROM equipment WHERE equipment_code='LIB-01'").fetchone()[0] == "Critical"
        assert database.execute("SELECT COUNT(*) FROM alerts WHERE equipment_id=(SELECT id FROM equipment WHERE equipment_code='LIB-01') AND condition='Active'").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM environmental_sensors WHERE building_id=3").fetchone()[0] == 4


def test_equipment_fault_alert_deduplicates(app, client, auth):
    auth.login()
    for _ in range(2):
        token = auth.token("/equipment/1")
        client.post("/admin/equipment/1/fault", data={"csrf_token": token})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM alerts WHERE equipment_id=1 AND condition='Active'").fetchone()[0] == 1


def test_complete_maintenance_workflow(app, client, auth):
    auth.login("staff01", "Staff@123")
    token = auth.token("/maintenance/1")
    response = client.post("/maintenance/1/start", data={"csrf_token": token}, follow_redirects=True)
    assert b"Maintenance started" in response.data
    token = auth.token("/maintenance/1")
    response = client.post("/maintenance/1/note", data={"csrf_token": token, "note": "Replaced the damaged thermostat."}, follow_redirects=True)
    assert b"Work note added" in response.data
    token = auth.token("/maintenance/1")
    response = client.post("/maintenance/1/complete", data={"csrf_token": token, "note": "Unit tested successfully."}, follow_redirects=True)
    assert b"submitted for Admin approval" in response.data
    auth.logout(); auth.login()
    with app.app_context():
        approval_id = get_db().execute("SELECT id FROM approval_requests WHERE maintenance_id=1 AND status='Pending'").fetchone()[0]
    token = auth.token(f"/admin/approvals/{approval_id}")
    response = client.post(f"/admin/approvals/{approval_id}/review", data={"csrf_token": token, "decision": "Approved", "admin_comment": "Checked work notes."}, follow_redirects=True)
    assert b"Approval request reviewed" in response.data
    with app.app_context():
        database = get_db()
        assert database.execute("SELECT status FROM maintenance_requests WHERE id=1").fetchone()[0] == "Completed"
        assert database.execute("SELECT status FROM equipment WHERE id=2").fetchone()[0] == "Normal"
        assert database.execute("SELECT condition FROM alerts WHERE equipment_id=2").fetchone()[0] == "Cleared"
        assert database.execute("SELECT COUNT(*) FROM maintenance_updates WHERE maintenance_id=1").fetchone()[0] >= 3
    assert b"ALT-001" not in client.get("/alerts").data
    assert b"ALT-001" in client.get("/alerts?condition=Cleared").data


def test_staff_sees_only_assigned_maintenance(client, auth):
    auth.login("staff01", "Staff@123")
    page = client.get("/maintenance")
    assert b"MR-001" in page.data
    assert b"MR-002" not in page.data
    assert client.get("/maintenance/2").status_code == 403


def test_create_request_approval_and_rejection(app, client, auth):
    auth.login("staff01", "Staff@123")
    token = auth.token("/equipment/1")
    client.post("/equipment/1/request-maintenance", data={"csrf_token": token, "priority": "Medium", "reason": "Air flow is weaker than normal."})
    auth.logout(); auth.login()
    with app.app_context():
        approval_id = get_db().execute("SELECT id FROM approval_requests WHERE equipment_id=1 AND request_type='Create Maintenance'").fetchone()[0]
    token = auth.token(f"/admin/approvals/{approval_id}")
    client.post(f"/admin/approvals/{approval_id}/review", data={"csrf_token": token, "decision": "Approved", "admin_comment": "Approved for inspection."})
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM maintenance_requests WHERE equipment_id=1").fetchone()[0] == 1
    auth.logout(); auth.login("staff01", "Staff@123")
    token = auth.token("/equipment/3")
    client.post("/equipment/3/request-status", data={"csrf_token": token, "requested_status": "Critical", "reason": "Lift makes an unusual sound."})
    auth.logout(); auth.login()
    with app.app_context():
        rejection_id = get_db().execute("SELECT id FROM approval_requests WHERE equipment_id=3 AND status='Pending'").fetchone()[0]
    token = auth.token(f"/admin/approvals/{rejection_id}")
    client.post(f"/admin/approvals/{rejection_id}/review", data={"csrf_token": token, "decision": "Rejected", "admin_comment": "Inspection found normal operation."})
    with app.app_context():
        assert get_db().execute("SELECT status FROM equipment WHERE id=3").fetchone()[0] == "Normal"


def test_sensor_offline_alert_and_repair(app, client, auth):
    with app.app_context():
        database = get_db()
        set_sensor_status(database, 1, "Offline", 1)
        database.commit()
        alert = database.execute("SELECT * FROM alerts WHERE sensor_id=1 AND alert_type='Sensor Offline'").fetchone()
        assert alert["condition"] == "Active" and alert["severity"] == "Critical"
    auth.login()
    environment = client.get("/environment?building_id=1").data
    assert b"SNS-001" in environment and b"No Reading" in environment and b"Offline" in environment
    with app.app_context():
        database = get_db()
        repair_sensor(database, 1, 1)
        database.commit()
        assert database.execute("SELECT status FROM environmental_sensors WHERE id=1").fetchone()[0] == "Normal"
        assert database.execute("SELECT condition FROM alerts WHERE id=?", (alert["id"],)).fetchone()[0] == "Cleared"
    assert alert["alert_code"].encode() not in client.get("/alerts").data
    assert alert["alert_code"].encode() in client.get("/alerts?condition=Cleared").data


def test_environment_threshold_alert_and_auto_clear(app, client, auth):
    with app.app_context():
        database = get_db()
        evaluate_environment(database, 1, 31, 50, 40, 250)
        database.commit()
        alert = database.execute("SELECT * FROM alerts WHERE alert_type='High Temperature' AND building_id=1").fetchone()
        assert alert["condition"] == "Active" and alert["severity"] == "Warning" and alert["sensor_id"] == 1
        assert database.execute("SELECT status FROM environmental_sensors WHERE id=1").fetchone()[0] == "Warning"
        evaluate_environment(database, 1, 24, 50, 40, 250)
        database.commit()
        assert database.execute("SELECT condition FROM alerts WHERE id=?", (alert["id"],)).fetchone()[0] == "Cleared"
        assert database.execute("SELECT status FROM environmental_sensors WHERE id=1").fetchone()[0] == "Normal"
        alert_code = alert["alert_code"]
    auth.login()
    assert alert_code.encode() not in client.get("/alerts").data
    assert alert_code.encode() in client.get("/alerts?condition=Cleared").data


def test_automatic_readings_are_time_gated_and_realistic(app):
    with app.app_context():
        database = get_db()
        latest = database.execute("SELECT * FROM environmental_readings WHERE building_id=1 ORDER BY recorded_at DESC LIMIT 1").fetchone()
        latest_time = datetime.fromisoformat(latest["recorded_at"].replace("Z", "+00:00"))
        before_count = database.execute("SELECT COUNT(*) FROM environmental_readings WHERE building_id=1").fetchone()[0]
        assert ensure_automatic_readings(database, 1, 300, latest_time + timedelta(seconds=299)) == []
        assert database.execute("SELECT COUNT(*) FROM environmental_readings WHERE building_id=1").fetchone()[0] == before_count
        assert ensure_automatic_readings(database, 1, 300, latest_time + timedelta(seconds=301)) == [1]
        database.commit()
        current = database.execute("SELECT * FROM environmental_readings WHERE building_id=1 ORDER BY recorded_at DESC LIMIT 1").fetchone()
        assert abs(current["temperature"] - latest["temperature"]) <= .4
        assert abs(current["humidity"] - latest["humidity"]) <= 2
        assert abs(current["air_quality"] - latest["air_quality"]) <= 5
        assert abs(current["energy_usage"] - latest["energy_usage"]) <= 10
        assert ensure_automatic_readings(database, 1, 300, latest_time + timedelta(seconds=301)) == []


def test_dashboard_and_environment_share_warning_reading(app, client, auth):
    stamp = datetime.now(timezone.utc).isoformat()
    with app.app_context():
        database = get_db()
        database.execute("INSERT INTO environmental_readings(building_id,temperature,humidity,air_quality,energy_usage,recorded_at) VALUES (1,24,72,50,300,?)", (stamp,))
        evaluate_environment(database, 1, 24, 72, 50, 300)
        database.commit()
    auth.login()
    dashboard = client.get("/?building_id=1").data
    environment = client.get("/environment?building_id=1").data
    for page in (dashboard, environment):
        assert b"72.0%" in page and b"Warning" in page and b"environmental-problem-card" in page
    assert b"SNS-002" in environment and b"35\xe2\x80\x9365%" in environment
    with app.app_context():
        database = get_db()
        assert database.execute("SELECT status FROM environmental_sensors WHERE id=2").fetchone()[0] == "Warning"
        assert database.execute("SELECT COUNT(*) FROM alerts WHERE sensor_id=2 AND alert_type='Humidity High' AND condition='Active'").fetchone()[0] == 1


def test_all_environment_threshold_states_use_saved_settings(app):
    with app.app_context():
        database = get_db()
        database.execute("UPDATE environmental_thresholds SET temperature_min=20,temperature_max=25,humidity_min=40,humidity_max=60,aqi_warning=90,aqi_critical=140,energy_warning=400,energy_critical=500 WHERE building_id=1")
        evaluate_environment(database, 1, 27, 65, 100, 420)
        database.commit()
        for alert_type in ("High Temperature", "Humidity High", "Poor Air Quality", "High Energy Usage"):
            alert = database.execute("SELECT * FROM alerts WHERE building_id=1 AND alert_type=? AND condition='Active'", (alert_type,)).fetchone()
            assert alert and alert["severity"] == "Warning"
        evaluate_environment(database, 1, 23, 50, 150, 520)
        database.commit()
        assert database.execute("SELECT severity FROM alerts WHERE alert_type='Poor Air Quality' AND condition='Active'").fetchone()[0] == "Critical"
        assert database.execute("SELECT severity FROM alerts WHERE alert_type='High Energy Usage' AND condition='Active'").fetchone()[0] == "Critical"
        assert database.execute("SELECT COUNT(*) FROM alerts WHERE alert_type IN ('High Temperature','Humidity High') AND condition='Active'").fetchone()[0] == 0


def test_sensor_problem_prefills_and_completes_maintenance(app, client, auth):
    abnormal_stamp = datetime.now(timezone.utc).isoformat()
    with app.app_context():
        database = get_db()
        database.execute("INSERT INTO environmental_readings(building_id,temperature,humidity,air_quality,energy_usage,recorded_at) VALUES (1,24,72,50,300,?)", (abnormal_stamp,))
        evaluate_environment(database, 1, 24, 72, 50, 300)
        database.commit()
        abnormal_id = database.execute("SELECT MAX(id) FROM environmental_readings WHERE building_id=1").fetchone()[0]
        alert_id = database.execute("SELECT id FROM alerts WHERE sensor_id=2 AND alert_type='Humidity High' AND condition='Active'").fetchone()[0]

    auth.login()
    prefill = client.get("/admin/maintenance/new?sensor_id=2").data
    assert b"Humidity sensor: Humidity High" in prefill
    assert b"Current reading: 72" in prefill and b"Expected range: 35\xe2\x80\x9365%" in prefill
    token = auth.token("/admin/maintenance/new?sensor_id=2")
    response = client.post("/admin/maintenance/new?sensor_id=2", data={
        "csrf_token": token, "target": "sensor:2", "issue": "Humidity reading outside expected range",
        "priority": "High", "assigned_staff_id": "2", "notes": "Current 72%; expected 35-65%.",
    }, follow_redirects=True)
    assert b"Maintenance created" in response.data
    with app.app_context():
        database = get_db()
        maintenance_id = database.execute("SELECT id FROM maintenance_requests WHERE sensor_id=2 AND status='Open'").fetchone()[0]
        simulate_reading(database, 1, "normal")
        database.commit()
        assert database.execute("SELECT humidity FROM environmental_readings WHERE building_id=1 ORDER BY recorded_at DESC LIMIT 1").fetchone()[0] == 72

    auth.logout(); auth.login("staff01", "Staff@123")
    token = auth.token(f"/maintenance/{maintenance_id}")
    client.post(f"/maintenance/{maintenance_id}/start", data={"csrf_token": token})
    token = auth.token(f"/maintenance/{maintenance_id}")
    client.post(f"/maintenance/{maintenance_id}/note", data={"csrf_token": token, "note": "Checked and recalibrated the humidity sensor."})
    token = auth.token(f"/maintenance/{maintenance_id}")
    client.post(f"/maintenance/{maintenance_id}/complete", data={"csrf_token": token, "note": "Stable readings confirmed."})
    auth.logout(); auth.login()
    with app.app_context():
        approval_id = get_db().execute("SELECT id FROM approval_requests WHERE maintenance_id=? AND status='Pending'", (maintenance_id,)).fetchone()[0]
    token = auth.token(f"/admin/approvals/{approval_id}")
    client.post(f"/admin/approvals/{approval_id}/review", data={"csrf_token": token, "decision": "Approved", "admin_comment": "Calibration verified."})

    with app.app_context():
        database = get_db()
        thresholds = database.execute("SELECT * FROM environmental_thresholds WHERE building_id=1").fetchone()
        latest = database.execute("SELECT * FROM environmental_readings WHERE building_id=1 ORDER BY recorded_at DESC LIMIT 1").fetchone()
        assert database.execute("SELECT status FROM maintenance_requests WHERE id=?", (maintenance_id,)).fetchone()[0] == "Completed"
        assert database.execute("SELECT status FROM environmental_sensors WHERE id=2").fetchone()[0] == "Normal"
        assert thresholds["humidity_min"] <= latest["humidity"] <= thresholds["humidity_max"]
        assert database.execute("SELECT condition FROM alerts WHERE id=?", (alert_id,)).fetchone()[0] == "Cleared"
        assert database.execute("SELECT humidity FROM environmental_readings WHERE id=?", (abnormal_id,)).fetchone()[0] == 72
    environment = client.get("/environment?building_id=1").data
    assert b"50.0%" in environment and b"All environmental sensors are working normally" in environment
    assert b"72.0" in environment
    report = client.get("/admin/reports?report_type=environmental&building_id=1&start_date=2000-01-01&end_date=2100-01-01").data
    assert b"72.0" in report and b"50.0" in report


def test_environment_building_filter_sensor_details_and_no_manual_controls(app, client, auth):
    now = datetime.now(timezone.utc)
    with app.app_context():
        database = get_db()
        database.execute("INSERT INTO environmental_readings(building_id,temperature,humidity,air_quality,energy_usage,recorded_at) VALUES (1,22,48,45,280,?)", (now.isoformat(),))
        database.execute("INSERT INTO environmental_readings(building_id,temperature,humidity,air_quality,energy_usage,recorded_at) VALUES (2,26,58,70,330,?)", ((now + timedelta(seconds=1)).isoformat(),))
        evaluate_environment(database, 1, 22, 48, 45, 280)
        evaluate_environment(database, 2, 26, 58, 70, 330)
        database.commit()
    auth.login()
    building_a = client.get("/environment?building_id=1").data
    building_b = client.get("/environment?building_id=2").data
    assert b"22.0\xc2\xb0C" in building_a and b"SNS-001" in building_a and b"SNS-005" not in building_a
    assert b"26.0\xc2\xb0C" in building_b and b"SNS-005" in building_b and b"SNS-001" not in building_b
    for forbidden in (b"Generate Reading", b"Normal reading", b"Sensor simulation controls", b"Simulate Error", b"Restore Normal"):
        assert forbidden not in building_a
    assert not any(rule.rule == "/environment/simulate" for rule in app.url_map.iter_rules())
    assert not any(rule.rule == "/sensors/<int:sensor_id>/status" for rule in app.url_map.iter_rules())
    assert client.get("/sensors/1").status_code == 200
    auth.logout(); auth.login("staff01", "Staff@123")
    assert client.get("/sensors/1").status_code == 200


def test_admin_seen_is_not_changed_by_staff(app, client, auth):
    auth.login("staff01", "Staff@123")
    client.get("/alerts/1")
    with app.app_context(): assert get_db().execute("SELECT admin_seen FROM alerts WHERE id=1").fetchone()[0] == 0
    auth.logout(); auth.login()
    client.get("/alerts/1")
    with app.app_context(): assert get_db().execute("SELECT admin_seen FROM alerts WHERE id=1").fetchone()[0] == 1


def test_reports_csv_theme_and_notification(app, client, auth):
    auth.login()
    assert client.get("/admin/reports?report_type=equipment").status_code == 200
    csv_response = client.get("/admin/reports/export?report_type=equipment")
    assert csv_response.mimetype == "text/csv"
    assert b"Equipment" in csv_response.data
    settings = client.get("/settings")
    assert b"data-set-theme=\"dark\"" in settings.data
    with app.app_context():
        database = get_db()
        database.execute("INSERT INTO notifications(recipient_user_id,event_type,entity_type,entity_id,message) VALUES (1,'alert','alert',1,'Test notice')")
        notice_id = database.execute("SELECT last_insert_rowid()").fetchone()[0]
        database.commit()
    response = client.get(f"/notifications/{notice_id}")
    assert response.status_code == 302 and "/alerts/1" in response.headers["Location"]
    with app.app_context(): assert get_db().execute("SELECT read_at FROM notifications WHERE id=?", (notice_id,)).fetchone()[0]


def test_alert_filters_default_history_and_combinations(app, client, auth):
    with app.app_context():
        database = get_db()
        database.execute("INSERT INTO alerts(alert_code,alert_type,building_id,equipment_id,location,message,severity,condition) VALUES ('ALT-T01','Equipment Fault',1,1,'Ground Floor','Warning test','Warning','Active')")
        database.execute("INSERT INTO alerts(alert_code,alert_type,building_id,sensor_id,location,message,severity,condition) VALUES ('ALT-T02','Sensor Fault',2,5,'Central Hall','Critical test','Critical','Active')")
        database.execute("INSERT INTO alerts(alert_code,alert_type,building_id,location,message,severity,condition,cleared_at) VALUES ('ALT-T03','Historical Test',1,'Roof','Cleared test','Warning','Cleared','2026-01-01T00:00:00+00:00')")
        database.commit()
    auth.login()

    page = client.get("/alerts").data
    assert b"ALT-T01" in page and b"ALT-T02" in page and b"ALT-T03" not in page
    page = client.get("/alerts?building_id=1").data
    assert b"ALT-T01" in page and b"ALT-T02" not in page
    page = client.get("/alerts?severity=Warning").data
    assert b"ALT-T01" in page and b"ALT-T02" not in page
    page = client.get("/alerts?condition=Cleared").data
    assert b"ALT-T03" in page and b"ALT-T01" not in page
    page = client.get("/alerts?alert_type=Sensor+Fault").data
    assert b"ALT-T02" in page and b"ALT-T01" not in page
    page = client.get("/alerts?building_id=2&severity=Critical&condition=Active&alert_type=Sensor+Fault").data
    assert b"ALT-T02" in page and b"ALT-T01" not in page and b"ALT-001" not in page
    assert b"ALT-T03" in client.get("/alerts?condition=").data


def test_dashboard_active_alert_count_excludes_cleared(app, client, auth):
    auth.login()
    dashboard = client.get("/").get_data(as_text=True)
    active_card = dashboard.split(">Active Faults</span>", 1)[1][:300]
    assert ">1</div>" in active_card
    with app.app_context():
        database = get_db()
        database.execute("UPDATE alerts SET condition='Cleared',cleared_at='2026-09-21T00:00:00+00:00' WHERE condition='Active'")
        database.commit()
    dashboard = client.get("/").get_data(as_text=True)
    active_card = dashboard.split(">Active Faults</span>", 1)[1][:300]
    assert ">0</div>" in active_card
    assert "No active alerts" in dashboard


def test_report_types_filters_dates_and_matching_csv(client, auth):
    auth.login()
    dates = "start_date=2000-01-01&end_date=2100-01-01"

    equipment = client.get(f"/admin/reports?report_type=equipment&building_id=1&{dates}").data
    for heading in (b"Equipment ID", b"Equipment", b"Type", b"Building", b"Location", b"Status", b"Last Updated"):
        assert heading in equipment
    assert b"EQ-001" in equipment and b"EQ-006" not in equipment

    maintenance = client.get(f"/admin/reports?report_type=maintenance&building_id=1&{dates}").data
    for heading in (b"Request ID", b"Equipment", b"Issue", b"Priority", b"Status", b"Building", b"Assigned Staff", b"Created Date", b"Completed Date"):
        assert heading in maintenance
    assert b"MR-001" in maintenance and b"MR-003" in maintenance and b"MR-002" not in maintenance

    environmental = client.get(f"/admin/reports?report_type=environmental&building_id=2&{dates}").data
    for heading in (b"Date/Time", b"Building", b"Temperature", b"Humidity", b"Air Quality", b"Energy Usage"):
        assert heading in environmental
    assert b"<td>Building B</td>" in environmental and b"<td>Building A</td>" not in environmental

    activity = client.get(f"/admin/reports?report_type=activity&building_id=2&{dates}").data
    for heading in (b"Date/Time", b"Staff", b"Action", b"Related Record", b"Detail"):
        assert heading in activity
    assert b"MR-002" in activity
    assert b"MR-002" not in client.get(f"/admin/reports?report_type=activity&building_id=1&{dates}").data

    empty = client.get("/admin/reports?report_type=equipment&start_date=2000-01-01&end_date=2000-01-02").data
    assert b"No report data" in empty and b"Last Updated" in empty
    invalid = client.get("/admin/reports?report_type=equipment&start_date=wrong&end_date=also-wrong").data
    assert b"Enter a valid date range" in invalid

    exported = client.get(f"/admin/reports/export?report_type=maintenance&building_id=1&{dates}")
    assert exported.mimetype == "text/csv"
    lines = exported.data.decode("utf-8").splitlines()
    assert lines[0] == "Request ID,Equipment,Issue,Priority,Status,Building,Assigned Staff,Created Date,Completed Date"
    exported_text = exported.get_data(as_text=True)
    assert "MR-001" in exported_text and "MR-003" in exported_text and "MR-002" not in exported_text


def test_staff_controls_work_status_and_admin_sees_activity(app, client, auth):
    auth.login("staff01", "Staff@123")
    token = auth.token("/settings")
    response = client.post("/settings", data={"csrf_token": token, "action": "work_status", "work_status": "Busy"}, follow_redirects=True)
    assert b"Busy" in response.data and b"Settings saved" in response.data
    with app.app_context():
        row = get_db().execute("SELECT work_status,last_active_at FROM users WHERE username='staff01'").fetchone()
        assert row["work_status"] == "Busy" and row["last_active_at"]

    with client.session_transaction() as session:
        session["last_active_touch"] = "2000-01-01T00:00:00+00:00"
    with app.app_context():
        get_db().execute("UPDATE users SET last_active_at='2000-01-01T00:00:00+00:00' WHERE username='staff01'")
        get_db().commit()
    client.get("/equipment")
    with app.app_context():
        first_touch = get_db().execute("SELECT last_active_at FROM users WHERE username='staff01'").fetchone()[0]
    assert first_touch != "2000-01-01T00:00:00+00:00"
    client.get("/environment")
    with app.app_context():
        second_touch = get_db().execute("SELECT last_active_at FROM users WHERE username='staff01'").fetchone()[0]
    assert second_touch == first_touch

    auth.logout()
    auth.login()
    staff_list = client.get("/admin/staff").data
    assert b"Account Status" in staff_list and b"Work Status" in staff_list and b"Last Active" in staff_list and b"Busy" in staff_list
    staff_detail = client.get("/admin/staff/2").data
    assert b"Work Status" in staff_detail and b"Last Active" in staff_detail and b"Assigned Tasks" in staff_detail and b"Busy" in staff_detail

    token = auth.token("/settings")
    response = client.post("/settings", data={"csrf_token": token, "action": "work_status", "work_status": "Off Duty"}, follow_redirects=True)
    assert b"Only Staff can change" in response.data
    with app.app_context():
        assert get_db().execute("SELECT work_status FROM users WHERE username='admin'").fetchone()[0] == "Available"


def test_all_primary_pages_render(client, auth):
    auth.login()
    admin_pages = [
        "/", "/equipment", "/equipment/1", "/maintenance", "/maintenance/1",
        "/environment", "/sensors/1", "/alerts", "/alerts/1", "/settings",
        "/admin/equipment/new", "/admin/equipment/1/edit", "/admin/maintenance/new",
        "/admin/approvals", "/admin/staff", "/admin/staff/new", "/admin/staff/2",
        "/admin/reports", "/admin/system-settings",
    ]
    for path in admin_pages:
        response = client.get(path)
        assert response.status_code == 200, path
    auth.logout(); auth.login("staff01", "Staff@123")
    for path in ("/", "/equipment", "/equipment/1", "/maintenance", "/maintenance/1", "/environment", "/sensors/1", "/alerts", "/alerts/1", "/my-requests", "/settings"):
        response = client.get(path)
        assert response.status_code == 200, path
