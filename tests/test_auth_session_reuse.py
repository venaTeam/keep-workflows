"""Regression tests for request-scoped DB session reuse during authentication.

These guard the fix that threads the FastAPI ``Depends(get_session)`` session
through API-key authentication so a request no longer opens extra, avoidable
SQLAlchemy sessions just to authenticate.
"""

import pytest
from sqlmodel import Session

from src.common.core.db import get_session
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

VALID_API_KEY = "session-reuse-api-key"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_api_key_auth_reuses_request_scoped_session(
    monkeypatch, db_session, client, test_app
):
    """API-key authentication must reuse the request's ``get_session`` session.

    Before the fix, ``get_api_key`` was called without a session and opened its
    own ``Session(engine)``. After the fix it receives the same session object
    that the request-scoped ``Depends(get_session)`` dependency yields.
    """
    setup_api_key(db_session, VALID_API_KEY)

    # Capture the session yielded by the request-scoped dependency.
    recorded = {}

    def override_get_session():
        gen = get_session()
        session = next(gen)
        recorded["request_session"] = session
        try:
            yield session
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    test_app.dependency_overrides[get_session] = override_get_session

    # Spy on the session that authentication hands to get_api_key.
    from src.identitymanager.identity_managers.noauth import (
        noauth_authverifier as noauth_module,
    )

    captured = {}
    real_get_api_key = noauth_module.get_api_key

    def spy_get_api_key(api_key, *args, session=None, **kwargs):
        captured["session"] = session
        return real_get_api_key(api_key, *args, session=session, **kwargs)

    monkeypatch.setattr(noauth_module, "get_api_key", spy_get_api_key)

    try:
        response = client.get("/workflows", headers={"x-api-key": VALID_API_KEY})
    finally:
        test_app.dependency_overrides.pop(get_session, None)

    # Authentication ran and looked up the key.
    assert response.status_code == 200
    assert "session" in captured

    # The key lookup reused the request-scoped session instead of opening
    # its own, and it is the exact same session object the dependency yielded.
    assert isinstance(captured["session"], Session)
    assert captured["session"] is recorded["request_session"]
