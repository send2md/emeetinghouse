"""Tests for the self-registration flow in app.py.

app.py reads its DB path and secrets from environment variables at
import time and keeps a single in-process `house`/`conn`, so each test
here reloads the module against a fresh temp database rather than
sharing state between tests.
"""

from __future__ import annotations

import importlib
import os
import re

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EMEETINGHOUSE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EMEETINGHOUSE_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("EMEETINGHOUSE_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("EMEETINGHOUSE_INVITE_DAYS", "7")

    import app as app_module

    importlib.reload(app_module)
    app_module.app.testing = True
    with app_module.app.test_client() as test_client:
        yield test_client, app_module


def _ExtractSetupLink(html: str) -> str:
    match = re.search(r"/register/([A-Za-z0-9_-]+)", html)
    assert match, f"no setup link found in response:\n{html}"
    return match.group(0)


def _AdminLogin(client):
    return client.post("/admin/login", data={"password": "test-admin-password"}, follow_redirects=True)


def test_admin_register_issues_a_link_with_no_password_field(client):
    test_client, _ = client
    _AdminLogin(test_client)
    resp = test_client.post("/admin/register", data={"name": "Jane Doe"}, follow_redirects=True)
    assert b'name="password"' not in resp.data  # the admin never types one in
    assert b"Setup link" in resp.data
    assert b"Registered Jane Doe" in resp.data


def test_full_self_registration_flow_logs_the_participant_in(client):
    test_client, app_module = client
    _AdminLogin(test_client)
    admin_resp = test_client.post("/admin/register", data={"name": "Jane Doe"}, follow_redirects=True)
    link = _ExtractSetupLink(admin_resp.data.decode())

    # The admin's own session never submits a username/password anywhere.
    setup_page = test_client.get(link)
    assert b"Jane Doe" in setup_page.data

    setup_resp = test_client.post(
        link, data={"username": "janedoe", "password": "correct horse battery"}, follow_redirects=True
    )
    assert b"Welcome, Jane Doe" in setup_resp.data

    # The link is single-use: visiting it again is rejected.
    reused = test_client.get(link, follow_redirects=True)
    assert b"invalid or has already been used" in reused.data

    # And the chosen password actually works for a normal login.
    test_client.get("/logout")
    login_resp = test_client.post(
        "/login", data={"username": "janedoe", "password": "correct horse battery"}, follow_redirects=True
    )
    assert b"Welcome, Jane Doe" in login_resp.data


def test_self_registration_rejects_a_taken_username_without_consuming_the_link(client):
    test_client, _ = client
    _AdminLogin(test_client)

    first = test_client.post("/admin/register", data={"name": "Jane Doe"}, follow_redirects=True)
    first_link = _ExtractSetupLink(first.data.decode())
    test_client.post(first_link, data={"username": "sameuser", "password": "correct horse battery"})

    second = test_client.post("/admin/register", data={"name": "John Roe"}, follow_redirects=True)
    second_link = _ExtractSetupLink(second.data.decode())
    clash = test_client.post(
        second_link, data={"username": "sameuser", "password": "another good password"}, follow_redirects=True
    )
    assert b"already taken" in clash.data

    # The link is still good — John can try again with a free username.
    retry = test_client.post(
        second_link, data={"username": "johnroe", "password": "another good password"}, follow_redirects=True
    )
    assert b"Welcome, John Roe" in retry.data


def test_admin_invite_refuses_once_credentials_exist(client):
    test_client, app_module = client
    _AdminLogin(test_client)

    resp = test_client.post("/admin/register", data={"name": "Jane Doe"}, follow_redirects=True)
    link = _ExtractSetupLink(resp.data.decode())
    test_client.post(link, data={"username": "janedoe", "password": "correct horse battery"})

    (participant_id,) = list(app_module.house.participants.keys())
    invite_resp = test_client.post("/admin/invite", data={"participant_id": participant_id}, follow_redirects=True)
    assert b"already has a login" in invite_resp.data


def test_expired_setup_link_is_rejected(client):
    test_client, app_module = client
    _AdminLogin(test_client)

    resp = test_client.post("/admin/register", data={"name": "Jane Doe"}, follow_redirects=True)
    link = _ExtractSetupLink(resp.data.decode())
    token = link.rsplit("/", 1)[-1]

    # Back-date the invite past its lifetime instead of waiting a week.
    stale = app_module.Now() - app_module.INVITE_LIFETIME - app_module.dt.timedelta(seconds=1)
    app_module.conn.execute(
        "UPDATE invites SET created_at = ? WHERE token = ?", (stale.isoformat(), token)
    )
    app_module.conn.commit()

    expired = test_client.get(link, follow_redirects=True)
    assert b"expired" in expired.data
