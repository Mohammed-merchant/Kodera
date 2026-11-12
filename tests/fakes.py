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

    async def delete(self, *names: str) -> int:
        return sum(self.keys.pop(n, None) is not None for n in names)

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


class RecorderRedis:
    """In-memory Redis with the commands the call recorder and the save job use."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.plain: dict[str, str] = {}

    # hashes
    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> int:
        h = self.hashes.setdefault(key, {})
        if field is not None:
            h[field] = str(value)
        for k, v in (mapping or {}).items():
            h[k] = str(v)
        return 1

    async def hgetall(self, key: str) -> dict[bytes, bytes]:  # real Redis returns bytes
        return {k.encode(): v.encode() for k, v in self.hashes.get(key, {}).items()}

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    # lists
    async def lrange(self, key: str, start: int, stop: int) -> list[bytes]:
        return [x.encode() for x in self.lists.get(key, [])]

    # plain keys
    async def set(
        self, key: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and key in self.plain:
            return None
        self.plain[key] = value
        return True

    async def delete(self, *names: str) -> int:
        count = 0
        for n in names:
            for store in (self.hashes, self.lists, self.plain):
                count += store.pop(n, None) is not None  # type: ignore[arg-type]
        return count

    async def scan_iter(self, match: str = "*", count: int = 100):  # type: ignore[no-untyped-def]
        import fnmatch

        for key in list(self.hashes):
            if fnmatch.fnmatch(key, match):
                yield key.encode()

    def pipeline(self) -> "RecorderRedis._Pipe":
        return RecorderRedis._Pipe(self)

    class _Pipe:
        def __init__(self, parent: "RecorderRedis") -> None:
            self.parent, self.ops = parent, []

        def rpush(self, key: str, value: str) -> None:
            self.ops.append(lambda: self.parent.lists.setdefault(key, []).append(value))

        def expire(self, key: str, seconds: int) -> None:
            return None

        def hset(self, key: str, field: str, value: str) -> None:
            self.ops.append(
                lambda: self.parent.hashes.setdefault(key, {}).__setitem__(field, str(value))
            )

        async def execute(self) -> list[int]:
            for op in self.ops:
                op()
            return []

    async def aclose(self) -> None:
        return None
