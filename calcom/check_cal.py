"""Check the Cal.diy connection from the Kodera side, before you switch the voice agent over.

    .venv/Scripts/python calcom/check_cal.py              # read-only: can we read free slots?
    .venv/Scripts/python calcom/check_cal.py --book-test  # also books the first slot, then cancels

Uses CAL_API_URL, CAL_API_KEY, CAL_EVENT_TYPE_ID from .env. From your own PC, set CAL_API_URL
to the public address, e.g. CAL_API_URL=https://cal.example.com/api (the /v2 path is added).
"""

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.business import appointments as appts  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.integrations.calcom import CalClient, CalError  # noqa: E402


async def main() -> int:
    s = Settings()
    key = s.cal_api_key.get_secret_value()
    if not key or not s.cal_event_type_id:
        print("Set CAL_API_KEY and CAL_EVENT_TYPE_ID in .env first.")
        return 2
    sched = appts.schedule_from_settings(s)
    client = CalClient(base_url=s.cal_api_url, api_key=key, event_type_id=s.cal_event_type_id,
                       timezone_name=s.clinic_timezone_name)  # fmt: skip
    try:
        found = None
        for offset in range(1, 15):  # first day with free slots in the next two weeks
            day = sched.now().date() + timedelta(days=offset)
            slots = await client.slots(day)
            if slots:
                found = (day, slots)
                break
        if not found:
            print("[FAIL] Cal.diy answered, but there are no free slots in the next 14 days.")
            print("       Check the availability schedule and that the event type is not hidden.")
            return 1
        day, slots = found
        times = ", ".join(f"{x.astimezone(sched.tz):%H:%M}" for x in slots[:6])
        print(f"[OK]   free slots on {day}: {times}  ({len(slots)} in total)")

        if "--book-test" in sys.argv:
            booking = await client.create_booking(
                start=slots[0],
                name="Kodera Test",
                phone_e164=f"{s.clinic_phone_country_code}9999999999",
                notes="Connection test, will be cancelled",
                metadata={"source": "check_cal"},
            )
            print(f"[OK]   booked {booking['uid']} (phone-only attendee works)")
            await client.cancel_booking(booking["uid"], "Connection test")
            print("[OK]   cancelled it again")
        print("\nCal.diy is reachable and answering as expected.")
        return 0
    except CalError as exc:
        print(f"[FAIL] {exc}")
        if exc.status == 401:
            print("       The API key is wrong or expired (Settings > Developer > API keys).")
        if exc.status == 400 and "--book-test" in sys.argv:
            print("       Phone-only booking is probably off: in the event type set Email to")
            print("       optional and Phone number to required (Advanced > Booking questions).")
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
