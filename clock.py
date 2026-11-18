"""
VirtualClock — single authoritative time source for the replay harness.

Uses freezegun to patch all datetime.now / time.time / time.sleep call sites
globally, including modules that do `from datetime import datetime`.

Usage:
    clock = VirtualClock()
    clock.start(start_dt)          # freeze time at start_dt
    clock.advance_to(next_dt)      # jump to next candle boundary
    clock.stop()                   # restore real time
"""

from datetime import datetime, timedelta, timezone

from freezegun import freeze_time
from freezegun.api import FakeDatetime, StepTickTimeFactory


class VirtualClock:
    def __init__(self) -> None:
        self._freeze_ctx = None
        self._frozen = None
        self._current: datetime | None = None

    def start(self, start_dt: datetime, real_time_modules: tuple[str, ...] = ()) -> None:
        """Freeze time at start_dt.

        real_time_modules: module-name prefixes that keep seeing wall-clock
        time (freezegun's `ignore`).  Used for the in-process FreqUI server,
        whose JWT tokens must expire in real minutes, not virtual ones —
        otherwise a 15-minute token dies within a second of replay time.
        """
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        self._current = start_dt
        # real_asyncio: asyncio event loops (uvicorn's) keep a real monotonic
        # clock, otherwise every asyncio.sleep()/timeout in the API server
        # thread waits for virtual time and its shutdown hangs forever.
        self._freeze_ctx = freeze_time(
            start_dt,
            ignore=list(real_time_modules),
            real_asyncio=bool(real_time_modules),
        )
        self._frozen = self._freeze_ctx.start()

    def advance_to(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._current = dt
        self._frozen.move_to(dt)

    def now(self) -> datetime:
        return self._current

    def stop(self) -> None:
        if self._freeze_ctx is not None:
            self._freeze_ctx.stop()
            self._freeze_ctx = None
            self._frozen = None
            self._current = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()
