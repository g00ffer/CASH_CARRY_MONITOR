from __future__ import annotations

import uuid

from monitor.domain import (
    AlertDeliveryStatus,
    AlertRecord,
    AlertType,
)
from monitor.utils import utc_now_ms

from .database import (
    Database,
    to_json,
)


def new_alert_id() -> str:
    """
    Generate unique alert id.
    """

    return str(uuid.uuid4())


class AlertRepository:
    """
    Repository for sent/suppressed/failed alerts.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # Save alert record
    # ------------------------------------------------------------------

    def save_alert(
        self,
        alert: AlertRecord,
    ) -> int:
        """
        Save alert record.

        Uses INSERT OR IGNORE to avoid duplicates by alert_id.
        """

        alert_type_value = (
            alert.alert_type.value
            if isinstance(alert.alert_type, AlertType)
            else str(alert.alert_type)
        )

        delivery_status_value = (
            alert.delivery_status.value
            if isinstance(alert.delivery_status, AlertDeliveryStatus)
            else str(alert.delivery_status)
        )

        if alert.message_payload is None:
            message_payload_text = None
        elif isinstance(alert.message_payload, str):
            message_payload_text = alert.message_payload
        else:
            message_payload_text = to_json(alert.message_payload)

        return self._db.execute(
            """
            INSERT OR IGNORE INTO alerts (
                alert_id,
                cycle_id,
                symbol_name,
                alert_type,
                delivery_status,
                created_at_ms,
                sent_at_ms,
                message_payload,
                error_message,
                inserted_at_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.alert_id,
                alert.cycle_id,
                alert.symbol_name,
                alert_type_value,
                delivery_status_value,
                alert.created_at_ms,
                alert.sent_at_ms,
                message_payload_text,
                alert.error_message,
                utc_now_ms(),
            ),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_last_sent_alert_ms(
        self,
        symbol_name: str,
        alert_type: AlertType | str,
    ) -> int | None:
        """
        Return timestamp of last successfully sent alert for symbol/type.
        """

        alert_type_value = (
            alert_type.value
            if isinstance(alert_type, AlertType)
            else str(alert_type)
        )

        row = self._db.fetch_one(
            """
            SELECT MAX(sent_at_ms) AS last_sent_ms
            FROM alerts
            WHERE symbol_name = ?
              AND alert_type = ?
              AND delivery_status = ?
            """,
            (
                symbol_name,
                alert_type_value,
                AlertDeliveryStatus.SENT.value,
            ),
        )

        if row is None:
            return None

        return row.get("last_sent_ms")

    def was_recent_alert(
        self,
        *,
        symbol_name: str,
        alert_type: AlertType | str,
        now_ms: int,
        window_sec: int,
    ) -> bool:
        """
        Check whether alert of given type was sent recently.
        """

        if window_sec <= 0:
            return False

        last_sent_ms = self.get_last_sent_alert_ms(
            symbol_name=symbol_name,
            alert_type=alert_type,
        )

        if last_sent_ms is None:
            return False

        return (int(now_ms) - int(last_sent_ms)) < window_sec * 1000

    def count_alerts_since(
        self,
        since_ms: int,
    ) -> int:
        """
        Count alerts inserted since given timestamp.

        Useful for heartbeat stats.
        """

        row = self._db.fetch_one(
            """
            SELECT COUNT(*) AS cnt
            FROM alerts
            WHERE inserted_at_ms >= ?
            """,
            (int(since_ms),),
        )

        if row is None:
            return 0

        return int(row.get("cnt", 0))
