# Smart Building Monitoring System

A server-rendered university prototype for monitoring building equipment, simulated environmental conditions, alerts, maintenance work, staff requests, and approval workflows. It was built as a clean CET333 Product Development project with straightforward Flask and SQLite code.

## Technology

- Python 3 and Flask 3.1
- SQLite through Python's built-in `sqlite3` module
- Jinja HTML templates
- Bootstrap 5.3.8 and Bootstrap Icons 1.13.1 (vendored locally)
- Chart.js 4.5.0 (vendored locally)
- Vanilla JavaScript and custom CSS
- pytest

No Node.js, React, ORM, WebSocket, cloud service, or real IoT hardware is required. Once installed, the site does not need internet access.

## Main Features

- One secure login for Admin and Staff roles; no public registration
- Role-specific responsive sidebars, dashboards, pages, and actions
- Multi-building equipment monitoring with Normal, Critical, and In Progress states
- Automatic, time-gated environmental simulation using realistic random-walk readings
- Transactional maintenance assignment, start, notes, completion submission, and Admin approval
- Create Maintenance, Complete Maintenance, and Equipment Status Change approval requests
- Shared Dashboard/Environment readings evaluated against each building's saved thresholds
- Automatic equipment, sensor, temperature, humidity, AQI, and energy alerts
- Automatic environmental alert clearing when a normal reading is recorded
- Persistent in-app notifications, independent Admin-seen alert state, and activity history
- Equipment, maintenance, environmental, and Staff activity reports with filtered CSV export
- Admin-managed Staff accounts with workload, work-status, and last-active visibility; buildings; and per-building thresholds
- Persistent light/dark mode and responsive mobile offcanvas navigation
- CSRF protection, secure password hashing, active-account checks, and server-side role/ownership validation

## Project Structure

```text
app.py                         Flask entry point
requirements.txt              Pinned Python dependencies
smart_building/
  __init__.py                 Application factory and error pages
  db.py                       SQLite connection, CLI, and demo seed
  schema.sql                  Tables, constraints, indexes
  security.py                 Session, CSRF, roles, template helpers
  services.py                 Transactional workflow and alert logic
  auth.py                     Login/logout routes
  core.py                     Shared and Staff-facing routes
  admin.py                    Admin-only routes and reports
  templates/                  Jinja pages
  static/                     CSS, JavaScript, and local vendor assets
tests/                        pytest integration/workflow tests
instance/                     Generated local database (not committed)
```

## Installation

From PowerShell in the project directory:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m flask --app app init-db
python -m flask --app app seed-demo
```

For local development, start the server with:

```powershell
python -m flask --app app run --debug
```

Then open `http://127.0.0.1:5000`.

For a non-development deployment, set a long random `SMART_BUILDING_SECRET` environment variable and configure HTTPS before enabling secure-only session cookies.

## Demo Accounts

| Role | Username | Password |
|---|---|---|
| Admin | `admin` | `Admin@123` |
| Staff | `staff01` | `Staff@123` |
| Staff | `staff02` | `Staff@123` |

These are demonstration credentials only. Passwords are stored as Werkzeug hashes, never plaintext.

To restore the known demo state, use the guarded command below. It replaces only the configured `.db` file inside Flask's `instance` directory:

```powershell
python -m flask --app app reset-demo --yes
```

## Roles

### Admin

Admin users can view all building data, add/edit/remove equipment, create and assign maintenance, review approval requests, manage Staff accounts, inspect environmental sensor details, export reports, add/remove buildings, and configure thresholds.

### Staff

Staff users can view equipment, environment, and alerts; see only maintenance assigned to them; start work; add work notes; submit completion; request maintenance; request equipment status changes; view their own requests and Admin comments; set their own Available/Busy/Off Duty work status; edit their display name; change password; and select a theme. Work status is separate from the Admin-controlled account Active/Inactive state.

## Core Workflows

### Equipment and Maintenance

1. A simulated or reported equipment fault changes Normal → Critical and creates one Active alert.
2. Admin creates maintenance directly, or approves a Staff Create Maintenance request.
3. Admin assigns active Staff.
4. Assigned Staff start work: maintenance becomes In Progress and equipment becomes In Progress.
5. Staff add work notes and submit completion for approval.
6. Admin approval completes maintenance, returns equipment to Normal, and clears its active alert.
7. A rejected completion remains In Progress and can be resubmitted with corrected work.

### Environmental Sensors

