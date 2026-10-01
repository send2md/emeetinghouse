"""Tests for the self-service login/registration flow in app.py.

app.py reads its DB path and secrets from environment variables at
import time and keeps a single in-process `house`/`conn`, so each test
here reloads the module against a fresh temp database rather than
sharing state between tests.
"""

from __future__ import annotations

import importlib
import io

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EMEETINGHOUSE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EMEETINGHOUSE_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("EMEETINGHOUSE_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("EMEETINGHOUSE_UPLOADS", str(tmp_path / "uploads"))

    import app as app_module

    importlib.reload(app_module)
    app_module.app.testing = True
    with app_module.app.test_client() as test_client:
        yield test_client, app_module


def _Image(name="photo.jpg"):
    return (io.BytesIO(b"not really a jpeg, just test bytes"), name)


def _Register(test_client, username, name="Jane Doe", password="correct horse battery", **overrides):
    """Drive the whole /login -> /register flow for a brand-new username."""
    test_client.post("/login", data={"username": username, "password": "whatever"})
    data = {
        "name": name,
        "password": password,
        "address": "123 Main St",
        "mothers_maiden_name": "Smith",
        "age": "30",
        "thumbprint": _Image("thumb.jpg"),
        "photo": _Image("photo.jpg"),
    }
    data.update(overrides)
    return test_client.post("/register", data=data, content_type="multipart/form-data", follow_redirects=True)


def _AdminLogin(client):
    return client.post("/admin/login", data={"password": "test-admin-password"}, follow_redirects=True)


def test_unknown_username_goes_straight_to_registration(client):
    test_client, _ = client
    resp = test_client.post("/login", data={"username": "janedoe", "password": "x"}, follow_redirects=True)
    assert b"janedoe is your login. You are a new Participant." in resp.data


def test_full_registration_creates_a_working_login(client):
    test_client, app_module = client
    resp = _Register(test_client, "janedoe")
    assert b"Welcome, Jane Doe" in resp.data
    assert b"123 Main St" in resp.data  # printable confirmation shows what was submitted
    assert b"data:image/jpeg;base64," in resp.data  # photo/thumbprint embedded for printing

    test_client.get("/logout")
    login_resp = test_client.post(
        "/login", data={"username": "janedoe", "password": "correct horse battery"}, follow_redirects=True
    )
    assert b"Welcome, Jane Doe" in login_resp.data


def test_registration_requires_all_fields(client):
    test_client, _ = client
    test_client.post("/login", data={"username": "janedoe", "password": "x"})
    resp = test_client.post(
        "/register",
        data={"name": "", "password": "short", "address": "", "mothers_maiden_name": "", "age": "not-a-number"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Whole real name is required" in resp.data
    assert b"at least 8 characters" in resp.data
    assert b"Age must be a whole number" in resp.data
    assert b"photo" in resp.data.lower()
    assert b"thumbprint" in resp.data.lower()


def test_known_username_wrong_password_offers_retry_or_new_login(client):
    test_client, _ = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")

    resp = test_client.post("/login", data={"username": "janedoe", "password": "nope"}, follow_redirects=True)
    assert b"that password didn" in resp.data.lower()
    assert b"I made a mistake, try again" in resp.data
    assert b"This is a new login" in resp.data


def test_claiming_a_taken_username_offers_a_free_alternative(client):
    test_client, _ = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")

    resp = test_client.post("/login/claim", data={"username": "janedoe"}, follow_redirects=True)
    assert b"&#34;janedoe&#34; is taken" in resp.data or b'"janedoe" is taken' in resp.data
    assert b"janedoe2" in resp.data  # the first free suffixed alternative

    start_resp = test_client.post("/register/start", data={"username": "janedoe2"}, follow_redirects=True)
    assert b"janedoe2 is your login. You are a new Participant." in start_resp.data


def test_register_start_loops_back_if_the_alternative_is_also_taken(client):
    test_client, _ = client
    _Register(test_client, "janedoe")
    _Register(test_client, "janedoe2", name="Jane Two")
    test_client.get("/logout")

    resp = test_client.post("/register/start", data={"username": "janedoe2"}, follow_redirects=True)
    assert b"janedoe3" in resp.data


def test_admin_can_view_a_participants_self_registration_profile(client):
    test_client, app_module = client
    _Register(test_client, "janedoe")
    test_client.get("/logout")
    _AdminLogin(test_client)

    (participant_id,) = list(app_module.house.participants.keys())
    profile_resp = test_client.get(f"/admin/participant/{participant_id}")
    assert b"123 Main St" in profile_resp.data
    assert b"Smith" in profile_resp.data

    photo_resp = test_client.get(f"/admin/participant/{participant_id}/photo")
    assert photo_resp.status_code == 200
    assert photo_resp.data == b"not really a jpeg, just test bytes"


def test_admin_registration_routes_are_gone(client):
    test_client, _ = client
    _AdminLogin(test_client)
    resp = test_client.post("/admin/register", data={"name": "Someone"})
    assert resp.status_code == 404


def test_front_page_is_a_plain_login_for_a_logged_out_visitor(client):
    test_client, _ = client
    resp = test_client.get("/")
    assert resp.status_code == 200  # no redirect
    assert b'<form method="post" action="/login">' in resp.data
    assert b"Please log in first" not in resp.data


def test_front_page_is_the_dashboard_once_logged_in(client):
    test_client, _ = client
    _Register(test_client, "janedoe")
    resp = test_client.get("/")
    assert b"Welcome" not in resp.data  # that flash was from registering, not a fresh "/" hit
    assert b"Forum" in resp.data  # the logged-in nav, not the login form
