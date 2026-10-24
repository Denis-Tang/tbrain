"""Update checks, verified downloads and local-package upgrades -- no network in tests.

All HTTP interactions are replaced by local fake streams/fetchers; installation and
upgrade work happens in ``tmp_path`` only.
"""

import hashlib
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from tbrain import __version__, installer, paths, proc, update

PACKAGE_FILES = [
    "tbrain/tbrain.exe",
    "tbrain/desktop/Tbrain.Desktop.exe",
    "tbrain/runtime/node.exe",
    "tbrain/integrations/README.md",
    "tbrain/_internal/tbrain/__init__.py",
    "tbrain/_internal/tbrain/web/app.js",
]
_major, _minor, _patch = __version__.split(".")
NEXT_VERSION = f"{_major}.{_minor}.{int(_patch) + 1}"


def make_package(base: Path, version: str = NEXT_VERSION) -> Path:
    app = base / f"package-{version}"
    (app / "desktop").mkdir(parents=True, exist_ok=True)
    (app / "runtime").mkdir(parents=True, exist_ok=True)
    (app / "_internal" / "tbrain" / "web").mkdir(parents=True, exist_ok=True)
    (app / "integrations").mkdir(parents=True, exist_ok=True)
    (app / "tbrain.exe").write_bytes(b"exe-" + version.encode())
    (app / "desktop" / "Tbrain.Desktop.exe").write_bytes(b"desktop")
    (app / "runtime" / "node.exe").write_bytes(b"node")
    (app / "_internal" / "tbrain" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    (app / "_internal" / "tbrain" / "web" / "app.js").write_text("// web", encoding="utf-8")
    (app / "integrations" / "README.md").write_text("readme", encoding="utf-8")
    return app


def zip_package(target: Path, version: str = NEXT_VERSION) -> bytes:
    package = make_package(target.parent / f"zip-{version}", version)
    with zipfile.ZipFile(target, "w") as archive:
        for path in package.rglob("*"):
            if path.is_file():
                archive.write(path, f"tbrain/{path.relative_to(package).as_posix()}")
    return Path(target).read_bytes()


class FakeStream:
    """Stands in for httpx's streaming response context manager."""

    def __init__(self, payload: bytes, status_code: int = 200, chunk: int = 4096):
        self.payload = payload
        self.status_code = status_code
        self.chunk = chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def headers(self):
        return {"content-length": str(len(self.payload))}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid/pkg.zip")
            raise httpx.HTTPStatusError("bad", request=request,
                                        response=httpx.Response(self.status_code, request=request))

    def iter_bytes(self, size=0):
        for index in range(0, len(self.payload), self.chunk):
            yield self.payload[index:index + self.chunk]


def release_for(version: str = NEXT_VERSION, payload: bytes = b"", **overrides) -> dict:
    digest = hashlib.sha256(payload).hexdigest() if payload else ""
    asset = {"name": f"tbrain-{version}-windows-x64.zip",
             "url": f"https://example.invalid/tbrain-{version}.zip",
             "size": len(payload), "sha256": digest}
    asset.update(overrides.pop("asset", {}))
    return {"tag": "v" + version, "version": version, "published_at": "2026-10-24T00:00:00Z",
            "notes": "改进", "notes_truncated": False, "page": update.RELEASE_PAGE,
            "asset": asset, **overrides}


def install_fake_version(root: Path, tmp: Path, version: str = __version__) -> None:
    installer.install_from_dir(make_package(tmp / f"install-src-{version}", version), version, root=root)


def test_check_latest_reports_newer_and_caches(tmp_path):
    calls = []

    def fetcher(timeout=8.0):
        calls.append(timeout)
        return release_for()

    result = update.check_latest(tmp_path, force=True, fetcher=fetcher)
    assert result["current"] == __version__
    assert result["latest"] == NEXT_VERSION
    assert result["newer"] is True
    assert result["available"] is True
    assert (tmp_path / update.CACHE_NAME).is_file()
    assert len(calls) == 1

    # A forced check inside REPEAT_SECONDS reuses the cache instead of hammering GitHub.
    again = update.check_latest(tmp_path, force=True, fetcher=lambda timeout=8.0: (_ for _ in ()).throw(
        AssertionError("network should not be used")))
    assert again["cached"] is True
    assert again["latest"] == NEXT_VERSION


def test_check_latest_degrades_to_structured_failure(tmp_path):
    def broken(timeout=8.0):
        raise update.UpdateError("无法连接 GitHub，请检查网络后重试。")

    result = update.check_latest(tmp_path, force=True, fetcher=broken)
    assert result["available"] is False
    assert result["newer"] is False
    assert "无法连接 GitHub" in result["message"]
    assert result["latest"] == ""


def test_check_latest_keeps_stale_cache_on_failure(tmp_path):
    update.check_latest(tmp_path, force=True, fetcher=lambda timeout=8.0: release_for())

    # age the cache past the repeat window but keep the stored release
    cache_path = tmp_path / update.CACHE_NAME
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["checked_at"] = "2020-01-01T00:00:00+00:00"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    def broken(timeout=8.0):
        raise update.UpdateError("GitHub 返回 HTTP 403。")

    result = update.check_latest(tmp_path, force=True, fetcher=broken)
    assert result["available"] is True
    assert result["stale"] is True
    assert result["latest"] == NEXT_VERSION


def test_is_newer_compares_numerically():
    assert update.is_newer("0.6.10", "0.6.9")
    assert not update.is_newer("0.6.0", "0.6.0")
    assert not update.is_newer("0.5.9", "0.6.0")
    assert update.parse_version("v0.6.0-rc.1") < update.parse_version("v0.6.0")


def test_download_release_verifies_sha_and_layout(tmp_path):
    archive = tmp_path / "release.zip"
    payload = zip_package(archive)
    release = release_for(payload=payload)
    downloaded = update.download_release(tmp_path, release, stream=lambda url, timeout: FakeStream(payload))
    assert Path(downloaded["path"]).is_file()
    assert downloaded["sha256"] == hashlib.sha256(payload).hexdigest()
    record = json.loads((Path(downloaded["path"]).parent / update.RECORD_NAME).read_text(encoding="utf-8"))
    assert record["version"] == NEXT_VERSION
    # a second call reuses the verified archive instead of downloading again
    reused = update.download_release(tmp_path, release, stream=lambda url, timeout: (_ for _ in ()).throw(
        AssertionError("must reuse the verified archive")))
    assert reused["reused"] is True


def test_download_release_rejects_bad_sha(tmp_path):
    payload = zip_package(tmp_path / "release.zip")
    release = release_for(payload=payload)
    release["asset"]["sha256"] = "0" * 64
    with pytest.raises(update.UpdateError):
        update.download_release(tmp_path, release, stream=lambda url, timeout: FakeStream(payload))
    folder = tmp_path / "updates" / NEXT_VERSION
    leftovers = [item for item in folder.glob("*.zip")] if folder.is_dir() else []
    assert leftovers == []


def test_download_requires_published_digest(tmp_path):
    release = release_for()
    release["asset"]["sha256"] = ""
    with pytest.raises(update.UpdateError):
        update.download_release(tmp_path, release, stream=lambda url, timeout: FakeStream(b""))


def test_validate_archive_rejects_wrong_layout(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("other/tbrain.exe", b"x")
    with pytest.raises(update.UpdateError):
        update.validate_archive(archive)


def test_infer_version_from_directory_and_zip(tmp_path):
    package = make_package(tmp_path)
    assert update.infer_package_version(package) == NEXT_VERSION
    archive = tmp_path / "release.zip"
    payload = zip_package(archive)
    assert update.infer_package_version(archive) == NEXT_VERSION
    assert payload


def test_infer_version_from_version_file(tmp_path):
    # PyInstaller onedir packages keep no readable tbrain/__init__.py; build.ps1
    # writes a plain VERSION file instead.
    package = make_package(tmp_path)
    (package / "_internal" / "tbrain" / "__init__.py").unlink()
    (package / "VERSION").write_text(NEXT_VERSION + chr(10), encoding="utf-8")
    assert update.infer_package_version(package) == NEXT_VERSION

    archive = tmp_path / "version-file.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("tbrain/tbrain.exe", b"x")
        zipped.writestr("tbrain/VERSION", NEXT_VERSION + chr(10))
    assert update.infer_package_version(archive) == NEXT_VERSION


def test_upgrade_from_local_package_runs_the_full_flow(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "install"
    install_fake_version(root, tmp_path)  # current == __version__
    package = make_package(tmp_path, NEXT_VERSION)
    doctor_calls = []

    result = update.upgrade_from_package(package, NEXT_VERSION, home=home, install_root=root,
                                         doctor=lambda: doctor_calls.append(True) or {"ok": True, "items": []})
    assert result["ok"] is True
    assert result["from"] == __version__
    assert result["to"] == NEXT_VERSION
    assert installer.current_version(root) == NEXT_VERSION
    assert doctor_calls == [True]
    assert result["pruned"]["kept"][0] == NEXT_VERSION
    state = installer.read_state(home)
    assert state["stage"] == "done"
    assert state["version"] == NEXT_VERSION


def test_upgrade_from_package_rejects_bad_sha(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "install"
    install_fake_version(root, tmp_path)
    archive = tmp_path / "release.zip"
    zip_package(archive)
    with pytest.raises(update.UpdateError):
        update.upgrade_from_package(archive, NEXT_VERSION, home=home, install_root=root,
                                    sha256="f" * 64)
    assert installer.current_version(root) == __version__


def test_upgrade_via_check_and_stream(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "install"
    install_fake_version(root, tmp_path)
    payload = zip_package(tmp_path / "release.zip")
    fetcher = lambda timeout=8.0: release_for(payload=payload)
    doctor_calls = []

    result = update.upgrade(home=home, install_root=root, fetcher=fetcher,
                            stream=lambda url, timeout: FakeStream(payload),
                            doctor=lambda: doctor_calls.append(True) or {"ok": True, "items": []})
    assert result["ok"] is True
    assert result["upgraded"] is True
    assert result["check"]["latest"] == NEXT_VERSION
    assert installer.current_version(root) == NEXT_VERSION
    assert doctor_calls == [True]


def test_upgrade_does_nothing_when_already_latest(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "install"
    install_fake_version(root, tmp_path)
    result = update.upgrade(home=home, install_root=root,
                            fetcher=lambda timeout=8.0: release_for(__version__),
                            stream=lambda url, timeout: (_ for _ in ()).throw(
                                AssertionError("must not download")))
    assert result["upgraded"] is False
    assert result["ok"] is True
    assert installer.current_version(root) == __version__


def test_upgrade_evicts_registered_old_process(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "install"
    install_fake_version(root, tmp_path)
    package = make_package(tmp_path, NEXT_VERSION)
    # A registered old-version process that "exits" as soon as the request lands.
    fake_pid = 990001
    proc.register("mcp", version=__version__, home=home, pid=fake_pid)

    real_evict = proc.evict_old_versions

    def evict(current, timeout=5.0, home=None, **kwargs):
        return real_evict(current, timeout=timeout, home=home,
                          alive=lambda pid: not paths.exit_request_file(pid, home).exists(),
                          sleep=lambda seconds: time.sleep(0.01))

    import tbrain.update as update_module

    original = update_module.proc.evict_old_versions
    update_module.proc.evict_old_versions = evict
    try:
        result = update.upgrade_from_package(package, NEXT_VERSION, home=home, install_root=root)
    finally:
        update_module.proc.evict_old_versions = original
    assert result["evicted"]["requested"] == [fake_pid]
    assert fake_pid in result["evicted"]["exited"]
