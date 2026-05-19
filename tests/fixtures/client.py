import hashlib
import pytest
from fastapi.testclient import TestClient
from src.common.core.dependencies import SINGLE_TENANT_UUID
from src.common.models.db.tenant import TenantApiKey
from src.main import get_app
from src.common.models.db.alert import Alert, LastAlert

@pytest.fixture
def test_app(request, monkeypatch, db_session):
    from src.workflowmanager.workflowmanager import WorkflowManager
    WorkflowManager._instance = None
    
    # If parameters are passed via indirect
    if hasattr(request, "param") and isinstance(request, pytest.FixtureRequest):
        config_overrides = request.param
        if isinstance(config_overrides, dict):
            for key, value in config_overrides.items():
                monkeypatch.setenv(key, str(value))
    
    # Reload config to pick up monkeypatched env vars
    from src.common.core.config import config
    config.config = None # Force reload
    
    app = get_app()
    yield app
    WorkflowManager._instance = None

@pytest.fixture
def client(test_app, db_session):
    with TestClient(test_app) as test_client:
        yield test_client

@pytest.fixture
def create_alert(db_session):
    def _create_alert(fingerprint, status, last_received=None, extra_event_data=None):
        from src.common.models.alert import AlertStatus
        from datetime import datetime, timezone
        from uuid import uuid4
        
        extra_event_data = extra_event_data or {}
        
        alert_timestamp = last_received if last_received else datetime.now(tz=timezone.utc)
        
        # 1. Update/Create LastAlert first
        last_alert = db_session.query(LastAlert).filter_by(
            tenant_id=SINGLE_TENANT_UUID,
            fingerprint=fingerprint
        ).first()
        
        if last_alert:
            # Get the previous status from the latest alert associated with this LastAlert
            prev_alert = db_session.query(Alert).get(last_alert.alert_id)
            prev_status = prev_alert.status if prev_alert else None
            
            # If it was resolved and now is firing, reset first_timestamp
            if prev_status == AlertStatus.RESOLVED.value and status == AlertStatus.FIRING:
                last_alert.first_timestamp = alert_timestamp
            
            last_alert.timestamp = alert_timestamp
        else:
            last_alert = LastAlert(
                tenant_id=SINGLE_TENANT_UUID,
                fingerprint=fingerprint,
                alert_id=uuid4(), # Placeholder, will update later
                timestamp=alert_timestamp,
                first_timestamp=alert_timestamp,
            )
            db_session.add(last_alert)
        
        db_session.flush() # Ensure it's in DB
        
        # 2. Now calculate firing_start_time based on the updated LastAlert
        firing_start_time = alert_timestamp
        if status == AlertStatus.FIRING:
            firing_start_time = last_alert.first_timestamp
            
        alert_data = {
            "fingerprint": fingerprint,
            "status": status.value if hasattr(status, "value") else status,
            "last_received": alert_timestamp.isoformat(),
            "firing_start_time": firing_start_time.isoformat() if firing_start_time else None,
            "name": "test-alert"
        }
        alert_data.update(extra_event_data)
        
        # Extract native fields and put others in extra_data
        native_fields = [
            "id", "tenant_id", "timestamp", "provider_type", "provider_id",
            "application", "object", "node_name", "severity", "message",
            "operator", "time_created", "network", "timezone", "custom_key",
            "expiry_in_minutes", "source", "service", "key_field", "name",
            "status", "description", "last_received", "is_full_duplicate",
            "is_partial_duplicate", "duplicate_reason", "note", "assignee",
            "incident", "dismiss_until", "dismissed", "enriched_fields",
            "started_at", "firing_counter", "unresolved_counter", "firing_start_time",
            "firing_start_time_since_last_resolved", "extra_data", "fingerprint",
            "alert_hash"
        ]
        
        alert_init_data = {
            "tenant_id": SINGLE_TENANT_UUID,
            "provider_type": "mock",
            "provider_id": "mock",
            "fingerprint": fingerprint,
            "timestamp": alert_timestamp,
        }
        extra_data = {}
        for k, v in alert_data.items():
            if k in native_fields:
                alert_init_data[k] = v
            else:
                extra_data[k] = v
        
        if extra_data:
            alert_init_data["extra_data"] = extra_data

        alert = Alert(**alert_init_data)
        db_session.add(alert)
        db_session.flush()
        
        # 3. Update LastAlert with the real alert ID
        last_alert.alert_id = alert.id
        db_session.commit()
        return alert
        
    return _create_alert

def setup_api_key(
    db_session, api_key_value, tenant_id=SINGLE_TENANT_UUID, role="admin"
):
    hash_api_key = hashlib.sha256(api_key_value.encode()).hexdigest()
    db_session.add(
        TenantApiKey(
            tenant_id=tenant_id,
            reference_id="test_api_key",
            key_hash=hash_api_key,
            created_by="admin@keephq",
            role=role,
        )
    )
    db_session.commit()
