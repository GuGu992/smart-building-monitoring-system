import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    database = g.pop("db", None)
    if database is not None:
        database.close()


@contextmanager
def transaction():
    database = get_db()
    try:
        database.execute("BEGIN IMMEDIATE")
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise


def init_db():
    database = get_db()
    schema_path = Path(__file__).with_name("schema.sql")
    database.executescript(schema_path.read_text(encoding="utf-8"))


def migrate_db():
    """Apply small, backwards-compatible upgrades to an existing local database."""
    database = get_db()
    if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
        return
    columns = {row["name"] for row in database.execute("PRAGMA table_info(users)")}
    if "work_status" not in columns:
        database.execute(
            "ALTER TABLE users ADD COLUMN work_status TEXT NOT NULL DEFAULT 'Available' "
            "CHECK (work_status IN ('Available', 'Busy', 'Off Duty'))"
        )
    if "last_active_at" not in columns:
        database.execute("ALTER TABLE users ADD COLUMN last_active_at TEXT")
    database.commit()
    sensor_schema = database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='environmental_sensors'").fetchone()
    if sensor_schema and "'Warning'" not in sensor_schema["sql"]:
        database.execute("PRAGMA foreign_keys = OFF")
        try:
            database.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE environmental_sensors_new (
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
                INSERT INTO environmental_sensors_new
                    (id,sensor_code,sensor_type,building_id,location,status,active,last_seen,created_at,updated_at)
                SELECT id,sensor_code,sensor_type,building_id,location,status,active,last_seen,created_at,updated_at
                FROM environmental_sensors;
                DROP TABLE environmental_sensors;
                ALTER TABLE environmental_sensors_new RENAME TO environmental_sensors;
                COMMIT;
                """
            )
        except Exception:
            database.rollback()
            raise
        finally:
            database.execute("PRAGMA foreign_keys = ON")
        if database.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Environmental sensor migration failed its foreign-key check.")


def _insert_user(database, username, password, name, role, staff_code=None, active=1):
    database.execute(
        "INSERT INTO users(staff_code, username, password_hash, display_name, role, active) VALUES (?, ?, ?, ?, ?, ?)",
        (staff_code, username, generate_password_hash(password), name, role, active),
    )


def seed_demo():
    database = get_db()
    if database.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return False
    now = datetime.now(timezone.utc)
    with transaction() as database:
        _insert_user(database, "admin", "Admin@123", "System Administrator", "admin")
        _insert_user(database, "staff01", "Staff@123", "Aye Aye", "staff", "ST-001")
        _insert_user(database, "staff02", "Staff@123", "Ko Min", "staff", "ST-002")
        database.executemany(
            "INSERT INTO buildings(name, location) VALUES (?, ?)",
            [("Building A", "Main Campus"), ("Building B", "Engineering Block")],
        )
        database.executemany(
            "INSERT INTO environmental_thresholds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(1, 18, 27, 35, 65, 100, 150, 350, 450), (2, 18, 27, 35, 65, 100, 150, 340, 440)],
        )
        equipment = [
            ("EQ-001", "Main Lobby AC", "HVAC", 1, "Ground Floor", "Normal"),
            ("EQ-002", "Lecture Hall AC", "HVAC", 1, "Floor 2", "Critical"),
            ("EQ-003", "Passenger Lift", "Lift", 1, "East Wing", "Normal"),
            ("EQ-004", "Water Pump", "Pump", 1, "Basement", "Normal"),
            ("EQ-005", "Server UPS", "Electrical", 1, "Server Room", "Normal"),
            ("EQ-006", "Workshop Extractor", "Ventilation", 2, "Workshop", "Normal"),
            ("EQ-007", "Engineering Lift", "Lift", 2, "West Wing", "Normal"),
            ("EQ-008", "Lab AC Unit", "HVAC", 2, "Lab 3", "In Progress"),
            ("EQ-009", "Fire Panel", "Safety", 2, "Ground Floor", "Normal"),
            ("EQ-010", "Backup Generator", "Electrical", 2, "Service Yard", "Normal"),
        ]
        database.executemany(
            "INSERT INTO equipment(equipment_code,name,type,building_id,location,status) VALUES (?,?,?,?,?,?)", equipment
        )
        sensor_rows = []
        number = 1
        for building_id in (1, 2):
            for sensor_type, location in (("Temperature", "Central Hall"), ("Humidity", "Central Hall"), ("Air Quality", "Main Corridor"), ("Energy", "Electrical Room")):
                sensor_rows.append((f"SNS-{number:03d}", sensor_type, building_id, location, "Normal", now.isoformat()))
                number += 1
        database.executemany(
            "INSERT INTO environmental_sensors(sensor_code,sensor_type,building_id,location,status,last_seen) VALUES (?,?,?,?,?,?)",
            sensor_rows,
        )
        rng = random.Random(333)
        for building_id in (1, 2):
            temp, humidity, aqi, energy = (23.5 + building_id * .3, 51 + building_id, 46 + building_id * 2, 285 + building_id * 18)
            for hours_ago in range(71, -1, -1):
                temp = min(26.2, max(21.3, temp + rng.uniform(-.25, .25)))
                humidity = min(61, max(42, humidity + rng.uniform(-1.0, 1.0)))
                aqi = min(84, max(30, aqi + rng.uniform(-3, 3)))
                energy = min(335, max(225, energy + rng.uniform(-7, 7)))
                stamp = (now - timedelta(hours=hours_ago)).isoformat()
                database.execute(
                    "INSERT INTO environmental_readings(building_id,temperature,humidity,air_quality,energy_usage,recorded_at) VALUES (?,?,?,?,?,?)",
                    (building_id, round(temp, 1), round(humidity, 1), round(aqi, 0), round(energy, 1), stamp),
                )
        database.execute(
            "INSERT INTO maintenance_requests(request_code,equipment_id,issue,initial_notes,priority,status,assigned_staff_id,created_by_user_id) VALUES ('MR-001',2,'Not cooling','Temperature reported above comfort range.','High','Open',2,1)"
        )
        database.execute(
            "INSERT INTO maintenance_requests(request_code,equipment_id,issue,initial_notes,priority,status,assigned_staff_id,created_by_user_id) VALUES ('MR-002',8,'Unusual fan noise','Inspection in progress.','Medium','In Progress',3,1)"
        )
        database.execute(
            "INSERT INTO maintenance_requests(request_code,equipment_id,issue,initial_notes,priority,status,assigned_staff_id,created_by_user_id,completed_at) VALUES ('MR-003',4,'Low water pressure','Filter cleaned and tested.','Low','Completed',2,1,?)",
            ((now - timedelta(days=2)).isoformat(),),
        )
        database.execute(
            "INSERT INTO maintenance_updates(maintenance_id,user_id,note,old_status,new_status) VALUES (2,3,'Started maintenance and checked fan housing.','Open','In Progress')"
        )
        database.execute(
            "INSERT INTO alerts(alert_code,alert_type,building_id,equipment_id,location,message,severity,condition) VALUES ('ALT-001','Equipment Fault',1,2,'Floor 2','Lecture Hall AC requires attention.','Critical','Active')"
        )
        database.execute(
            "INSERT INTO activity_logs(user_id,action,entity_type,entity_id,description) VALUES (3,'started','maintenance',2,'Ko Min started MR-002')"
        )
    return True


@click.command("init-db")
def init_db_command():
    init_db()
    click.echo("Initialized the database.")


@click.command("seed-demo")
def seed_demo_command():
    created = seed_demo()
    click.echo("Demo data seeded." if created else "Database already contains users; no demo data added.")


@click.command("reset-demo")
@click.option("--yes", is_flag=True, help="Confirm replacement of the local demo database.")
def reset_demo_command(yes):
    if not yes:
        raise click.ClickException("This replaces the local demo database. Re-run with --yes.")
    close_db()
    path = Path(current_app.config["DATABASE"]).resolve()
    instance = Path(current_app.instance_path).resolve()
    if instance not in path.parents or path.suffix != ".db":
        raise click.ClickException("Refusing to reset a database outside the Flask instance directory.")
    if path.exists():
        path.unlink()
    init_db()
    seed_demo()
    click.echo("Demo database reset and seeded.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(seed_demo_command)
    app.cli.add_command(reset_demo_command)
