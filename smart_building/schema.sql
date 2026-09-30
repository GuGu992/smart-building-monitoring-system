PRAGMA foreign_keys = ON;

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_code TEXT COLLATE NOCASE UNIQUE,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'staff')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    work_status TEXT NOT NULL DEFAULT 'Available' CHECK (work_status IN ('Available', 'Busy', 'Off Duty')),
    last_active_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE buildings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    location TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    location TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Normal' CHECK (status IN ('Normal', 'Critical', 'In Progress')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE environmental_sensors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    sensor_type TEXT NOT NULL CHECK (sensor_type IN ('Temperature', 'Humidity', 'Air Quality', 'Energy')),
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    location TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Normal' CHECK (status IN ('Normal', 'Warning', 'Error', 'Offline', 'Fixed')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    last_seen TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE maintenance_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    equipment_id INTEGER REFERENCES equipment(id),
    sensor_id INTEGER REFERENCES environmental_sensors(id),
    issue TEXT NOT NULL,
    initial_notes TEXT,
    priority TEXT NOT NULL CHECK (priority IN ('Low', 'Medium', 'High', 'Critical')),
    status TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'In Progress', 'Completed')),
    assigned_staff_id INTEGER REFERENCES users(id),
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT,
    CHECK ((equipment_id IS NOT NULL) <> (sensor_id IS NOT NULL))
);

CREATE TABLE maintenance_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maintenance_id INTEGER NOT NULL REFERENCES maintenance_requests(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE approval_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
    request_type TEXT NOT NULL CHECK (request_type IN ('Create Maintenance', 'Complete Maintenance', 'Equipment Status Change')),
    equipment_id INTEGER REFERENCES equipment(id),
    sensor_id INTEGER REFERENCES environmental_sensors(id),
    maintenance_id INTEGER REFERENCES maintenance_requests(id),
    requested_priority TEXT CHECK (requested_priority IN ('Low', 'Medium', 'High', 'Critical')),
    requested_status TEXT CHECK (requested_status IN ('Normal', 'Critical')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Approved', 'Rejected')),
    submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    reviewed_by_user_id INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    admin_comment TEXT,
    CHECK (
        (request_type = 'Create Maintenance' AND maintenance_id IS NULL AND ((equipment_id IS NOT NULL) <> (sensor_id IS NOT NULL))) OR
        (request_type = 'Complete Maintenance' AND maintenance_id IS NOT NULL AND equipment_id IS NULL AND sensor_id IS NULL) OR
        (request_type = 'Equipment Status Change' AND equipment_id IS NOT NULL AND sensor_id IS NULL AND maintenance_id IS NULL)
    )
);

CREATE TABLE environmental_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    temperature REAL NOT NULL,
    humidity REAL NOT NULL,
    air_quality REAL NOT NULL,
    energy_usage REAL NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE environmental_thresholds (
    building_id INTEGER PRIMARY KEY REFERENCES buildings(id),
    temperature_min REAL NOT NULL,
    temperature_max REAL NOT NULL,
    humidity_min REAL NOT NULL,
    humidity_max REAL NOT NULL,
    aqi_warning REAL NOT NULL,
    aqi_critical REAL NOT NULL,
    energy_warning REAL NOT NULL,
    energy_critical REAL NOT NULL
);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    alert_type TEXT NOT NULL,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    equipment_id INTEGER REFERENCES equipment(id),
    sensor_id INTEGER REFERENCES environmental_sensors(id),
    location TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('Warning', 'Critical')),
    condition TEXT NOT NULL DEFAULT 'Active' CHECK (condition IN ('Active', 'Cleared')),
    admin_seen INTEGER NOT NULL DEFAULT 0 CHECK (admin_seen IN (0, 1)),
    current_value REAL,
    limit_value REAL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    cleared_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_user_id INTEGER NOT NULL REFERENCES users(id),
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    read_at TEXT
);

CREATE INDEX idx_equipment_building_status ON equipment(building_id, status, active);
CREATE INDEX idx_maintenance_status_staff ON maintenance_requests(status, assigned_staff_id);
CREATE INDEX idx_readings_building_time ON environmental_readings(building_id, recorded_at DESC);
CREATE INDEX idx_alerts_condition_building ON alerts(condition, building_id, created_at DESC);
CREATE INDEX idx_approvals_status_user ON approval_requests(status, requested_by_user_id);
CREATE INDEX idx_notifications_recipient ON notifications(recipient_user_id, read_at, created_at DESC);
CREATE UNIQUE INDEX one_open_equipment_maintenance ON maintenance_requests(equipment_id) WHERE equipment_id IS NOT NULL AND status IN ('Open', 'In Progress');
CREATE UNIQUE INDEX one_open_sensor_maintenance ON maintenance_requests(sensor_id) WHERE sensor_id IS NOT NULL AND status IN ('Open', 'In Progress');
CREATE UNIQUE INDEX one_pending_completion ON approval_requests(maintenance_id) WHERE request_type = 'Complete Maintenance' AND status = 'Pending';
CREATE UNIQUE INDEX one_active_equipment_alert ON alerts(equipment_id, alert_type) WHERE equipment_id IS NOT NULL AND condition = 'Active';
CREATE UNIQUE INDEX one_active_sensor_alert ON alerts(sensor_id, alert_type) WHERE sensor_id IS NOT NULL AND condition = 'Active';
CREATE UNIQUE INDEX one_active_building_alert ON alerts(building_id, alert_type) WHERE equipment_id IS NULL AND sensor_id IS NULL AND condition = 'Active';
