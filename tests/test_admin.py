"""
Sprint 20H — unit tests for the admin dashboard service.

The aggregation/query paths are DB-backed and covered by the manual e2e
(tests/e2e_admin_manual.py, run against Postgres). Here we cover the pure
validation guards in set_user_status, which run before any DB access, so they
need no session.
"""

from __future__ import annotations

import asyncio

import pytest

from app.admin.service import AdminError, set_user_status


def test_set_user_status_rejects_invalid_status():
    with pytest.raises(AdminError) as ei:
        asyncio.run(set_user_status(None, admin_id=1, user_id=2, status="DELETED"))
    assert ei.value.status_code == 400


def test_set_user_status_rejects_self_suspend():
    with pytest.raises(AdminError) as ei:
        asyncio.run(set_user_status(None, admin_id=7, user_id=7, status="SUSPENDED"))
    assert ei.value.status_code == 400
    assert "their own" in ei.value.detail


def test_admin_error_carries_status_and_detail():
    err = AdminError(404, "User not found")
    assert err.status_code == 404 and err.detail == "User not found"
    assert str(err) == "User not found"
