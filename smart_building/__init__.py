import os
from pathlib import Path

from flask import Flask, render_template

from . import db
from .auth import bp as auth_bp
from .core import bp as core_bp
from .admin import bp as admin_bp
from .security import init_security


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SMART_BUILDING_SECRET", "dev-only-change-me"),
        DATABASE=str(Path(app.instance_path) / "smart_building.db"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        APP_TIMEZONE="Asia/Yangon",
        ENVIRONMENT_READING_INTERVAL_SECONDS=300,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    with app.app_context():
        db.migrate_db()
    init_security(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(admin_bp)

    @app.errorhandler(400)
    def bad_request(error):
        message = getattr(error, "description", "The request could not be processed.")
        return render_template("error.html", code=400, message=message), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", code=403, message="You do not have permission to view this page."), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="The page you requested could not be found."), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("error.html", code=500, message="Something went wrong. Please try again."), 500

    return app
