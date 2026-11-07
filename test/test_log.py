"""The Markdown log: one row per folder, kept across runs."""

from pathlib import Path


def record(log, folder, **kw):
    args = dict(album="A", tags=[], files=3, new=3, status="verified", deleted="no", now="2026-11-07 10:00")
    args.update(kw)
    log.record(Path(folder), **args)


def test_new_log_has_intro_and_row(mod, tmp_path):
    p = tmp_path / "log.md"
    record(mod.Log(p), "/photos/Iceland", tags=["Travel/Iceland"])
    text = p.read_text()
    assert text.startswith("# Immich uploads")
    assert "| `/photos/Iceland` | A | Travel/Iceland | 3 | 3 | verified | no | 2026-11-07 10:00 | 2026-11-07 10:00 |" in text


def test_runs_are_merged(mod, tmp_path):
    p = tmp_path / "log.md"
    record(mod.Log(p), "/photos/Iceland", new=2, status="1 failed", tags=["A"])
    record(mod.Log(p), "/photos/Iceland", new=1, deleted="yes", tags=["B"], now="2026-11-08 09:00")
    record(mod.Log(p), "/photos/Iceland", new=0, now="2026-11-09 09:00")
    row = mod.Log(p).get(Path("/photos/Iceland"))
    assert row["Uploaded"] == "3"  # adds up over the runs
    assert row["Status"] == "verified"
    assert row["Deleted"] == "yes"  # a later "no" does not undo it
    assert row["Tags"] == "A, B"
    assert row["First run"] == "2026-11-07 10:00"
    assert row["Last run"] == "2026-11-09 09:00 (3 runs)"


def test_text_around_the_table_is_kept_and_pipes_escaped(mod, tmp_path):
    p = tmp_path / "log.md"
    p.write_text("# My log\n\nnotes before\n")
    record(mod.Log(p), "/photos/a|b")
    p.write_text(p.read_text() + "\nnotes after\n")
    record(mod.Log(p), "/photos/c")
    text = p.read_text()
    assert text.startswith("# My log\n\nnotes before\n")
    assert text.rstrip().endswith("notes after")
    assert "| `/photos/a\\|b` |" in text  # a | in a name must not split the row
    assert mod.Log(p).get(Path("/photos/a|b"))["Status"] == "verified"
    assert len(mod.Log(p).rows) == 2


def test_old_one_row_per_run_layout_is_folded(mod, tmp_path):
    p = tmp_path / "log.md"
    p.write_text(
        "# Immich uploads\n\n"
        "| Date | Folder | Album | Files | New | Already in Immich | Status | Deleted | Tags |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| 2026-11-01 10:00 | `/p/x` | X | 5 | 3 | 0 | 2 failed | no | T1 |\n"
        "| 2026-11-02 10:00 | `/p/x` | X | 5 | 2 | 3 | verified | yes | T2 |\n")
    row = mod.Log(p).get(Path("/p/x"))
    assert (row["Uploaded"], row["Status"], row["Deleted"], row["Tags"]) == ("5", "verified", "yes", "T1, T2")
    assert row["First run"] == "2026-11-01 10:00" and row["Last run"] == "2026-11-02 10:00 (2 runs)"
