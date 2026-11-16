"""A go2rtc client double the library's locked go2rtc API accepts.

``async_ensure_stream`` and ``async_remove_stream`` take the client's own
per-server write lock and call its unlocked ``_put``/``_delete``/``_get``, so a
bare mock no longer stands in for a client. This is a real ``Go2rtcClient``
whose HTTP primitives forward to the public methods, each an ``AsyncMock`` a
test can configure and assert on as before: a PUT arrives as
``ensure_stream(name, src)``, a DELETE as ``remove_stream(name)`` and a stream
list as ``list_streams()``. ``has_stream`` answers from ``list_streams``, as
the real one does, until a test sets its return value.
"""

from unittest.mock import AsyncMock, MagicMock

from aidot_cameras.camera.go2rtc import DEFAULT_BASE_URL, Go2rtcClient


class Go2rtcDouble(Go2rtcClient):
    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        super().__init__(MagicMock(), base_url)
        self.ensure_stream = AsyncMock(return_value=True)
        self.remove_stream = AsyncMock(return_value=True)
        self.list_streams = AsyncMock(return_value={})
        self.available = AsyncMock(return_value=True)
        self.has_stream = AsyncMock(wraps=self._listed)

    async def _listed(self, name):
        return name in (await self.list_streams())

    async def _put(self, name, sources):
        return await self.ensure_stream(name, *sources)

    async def _delete(self, name):
        return await self.remove_stream(name)

    async def _get(self):
        return await self.list_streams()
