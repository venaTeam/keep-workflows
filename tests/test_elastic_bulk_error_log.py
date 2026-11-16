import logging
from unittest.mock import MagicMock, patch

import pytest
from elasticsearch.helpers import BulkIndexError

from src.common.core.elastic import ElasticClient, _first_bulk_error

DOC = {"name": "x" * 1000}
ERRORS = [
    {
        "index": {
            "_id": f"fp-{i}",
            "status": 400,
            "error": {"type": "mapper_parsing_exception", "reason": "bad field"},
            "data": DOC,
        }
    }
    for i in range(3)
]


def test_first_bulk_error_drops_document():
    assert _first_bulk_error(ERRORS) == {
        "id": "fp-0",
        "status": 400,
        "error": {"type": "mapper_parsing_exception", "reason": "bad field"},
    }
    assert _first_bulk_error([]) is None


def test_bulk_failure_logs_count_not_documents(caplog):
    client = ElasticClient.__new__(ElasticClient)
    client.enabled = True
    client.tenant_id = "t1"
    client.refresh_strategy = "false"
    client._client = MagicMock()
    client.logger = logging.getLogger("test-elastic")

    with patch(
        "src.common.core.elastic.bulk",
        side_effect=BulkIndexError("3 document(s) failed to index.", ERRORS),
    ), caplog.at_level(logging.ERROR, logger="test-elastic"):
        with pytest.raises(Exception):
            client.index_alerts([])

    record = caplog.records[-1]
    assert record.failed_count == 3
    assert record.first_error["id"] == "fp-0"
    assert not hasattr(record, "errors")
