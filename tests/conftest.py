import os
import random
import tempfile
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from src.common.core.dependencies import SINGLE_TENANT_UUID
from src.common.models.db.tenant import Tenant
from src.common.models.db.workflow import Workflow, WorkflowExecution, WorkflowVersion, WorkflowToAlertExecution
from src.common.models.db.provider import Provider, ProviderExecutionLog
from src.common.models.db.preset import Preset
from unittest.mock import patch
import logging

# Set up a temporary directory for secret manager if needed
if "SECRET_MANAGER_DIRECTORY" not in os.environ:
    os.environ["SECRET_MANAGER_DIRECTORY"] = tempfile.mkdtemp(prefix="keep_secrets_")


@pytest.fixture(autouse=True)
def setup_logging():
    logging.basicConfig(level=logging.DEBUG)

@pytest.fixture(autouse=True)
def disable_db_logging(monkeypatch):
    monkeypatch.setattr("src.common.logging.KEEP_STORE_WORKFLOW_LOGS", False)

@pytest.fixture
def db_session(request, monkeypatch, tmp_path):
    # Use a real file for the database to ensure it's shared correctly across threads
    db_file = tmp_path / "test.db"
    db_connection_string = f"sqlite:///{db_file}"
    mock_engine = create_engine(
        db_connection_string,
        connect_args={"check_same_thread": False},
    )

    # Ensure all models are registered
    from src.common.models.db import tenant, workflow, provider, preset, alert
    
    SQLModel.metadata.create_all(mock_engine)

    # Create a session
    SessionLocal = sessionmaker(
        class_=Session, autocommit=False, autoflush=False, bind=mock_engine
    )
    session = SessionLocal()

    # Prepopulate the database with test data
    tenant_obj = Tenant(id=SINGLE_TENANT_UUID, name="test-tenant", created_by="tests@keephq.dev")
    session.add(tenant_obj)
    session.commit()

    # Patch the engine in the app
    # We patch it in multiple places just to be sure
    patchers = [
        patch("src.common.core.db.engine", mock_engine),
        patch("src.workflowmanager.workflowmanager.WorkflowStore.get_all_workflows", 
              side_effect=lambda tenant_id, exclude_disabled=False: session.exec(
                  select(Workflow).where(Workflow.tenant_id == tenant_id, Workflow.is_disabled == (True if exclude_disabled else Workflow.is_disabled))
              ).all()
        )
    ]
    # Actually, let's just patch the engine and see. 
    # If the engine is patched, all functions using it should see the same DB.
    
    with patch("src.common.core.db.engine", mock_engine):
        yield session

    session.close()
    mock_engine.dispose()

@pytest.fixture(autouse=True)
def mocked_context_manager():
    # Many tests might expect a mocked context manager
    with patch("src.contextmanager.contextmanager.ContextManager") as mock:
        yield mock

# Register fixture plugins
pytest_plugins = ["tests.fixtures.client", "tests.fixtures.workflow_manager"]
