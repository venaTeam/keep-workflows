import pytest

from src.common.models.alert import AlertDto, AlertEnvironment
from src.contextmanager.contextmanager import ContextManager
from src.iohandler.iohandler import IOHandler


def _alert(**kwargs):
    """Build an AlertDto with the minimal required fields."""
    defaults = {
        "name": "env alert",
        "status": "firing",
        "severity": "critical",
        "last_received": "2024-01-01T00:00:00.000Z",
    }
    defaults.update(kwargs)
    return AlertDto(**defaults)


def test_environment_defaults_to_production():
    """A missing environment resolves to 'production'."""
    assert _alert().environment == AlertEnvironment.PRODUCTION.value


@pytest.mark.parametrize(
    "value", ["production", "integration", "load", "development", "test"]
)
def test_environment_valid_values_preserved(value):
    """Every valid enum value passes through unchanged."""
    assert _alert(environment=value).environment == value


@pytest.mark.parametrize("value", ["", "bogus", "PRODUCTION", "prod", None])
def test_environment_invalid_falls_back_to_production(value):
    """Empty/invalid environment values fall back to 'production'."""
    assert _alert(environment=value).environment == AlertEnvironment.PRODUCTION.value


def _render_environment(alert):
    cm = ContextManager(tenant_id="keep")
    cm.set_event_context(alert)
    return IOHandler(cm).render("{{ alert.environment }}")


def test_workflow_template_reads_environment():
    """Workflow YAML templates can reference {{ alert.environment }}."""
    assert _render_environment(_alert(environment="test")) == "test"


def test_workflow_template_environment_defaults_to_production():
    """{{ alert.environment }} resolves to the 'production' default when unset."""
    assert _render_environment(_alert()) == "production"
