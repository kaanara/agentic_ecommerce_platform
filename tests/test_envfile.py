import os
import stat

import pytest

from aecom import envfile


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_updates_adds_removes_and_keeps_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# header\nA=1\n\n# note\nB=\"two\"\nC=3\n")
    changed = envfile.set_values({"A": "10", "B": None, "D": "4"}, p)
    assert set(changed) == {"A", "B", "D"}
    assert p.read_text() == "# header\nA=10\n\n# note\nC=3\nD=4\n"
    assert (tmp_path / ".env.bak").read_text().startswith("# header\nA=1")
    assert _mode(p) == 0o600 and _mode(tmp_path / ".env.bak") == 0o600


def test_no_change_touches_nothing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    assert envfile.set_values({"A": "1"}, p) == []
    assert not (tmp_path / ".env.bak").exists()


def test_creates_missing_file_with_0600(tmp_path):
    p = tmp_path / ".env"
    assert envfile.set_values({"X": "y"}, p) == ["X"]
    assert p.read_text() == "X=y\n" and _mode(p) == 0o600


@pytest.mark.parametrize("key,value", [("lower", "x"), ("1BAD", "x"), ("A B", "x"), ("OK", "a\nb"), ("OK", "a\x00")])
def test_rejects_bad_input_and_leaves_file(tmp_path, key, value):
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    with pytest.raises(ValueError):
        envfile.set_values({key: value}, p)
    assert p.read_text() == "A=1\n"


def test_only_one_backup_is_kept(tmp_path):
    tmp_path = tmp_path / "envdir"
    tmp_path.mkdir()
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    envfile.set_values({"A": "2"}, p)
    envfile.set_values({"A": "3"}, p)
    assert (tmp_path / ".env.bak").read_text() == "A=2\n"
    assert sorted(f.name for f in tmp_path.iterdir()) == [".env", ".env.bak"]
