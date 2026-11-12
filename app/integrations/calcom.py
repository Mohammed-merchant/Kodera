"""Client for the Cal.diy API v2 (self-hosted scheduling).

Only the three calls the voice agent needs:

    GET  /v2/slots                     free slots of an event type   (cal-api-version 2024-09-04)
    POST /v2/bookings                  create a booking              (cal-api-version 2024-08-13)
    POST /v2/bookings/{uid}/cancel     cancel a booking              (cal-api-version 2024-08-13)

Formats are taken from the Cal.diy source (apps/api/v2, packages/platform/types).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

SLOTS_VERSION = "2024-09-04"
BOOKINGS_VERSION = "2024-08-13"


class CalError(Exception):
    """Cal.diy refused (4xx: `rejected` is True) or could not be reached / failed (5xx)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def rejected(self) -> bool:
        """The request itself was refused (slot taken, bad data): retrying will not help."""
        return self.status is not None and 400 <= self.status < 500


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class CalClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        event_type_id: int,
        timezone_name: str,
        timeout: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.event_type_id = event_type_id
        self.timezone_name = timezone_name
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, *, version: str, **kwargs: Any) -> Any:
        headers = {"cal-api-version": version}
        try:
            resp = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise CalError(f"Cal.diy is not reachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:300]
            log.warning("cal_request_failed", path=path, status=resp.status_code, detail=detail)
            raise CalError(
                f"Cal.diy answered {resp.status_code}: {detail}", status=resp.status_code
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise CalError("Cal.diy sent an unreadable answer", status=resp.status_code) from exc
        if isinstance(body, dict) and body.get("status") == "error":
            raise CalError(f"Cal.diy error: {str(body.get('error'))[:300]}", status=400)
        return body.get("data") if isinstance(body, dict) else body

    async def slots(self, day: date) -> list[datetime]:
        """Free slot start times on one day (timezone-aware), earliest first."""
        data = await self._call(
            "GET",
            "/v2/slots",
            version=SLOTS_VERSION,
            params={
                "eventTypeId": self.event_type_id,
                "start": day.isoformat(),
                "end": (day + timedelta(days=1)).isoformat(),
                "timeZone": self.timezone_name,
            },
        )
        out: list[datetime] = []
        for items in (data or {}).values():
            for item in items:
                raw = item.get("start") if isinstance(item, dict) else item
                if raw:
                    out.append(_parse_time(str(raw)))
        return sorted(out)

    async def create_booking(
        self,
        *,
        start: datetime,
        name: str,
        phone_e164: str,
        notes: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        """Book the slot. Returns Cal.diy's booking (it has `uid`, `start`, `status`)."""
        body: dict[str, Any] = {
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "eventTypeId": self.event_type_id,
            "attendee": {
                "name": name,
                "timeZone": self.timezone_name,
                "phoneNumber": phone_e164,
                "language": "en",
            },
            "metadata": metadata,
        }
        if notes:
            body["bookingFieldsResponses"] = {"notes": notes}
        data = await self._call("POST", "/v2/bookings", version=BOOKINGS_VERSION, json=body)
        if not isinstance(data, dict) or not data.get("uid"):
            raise CalError("Cal.diy created a booking but sent no booking id")
        return data

    async def cancel_booking(self, uid: str, reason: str) -> None:
        await self._call(
            "POST",
            f"/v2/bookings/{uid}/cancel",
            version=BOOKINGS_VERSION,
            json={"cancellationReason": reason},
        )
