"""Tests for monitor.persistence (tmp SQLite)"""
from __future__ import annotations

import sqlite3
import types

import pytest

from monitor.domain import (
    AlertDeliveryStatus,
    AlertRecord,
    AlertType,
)
from monitor.persistence import (
    AlertRepository,
    Database,
    SnapshotRepository,
    new_alert_id,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.sqlite"


@pytest.fixture
def database(db_path):
    # Duck-typing контракт DatabaseParams: те же поля, что в storage settings.yaml.
    # Избавляет от зависимости от точной сигнатуры/экспорта DatabaseParams.
    params = types.SimpleNamespace(
        mode="sqlite",
        sqlite_path=str(db_path),
        save_raw_responses=True,
        retention_days=90,
        save_snapshots=True,
        save_alerts=True,
    )
    db = Database(params)
    yield db
    db.close()


@pytest.fixture
def snapshot_repository(database):
    return SnapshotRepository(database)


@pytest.fixture
def alert_repository(database):
    return AlertRepository(database)


def _count(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return count


class TestNewAlertId:
    def test_unique(self):
        ids = {new_alert_id() for _ in range(100)}
        assert len(ids) == 100

    def test_string(self):
        assert isinstance(new_alert_id(), str)


class TestDatabase:
    def test_creates_tables(self, db_path, database):
        conn = sqlite3.connect(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
        conn.close()
        assert "market_snapshots" in tables
        assert "funding_snapshots" in tables
        assert "quality_reports" in tables
        assert "metrics" in tables
        assert "signal_decisions" in tables
        assert "alerts" in tables

    def test_cleanup_on_empty_db(self, database):
        database.cleanup_old_records(retention_days=90)


class TestSnapshotRepository:
    def test_save_market_snapshot(
        self, db_path, snapshot_repository, market_snapshot,
    ):
        snapshot_repository.save_market_snapshot(market_snapshot)
        assert _count(db_path, "market_snapshots") == 1

    def test_save_funding_snapshot(
        self, db_path, snapshot_repository, funding_snapshot,
    ):
        snapshot_repository.save_funding_snapshot(funding_snapshot)
        assert _count(db_path, "funding_snapshots") == 1

    def test_save_quality_report(
        self, db_path, snapshot_repository, quality_report_ok,
    ):
        snapshot_repository.save_quality_report(
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            quality_report=quality_report_ok,
        )
        assert _count(db_path, "quality_reports") == 1

    def test_save_metrics(
        self,
        db_path,
        snapshot_repository,
        basis_metrics,
        funding_yield_metrics,
        cost_metrics,
        net_yield_metrics,
    ):
        snapshot_repository.save_metrics(
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            basis_metrics=basis_metrics,
            funding_yield_metrics=funding_yield_metrics,
            cost_metrics=cost_metrics,
            net_yield_metrics=net_yield_metrics,
            calculated_at_ms=1710000000500,
        )
        assert _count(db_path, "metrics") == 1

    def test_save_signal_decision(
        self, db_path, snapshot_repository, signal_decision_normal,
    ):
        snapshot_repository.save_signal_decision(signal_decision_normal)
        assert _count(db_path, "signal_decisions") == 1


class TestAlertRepository:
    def _make_record(self, status=AlertDeliveryStatus.SENT):
        return AlertRecord(
            alert_id=new_alert_id(),
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            alert_type=AlertType.SIGNAL,
            delivery_status=status,
            created_at_ms=1710000000000,
            sent_at_ms=1710000000000,
            message_payload="test message",
            error_message=None,
        )

    def test_save_alert(self, db_path, alert_repository):
        alert_repository.save_alert(self._make_record())
        assert _count(db_path, "alerts") == 1

    def test_save_failed_alert(self, db_path, alert_repository):
        record = AlertRecord(
            alert_id=new_alert_id(),
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            alert_type=AlertType.WARNING,
            delivery_status=AlertDeliveryStatus.FAILED,
            created_at_ms=1710000000000,
            sent_at_ms=None,
            message_payload="warn",
            error_message="timeout",
        )
        alert_repository.save_alert(record)
        assert _count(db_path, "alerts") == 1

    def test_save_suppressed_alert(self, db_path, alert_repository):
        record = AlertRecord(
            alert_id=new_alert_id(),
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            alert_type=AlertType.SIGNAL,
            delivery_status=AlertDeliveryStatus.SUPPRESSED,
            created_at_ms=1710000000000,
            sent_at_ms=None,
            message_payload="suppressed",
            error_message=None,
        )
        alert_repository.save_alert(record)
        assert _count(db_path, "alerts") == 1

    def test_save_pending_alert(self, db_path, alert_repository):
        record = AlertRecord(
            alert_id=new_alert_id(),
            cycle_id="test-cycle",
            symbol_name="BTC_CARRY",
            alert_type=AlertType.SIGNAL,
            delivery_status=AlertDeliveryStatus.PENDING,
            created_at_ms=1710000000000,
            sent_at_ms=None,
            message_payload="pending",
            error_message=None,
        )
        alert_repository.save_alert(record)
        assert _count(db_path, "alerts") == 1

    def test_cleanup_removes_old_alerts(
        self, database, alert_repository, db_path,
    ):
        old_record = AlertRecord(
            alert_id=new_alert_id(),
            cycle_id="old-cycle",
            symbol_name="BTC_CARRY",
            alert_type=AlertType.SIGNAL,
            delivery_status=AlertDeliveryStatus.SENT,
            created_at_ms=1,  # epoch — очень старая запись
            sent_at_ms=1,
            message_payload="old",
            error_message=None,
        )
        alert_repository.save_alert(old_record)
        database.cleanup_old_records(retention_days=90)
        assert _count(db_path, "alerts") == 0