from datetime import datetime, timedelta

from app.message_queue_tasks.write_neo4j_results import cleanup_due

NOW = datetime(2026, 10, 27, 12, 0)


def test_never_cleaned_is_due():
    assert cleanup_due(None, 30, NOW)


def test_due_once_the_interval_has_passed():
    assert not cleanup_due(NOW - timedelta(days=29), 30, NOW)
    assert cleanup_due(NOW - timedelta(days=30), 30, NOW)


def test_a_non_positive_interval_leaves_it_to_forced_runs():
    assert not cleanup_due(None, 0, NOW)
    assert not cleanup_due(NOW - timedelta(days=400), -1, NOW)
