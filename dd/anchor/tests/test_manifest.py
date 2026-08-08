# Copyright © 2019-present gsfernandes81

# This file is part of "dd" henceforth referred to as "destiny-director".

# destiny-director is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.

# "destiny-director" is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License along with
# destiny-director. If not, see <https://www.gnu.org/licenses/>.

# Manifest resolution: the currency check, the on-disk reuse it enables, and the
# Bungie-unreachable fallback. No network — aiohttp.ClientSession is faked out, and the
# manifest directory is a tmp cwd.
#
# The behaviour under test is specifically the thing a TTL cache broke once: reuse must
# be keyed on Bungie's versioned filename, never on elapsed time, so a manifest that
# ships mid-week is picked up on the next resolve rather than hours later.

import asyncio
import io
import typing as t
import zipfile

import pytest

from dd.anchor.extensions.bungie_api import manifest as m

pytestmark = pytest.mark.asyncio

_FRAGMENT = "/common/destiny2_content/sqlite/en/"


class _FakeContent:
    """``response.content`` — the download streams off this rather than ``read()``."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int) -> t.AsyncIterator[bytes]:
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _FakeResponse:
    def __init__(self, payload: t.Any, body: bytes) -> None:
        self._payload = payload
        self.content = _FakeContent(body)

    async def json(self) -> t.Any:
        return self._payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeSession:
    """Stands in for both sessions the resolver opens (metadata, then download)."""

    def __init__(self, calls: list[str], meta: t.Any, zip_bytes: bytes) -> None:
        self._calls = calls
        self._meta = meta
        self._zip = zip_bytes

    def get(self, url: str, **_kw: t.Any) -> _FakeResponse:
        self._calls.append(url)
        return _FakeResponse(self._meta, self._zip)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _zip_containing(name: str) -> bytes:
    """A real zip, so the production extract path runs unmodified."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, b"not really sqlite, but a file with the right name")
    return buf.getvalue()


def _install(monkeypatch: pytest.MonkeyPatch, *, current: str | None) -> list[str]:
    """Fake out the network. `current` is the filename Bungie reports, None to fail."""
    calls: list[str] = []

    def session(*_a: t.Any, **_kw: t.Any) -> _FakeSession:
        if current is None:
            raise OSError("Bungie unreachable")
        payload = {"Response": {"mobileWorldContentPaths": {"en": _FRAGMENT + current}}}
        return _FakeSession(calls, payload, _zip_containing(current))

    monkeypatch.setattr(m.aiohttp, "ClientSession", session)
    return calls


def _downloaded(manifest_dir) -> list[str]:
    """Extracted manifests actually on disk, by name."""
    return sorted(p.name for p in manifest_dir.iterdir() if p.is_file())


@pytest.fixture
def manifest_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A tmp cwd — the resolver works against the relative `manifest/` directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "manifest").mkdir()
    return tmp_path / "manifest"


async def test_reuses_the_copy_on_disk_when_it_is_the_current_one(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    (manifest_dir / "world_v1.content").write_bytes(b"already here")
    calls = _install(monkeypatch, current="world_v1.content")

    path = await m._get_latest_manifest("key")

    assert path == "manifest/world_v1.content"
    # One call — the currency check. The download is what reuse saves, not the check.
    assert len(calls) == 1
    assert (manifest_dir / "world_v1.content").read_bytes() == b"already here"


async def test_a_newer_manifest_is_downloaded_rather_than_served_stale(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression a time-based cache introduced: a copy on disk, and a different one
    # live at Bungie. Elapsed time must not decide this.
    (manifest_dir / "world_v1.content").write_bytes(b"last week's definitions")
    _install(monkeypatch, current="world_v2.content")

    path = await m._get_latest_manifest("key")

    assert path == "manifest/world_v2.content"
    assert not (manifest_dir / "world_v1.content").exists()


async def test_raises_when_bungie_is_unreachable_and_nothing_is_on_disk(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, current=None)

    with pytest.raises(OSError):
        await m._get_latest_manifest("key")


async def test_falls_back_to_disk_when_bungie_cannot_be_reached_and_retries_after(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback answers, and does not stick — the next resolve re-checks."""
    (manifest_dir / "world_v1.content").write_bytes(b"stale but usable")
    _install(monkeypatch, current=None)
    assert await m._get_latest_manifest("key") == "manifest/world_v1.content"

    _install(monkeypatch, current="world_v2.content")
    assert await m._get_latest_manifest("key") == "manifest/world_v2.content"


async def test_a_failed_extract_leaves_nothing_the_next_resolve_would_trust(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one corruption the currency check cannot see, so it must not be left behind.

    An extract that dies part-way (running out of disk inside ~340MB is the realistic
    cause) leaves a file with exactly the name Bungie just gave us. Nothing downstream
    can tell it apart from a good copy — the next resolve finds the name, believes it,
    and hands every consumer a truncated sqlite for the life of the process.
    """
    calls = _install(monkeypatch, current="world_v1.content")

    real_extractall = zipfile.ZipFile.extractall

    def _die_after_writing(self, path, *a, **kw):
        real_extractall(self, path, *a, **kw)  # the partial file now exists
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", _die_after_writing)
    with pytest.raises(OSError):
        await m._get_latest_manifest("key")
    assert _downloaded(manifest_dir) == []

    # And the next resolve genuinely re-downloads rather than reusing the wreckage.
    monkeypatch.setattr(zipfile.ZipFile, "extractall", real_extractall)
    assert await m._get_latest_manifest("key") == "manifest/world_v1.content"
    assert len(calls) == 4  # two metadata checks + two downloads, nothing reused


async def test_concurrent_callers_share_one_resolve(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several pages needing the manifest at once must not each call Bungie."""
    (manifest_dir / "world_v1.content").write_bytes(b"already here")
    calls = _install(monkeypatch, current="world_v1.content")

    results = await asyncio.gather(*(m._get_latest_manifest("key") for _ in range(5)))

    assert results == ["manifest/world_v1.content"] * 5
    assert len(calls) == 1


async def test_a_cancelled_caller_does_not_cancel_the_shared_resolve(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    (manifest_dir / "world_v1.content").write_bytes(b"already here")
    _install(monkeypatch, current="world_v1.content")

    first = asyncio.create_task(m._get_latest_manifest("key"))
    second = asyncio.create_task(m._get_latest_manifest("key"))
    await asyncio.sleep(0)  # let both attach to the same resolve
    first.cancel()

    assert await second == "manifest/world_v1.content"


async def test_prewarm_swallows_a_failure(
    manifest_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot that cannot reach Bungie at boot must still start."""
    _install(monkeypatch, current=None)
    await m.prewarm_manifest("key")  # must not raise
