"""
Phase 4 product-BI: workflow execution status counter is incremented exactly
once, with the correct bounded status label, at the single terminal chokepoint
(_finish_workflow_execution).
"""

import logging
from unittest.mock import patch

import src.workflowmanager.workflowscheduler as sched


def _make_scheduler():
    # Build a bare instance without running __init__ (which needs a manager).
    stub = sched.WorkflowScheduler.__new__(sched.WorkflowScheduler)
    stub.logger = logging.getLogger("test-scheduler")
    return stub


def _run_finish(status):
    stub = _make_scheduler()
    with patch.object(sched, "finish_workflow_execution_db") as mock_db, patch.object(
        sched, "workflow_execution_status"
    ) as mock_metric:
        stub._finish_workflow_execution(
            tenant_id="keep",
            workflow_id="wf-1",
            workflow_execution_id="ex-1",
            status=status,
            error=None,
        )
    return mock_db, mock_metric


def test_finish_records_success_status_once():
    mock_db, mock_metric = _run_finish(sched.WorkflowStatus.SUCCESS)
    mock_metric.labels.assert_called_once_with(
        tenant_id="keep", workflow_id="wf-1", status="success"
    )
    mock_metric.labels.return_value.inc.assert_called_once()
    mock_db.assert_called_once()


def test_finish_records_error_status_once():
    _, mock_metric = _run_finish(sched.WorkflowStatus.ERROR)
    mock_metric.labels.assert_called_once_with(
        tenant_id="keep", workflow_id="wf-1", status="error"
    )
    mock_metric.labels.return_value.inc.assert_called_once()


def test_finish_records_providers_not_configured_status():
    _, mock_metric = _run_finish(sched.WorkflowStatus.PROVIDERS_NOT_CONFIGURED)
    mock_metric.labels.assert_called_once_with(
        tenant_id="keep", workflow_id="wf-1", status="providers_not_configured"
    )


def test_metric_failure_does_not_break_finish():
    """A metric error must not prevent the DB finish from being written."""
    stub = _make_scheduler()
    with patch.object(sched, "finish_workflow_execution_db") as mock_db, patch.object(
        sched, "workflow_execution_status"
    ) as mock_metric:
        mock_metric.labels.side_effect = RuntimeError("prom down")
        stub._finish_workflow_execution(
            tenant_id="keep",
            workflow_id="wf-1",
            workflow_execution_id="ex-1",
            status=sched.WorkflowStatus.SUCCESS,
        )
    mock_db.assert_called_once()
