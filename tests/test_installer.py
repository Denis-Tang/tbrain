"""Versioned installation: layout, entry script, upgrade/rollback/prune/uninstall.

Everything runs inside ``tmp_path`` with a fake package (real executables are not
needed); the tests never read or write a real installation.
"""

import sys
from pathlib import Path

import pytest

from tbrain import installer, paths


def make_package(base: Path, version: str, marker: str = "") -> Path:
    """Create a directory shaped like a built/release package."""
    app = base / f"package-{version}"
    (app / "desktop").mkdir(parents=True, exist_ok=True)
    (app / "runtime").mkdir(parents=True, exist_ok=True)
    (app / "_internal" / "tbrain" / "web").mkdir(parents=True, exist_ok=True)
    (app / "integrations" / "pi").mkdir(parents=True, exist_ok=True)
    (app / "tbrain.exe").write_bytes(b"fake-exe-" + version.encode())
    (app / "desktop" / "Tbrain.Desktop.exe").write_bytes(b"fake-desktop-" + version.encode())
    (app / "runtime" / "node.exe").write_bytes(b"fake-node-" + version.encode())
    (app / "_internal" / "tbrain" / "web" / "app.js").write_text("// web", encoding="utf-8")
    (app / "integrations" / "README.md").write_text("readme", encoding="utf-8")
    (app / "integrations" / "hooks.json").write_text("{}", encoding="utf-8")
    (app / "integrations" / "pi" / "tbrain.ts").write_text("// pi", encoding="utf-8")
    (app / "marker.txt").write_text(marker or version, encoding="utf-8")
    return app


def make_release_zip(target: Path, version: str) -> Path:
    package = make_package(target.parent / "zip-src", version)
    with zipfile_zip(target) as archive:
        for path in package.rglob("*"):
            if path.is_file():
                archive.write(path, f"tbrain/{path.relative_to(package).as_posix()}")
    return target


def zipfile_zip(target: Path):
    import zipfile

    return zipfile.ZipFile(target, "w")


def test_install_creates_versioned_layout_and_stable_entry(tmp_path):
    root = tmp_path / "install"
    package = make_package(tmp_path, "0.6.0")
    result = installer.install_from_dir(package, "0.6.0", root=root)

    assert result["ok"] is True
    assert (root / "versions" / "0.6.0" / "tbrain.exe").is_file()
    assert (root / "versions" / "0.6.0" / "marker.txt").read_text(encoding="utf-8") == "0.6.0"
    assert installer.current_version(root) == "0.6.0"
    entry = paths.bin_entry(root)
    assert entry.is_file()
    text = entry.read_text(encoding="ascii")
    assert "%~dp0..\\current.txt" in text
    assert "versions\\%TBRAIN_VERSION%\\tbrain.exe" in text
    assert "%*" in text
    assert installer.entry_command(root) == str(entry)
    assert installer.verify_layout("0.6.0", root)["ok"] is True
    assert result["junction"] in {
        "junction", "symlink", "skipped", "skipped-in-use", "skipped-existing-directory",
    }


def test_install_upgrade_and_prune_keeps_two_versions(tmp_path):
    root = tmp_path / "install"
    installer.install_from_dir(make_package(tmp_path, "0.6.0"), "0.6.0", root=root)
    installer.install_from_dir(make_package(tmp_path, "0.6.1"), "0.6.1", root=root)

    assert installer.current_version(root) == "0.6.1"
    assert installer.installed_versions(root) == ["0.6.0", "0.6.1"]
    assert (root / "versions" / "0.6.0" / "tbrain.exe").is_file()

    installer.install_from_dir(make_package(tmp_path, "0.6.2"), "0.6.2", root=root)
    pruned = installer.prune_versions(keep=2, root=root)
    assert pruned["removed"] == ["0.6.0"]
    assert installer.installed_versions(root) == ["0.6.1", "0.6.2"]
    assert installer.current_version(root) == "0.6.2"
    # the selected version and the stable entry are never pruned
    assert (root / "bin" / "tbrain.cmd").is_file()
    assert installer.verify_layout(root=root)["ok"] is True


