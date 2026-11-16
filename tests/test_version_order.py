"""The 28 release-order cases from planning stage 0's frozen table."""

import pytest

from tbrain import installer, update


B1 = "0.8.0-beta.1"
B2 = "0.8.0-beta.2"


def make_layout(root, names, current):
    for name in names:
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        (folder / "tbrain.exe").touch()
    (root / "current.txt").write_text(current + "\n", encoding="utf-8")


@pytest.mark.parametrize("names,current,target", [
    pytest.param(["0.7.2", "0.7.3", B1], "0.7.3", "0.7.2", id="R01"),
    pytest.param(["0.7.3", B1], "0.7.3", None, id="R02"),
    pytest.param(["0.7.3", B1, B2], B2, B1, id="R03"),
    pytest.param(["0.6.0", "0.6.1"], "0.6.0", "0.6.1", id="R04"),
    pytest.param(["0.7.3", "0.7.4", "weird"], "0.7.4", "0.7.3", id="R05"),
    pytest.param(["0.7.4", "0.8.0-hotfix"], "0.7.4", None, id="R06"),
])
def test_rollback_order(tmp_path, names, current, target):
    make_layout(tmp_path, names, current)
    if target is None:
        with pytest.raises(installer.InstallError, match="没有可回退的版本"):
            installer.rollback(root=tmp_path)
        assert installer.current_version(tmp_path) == current
    else:
        result = installer.rollback(root=tmp_path)
        assert result["version"] == target
        assert result["previous"] == current
        assert installer.current_version(tmp_path) == target


@pytest.mark.parametrize("names,current,keep,kept,removed", [
    pytest.param(["0.7.2", "0.7.3", B1], "0.7.3", 2,
                 {"0.7.2", "0.7.3", B1}, set(), id="P01"),
    pytest.param(["0.7.3", B1, B2], B2, 2,
                 {"0.7.3", B1, B2}, set(), id="P02"),
    pytest.param(["0.7.2", "0.7.3", B1, B2], B2, 2,
                 {"0.7.2", "0.7.3", B1, B2}, set(), id="P03"),
    pytest.param(["0.7.2", "0.7.3", "0.7.4", B1], "0.7.4", 2,
                 {"0.7.3", "0.7.4", B1}, {"0.7.2"}, id="P04"),
    pytest.param(["0.6.0", "0.6.1", "0.6.2"], "0.6.2", 2,
                 {"0.6.1", "0.6.2"}, {"0.6.0"}, id="P05"),
    pytest.param(["0.7.3", "0.7.4", "weird"], "0.7.4", 2,
                 {"0.7.3", "0.7.4", "weird"}, set(), id="P06"),
    pytest.param(["0.7.3", "0.7.4"], "0.7.3", 1,
                 {"0.7.3", "0.7.4"}, set(), id="P07"),
    pytest.param(["0.7.2", "0.7.3", "0.7.4"], "0.7.4", 2,
                 {"0.7.3", "0.7.4"}, {"0.7.2"}, id="P08"),
])
def test_prune_order(tmp_path, names, current, keep, kept, removed):
    make_layout(tmp_path, names, current)
    result = installer.prune_versions(keep=keep, root=tmp_path)
    assert set(result["kept"]) == kept
    assert set(result["removed"]) == removed
    assert {path.name for path in (tmp_path / "versions").iterdir()} == kept
    assert installer.current_version(tmp_path) == current


@pytest.mark.parametrize("names", [
    pytest.param([B2, "0.8.0-beta.10"], id="C01"),
    pytest.param([B1, "0.8.0"], id="C02"),
    pytest.param(["0.8.0-alpha.1", B1, "0.8.0-rc.1", "0.8.0"], id="C03"),
])
def test_version_key_order(names):
    keys = [installer._version_key(name) for name in names]
    assert all(left < right for left, right in zip(keys, keys[1:]))


@pytest.mark.parametrize("names", [
    pytest.param(["0.8.0-rc1", "0.8.0rc1", "0.8.0-rc.1"], id="C04"),
])
def test_equivalent_release_candidates(names):
    parsed = [update.parse_version(name) for name in names]
    assert all(parsed)
    assert parsed[0] == parsed[1] == parsed[2]


@pytest.mark.parametrize("name", [
    pytest.param("weird", id="C05a"),
    pytest.param("0.8.0-hotfix", id="C05b"),
    pytest.param("0.7.3+e9563b9", id="C05c"),
])
def test_unsupported_release(name):
    assert not update.parse_version(name)


@pytest.mark.parametrize("comparisons", [
    pytest.param([(B1, "0.7.3", False)], id="N01"),
    pytest.param([(B2, B1, True)], id="N02"),
    pytest.param([("0.8.0-beta.10", B2, True)], id="N03"),
    pytest.param([("0.8.0", B1, True)], id="N04"),
    pytest.param([("0.6.10", "0.6.9", True), ("0.6.0", "0.6.0", False)], id="N05"),
    pytest.param([("0.8.0-hotfix", "0.7.3", False)], id="N06"),
    pytest.param([(B1, B1, False)], id="N07"),
])
def test_newer_release(comparisons):
    for candidate, current, expected in comparisons:
        assert update.is_newer(candidate, current) is expected
