import re

import pytest

from smart_building import create_app
from smart_building.db import get_db, init_db, seed_demo


@pytest.fixture()
def app(tmp_path):
    application = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE": str(tmp_path / "test.db"),
    })
    with application.app_context():
        init_db()
        seed_demo()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_from(response):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.data[:500]
    return match.group(1).decode()


class AuthActions:
    def __init__(self, client):
        self.client = client

    def login(self, username="admin", password="Admin@123"):
        page = self.client.get("/login")
        return self.client.post("/login", data={"username": username, "password": password, "csrf_token": csrf_from(page)}, follow_redirects=True)

    def logout(self):
        page = self.client.get("/")
        return self.client.post("/logout", data={"csrf_token": csrf_from(page)}, follow_redirects=True)

    def token(self, path="/"):
        return csrf_from(self.client.get(path))


@pytest.fixture()
def auth(client):
    return AuthActions(client)

