from typing import Any


class FakeRedis:
    """Tiny in-memory stand-in for the Redis calls the app makes."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}

    async def set(
        self, key: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def xadd(
        self, name: str, fields: dict[str, str], maxlen: int | None = None, approximate: bool = True
    ) -> str:
        entries = self.streams.setdefault(name, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, dict(fields)))
        return entry_id

    async def aclose(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:  # fail loudly if the app calls something unexpected
        raise AttributeError(name)