def test_switch_current_and_rollback(tmp_path):
    root = tmp_path / "install"
    installer.install_from_dir(make_package(tmp_path, "0.6.0"), "0.6.0", root=root)
    installer.install_from_dir(make_package(tmp_path, "0.6.1"), "0.6.1", root=root)
    assert installer.current_version(root) == "0.6.1"

    switched = installer.switch_current("0.6.0", root=root)
    assert switched["ok"] is True
    assert installer.current_version(root) == "0.6.0"

    with pytest.raises(installer.InstallError):
        installer.switch_current("9.9.9", root=root)

    rolled = installer.rollback(root=root)
    assert rolled["version"] == "0.6.1"
    assert rolled["previous"] == "0.6.0"
    assert installer.current_version(root) == "0.6.1"


def test_rollback_without_other_version_fails(tmp_path):
    root = tmp_path / "install"
    installer.install_from_dir(make_package(tmp_path, "0.6.0"), "0.6.0", root=root)
    with pytest.raises(installer.InstallError):
        installer.rollback(root=root)


def test_uninstall_removes_program_files_only(tmp_path):
    root = tmp_path / "install"
    installer.install_from_dir(make_package(tmp_path, "0.6.0"), "0.6.0", root=root)
    result = installer.uninstall(root=root)
    assert result["ok"] is True
    assert not (root / "versions").exists()
    assert not (root / "current.txt").exists()
    assert not (root / "bin").exists()
    assert installer.installed_versions(root) == []
    assert installer.current_version(root) == ""


def test_install_from_zip_package(tmp_path):
    root = tmp_path / "install"
    archive = make_release_zip(tmp_path / "release.zip", "0.6.0")
    result = installer.install_from_package(archive, "0.6.0", root=root)
    assert result["ok"] is True
    assert (root / "versions" / "0.6.0" / "desktop" / "Tbrain.Desktop.exe").is_file()
    assert installer.current_version(root) == "0.6.0"


def test_extract_refuses_entries_outside_target(tmp_path):
    import zipfile

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("tbrain/tbrain.exe", b"x")
        zipped.writestr("tbrain/../../escaped.txt", b"nope")
    with pytest.raises(installer.InstallError):
        installer.extract_archive(archive, tmp_path / "staging")
    assert not (tmp_path / "escaped.txt").exists()


def test_verify_layout_reports_missing_files(tmp_path):
    root = tmp_path / "install"
    installer.install_from_dir(make_package(tmp_path, "0.6.0"), "0.6.0", root=root)
    (root / "versions" / "0.6.0" / "desktop" / "Tbrain.Desktop.exe").unlink()
    (root / "bin" / "tbrain.cmd").unlink()
    verify = installer.verify_layout("0.6.0", root)
    assert verify["ok"] is False
    assert "desktop/Tbrain.Desktop.exe" in verify["missing"]
    assert "bin/tbrain.cmd" in verify["missing"]


def test_install_rejects_bad_version_and_incomplete_package(tmp_path):
    root = tmp_path / "install"
    package = make_package(tmp_path, "0.6.0")
    with pytest.raises(installer.InstallError):
        installer.install_from_dir(package, "../oops", root=root)
    broken = tmp_path / "broken"
    (broken / "desktop").mkdir(parents=True)
    with pytest.raises(installer.InstallError):
        installer.install_from_dir(broken, "0.6.0", root=root)
    assert not (root / "versions" / "0.6.0" / "tbrain.exe").exists()


def test_entry_script_is_ascii_and_reads_current(tmp_path):
    root = tmp_path / "install"
    installer.install_from_dir(make_package(tmp_path, "0.6.0"), "0.6.0", root=root)
    raw = paths.bin_entry(root).read_bytes()
    assert raw.decode("ascii")  # cmd.exe only reliably handles ASCII
    assert b"current.txt" in raw
    assert installer.current_version(root) == "0.6.0"
    # a manual switch is picked up by the same entry, no rewrite needed
    installer.switch_current("0.6.0", root=root)
    assert installer.current_version(root) == "0.6.0"