Sensor connection and environmental condition are separate. Connection is **Online** or **Offline**; condition is **Normal**, **Warning**, or **Critical** only while the sensor is online. Admin can mark a sensor Offline from Sensor Detail after confirmation. That creates one active Sensor Offline alert, shows a current UI value of zero, and stores null for that metric in subsequent reading rows so charts show a gap rather than fake zero data. Maintenance can target any problem sensor. Approved completion returns the sensor Online, records a stable in-range value, restores Normal condition, and clears its active alerts while preserving earlier abnormal readings.

### Environmental Conditions

Dashboard and Environment requests generate one realistic next reading only after the configured interval has elapsed; no background service or manual simulation button is required. Normal readings use small changes from the previous valid reading. Automatic generation has an 8% Warning chance and a 2% Critical chance, applied to one online metric, rather than a fixed every-N-reading pattern.

Each saved reading uses the latest per-building thresholds. Temperature is Warning up to 3°C outside its configured range and Critical beyond 3°C. Humidity is Warning up to 10 percentage points outside its range and Critical beyond 10. AQI and energy use their configured Warning and Critical limits. Later normal readings clear the corresponding environmental alert automatically. Dashboard cards, Environment cards, charts, sensor state, alerts, and reports all use the same saved reading.

The implementation wireframe and state-transition mock are available in [`docs/environmental-sensor-mock-flow.html`](docs/environmental-sensor-mock-flow.html).

## Database

The SQLite database enables foreign keys and contains:

- `users`
- `buildings`
- `equipment`
- `maintenance_requests`
- `maintenance_updates`
- `approval_requests`
- `environmental_sensors`
- `environmental_readings`
- `environmental_thresholds`
- `alerts`
- `activity_logs`
- `notifications`

Records with history are deactivated instead of deleted. Partial unique indexes prevent simultaneous active maintenance and duplicate active alerts for the same target.

## Page Guide

- **Dashboard:** role-specific counts, environmental summary, alerts, maintenance, requests, and activity
- **Equipment:** building/search/status filters and equipment detail/history
- **Maintenance:** Admin-wide work list or Staff's assigned tasks
- **Environment:** latest values and conditions, 24-reading trend charts, sensor overview, active problems, and full sensor list/details
- **Alerts:** building/severity/condition/type filters and detailed values/limits
- **Approvals / My Requests:** Admin review or Staff-specific decision history
- **Staff Management:** account and work status, workload, request counts, last active time, recent activity, and reset controls
- **Reports:** four simple report types with date/building filters and CSV export
- **Settings:** profile, password, appearance, Staff work status, and Admin access to building/threshold configuration

## Testing

Run:

```powershell
python -m pytest -q
```

The suite uses isolated temporary SQLite databases and covers authentication, CSRF and permissions, building/equipment creation, combined alert filters and history, alert deduplication, assigned-work visibility, the full maintenance-completion workflow, approval/rejection, automatic reading timing and anomaly paths, shared Dashboard/Environment values, exact environmental threshold boundaries, separate connection/condition state, Admin-only offline control, offline chart gaps, sensor details and building filters, sensor repair normalization, retained chart/report history, Admin-seen behavior, all report filters and CSV parity, Staff work status and last-active tracking, notifications, theme markup, and primary-page rendering.

## Limitations

- Sensor and environmental data are deliberately simulated and advance on normal Dashboard/Environment requests after a short time gate.
- Notifications update on normal page requests; there is no live push or background scheduler.
- The application targets a small university prototype and a single SQLite database, not high-concurrency production deployment.
- Theme preference is stored in the current browser rather than synchronized between devices.
- CSV is the only export format.

## Future Improvements

- Connect authenticated physical sensor gateways
- Add scheduled reading ingestion and delivery channels for urgent notifications
- Add building/floor plans and richer energy comparison charts
- Add database migration tooling if the prototype evolves beyond its fixed schema
- Add accessibility testing and end-to-end browser automation

## Vercel browser-demo deployment

The repository includes a separate static Vercel target. Local development continues to use Flask and SQLite. Vercel builds the files in `vercel/` into `public/` and stores demo data in each browser's `localStorage`.

1. Push the repository to GitHub, GitLab, or Bitbucket.
2. Import it into Vercel and select **Other** as the framework preset.
3. Keep the settings supplied by `vercel.json`.
4. Deploy. The build command is `python scripts/build_vercel.py` and the output directory is `public`.

Hosted data is not shared between browsers or devices. Clearing browser site data removes it. The included demo passwords are for demonstration only; localStorage is not a secure authentication backend.
