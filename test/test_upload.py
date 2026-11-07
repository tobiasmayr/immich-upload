"""Whole runs against the fake Immich server."""

import datetime as dt
import os
import subprocess
import sys

import pytest

import media
from conftest import SCRIPT, write

DATED = media.tiff("2019:07:14 15:30:00")


def photo(folder, name, exif=None, gps=None):
    """A JPEG with an EXIF date ("2019:07:14 15:30:00") or none; every file gets its own checksum."""
    return write(folder / name, media.jpeg(media.tiff(exif, gps=gps) if exif or gps else None, salt=name))


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "Iceland"
    f.mkdir()
    return f


# --------------------------------------------------------------------------- basics


def test_upload_verify_album_log(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    photo(folder / "sub", "b.jpg", "2019:07:15 10:00:00")
    write(folder / "notes.txt", b"hi")
    rc, out = run(folder)
    assert rc == 0
    assert "Result: 2 uploaded, 0 already in Immich, 0 failed" in out
    assert len(immich.album("Iceland")["ids"]) == 2
    row = run.log.read_text()
    assert "| Iceland |  | 2 | 2 | verified | no |" in row


def test_verified_folder_is_skipped_until_it_changes(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    run(folder)
    rc, out = run(folder)
    assert rc == 0 and "Already uploaded and verified" in out
    photo(folder, "b.jpg", "2019:07:14 16:00:00")
    rc, out = run(folder)
    assert "now 2: checking again" in out and "1 uploaded, 1 already in Immich" in out


def test_files_in_immich_are_not_sent_again(run, immich, folder):
    p = photo(folder, "a.jpg", "2019:07:14 15:30:00")
    immich.add(p.read_bytes(), "a.jpg")
    rc, out = run(folder)
    assert rc == 0 and "0 uploaded, 1 already in Immich" in out


def test_trashed_file_fails(run, immich, folder):
    p = photo(folder, "a.jpg", "2019:07:14 15:30:00")
    immich.add(p.read_bytes(), "a.jpg", trashed=True)
    rc, out = run(folder, "--delete")
    assert rc == 1 and "in the trash" in out
    assert p.exists()  # not deleted: it did not check out


def test_dry_run_uploads_nothing(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    rc, out = run("-n", "-t", "Travel", "--delete", folder)
    assert rc == 0 and "Dry run: nothing is uploaded" in out
    assert "a.jpg" in out and "upload" in out
    assert not immich.assets and not immich.albums and not immich.tags and not run.log.exists()
    assert (folder / "a.jpg").exists()


def test_album_and_tags(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    rc, out = run("-a", "Trip", "-t", "Travel/Iceland", "-t", "Family", folder)
    assert rc == 0
    a = immich.by_name("a.jpg")["id"]
    assert immich.album("Trip")["ids"] == {a}
    assert a in immich.tags["Travel/Iceland"]["assets"] and a in immich.tags["Family"]["assets"]
    assert "Travel" in immich.tags  # parent created too


def test_rerun_keeps_the_album(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    run("-a", "Trip", folder)
    photo(folder, "b.jpg", "2019:07:14 16:00:00")
    run(folder)
    assert len(immich.album("Trip")["ids"]) == 2 and immich.album("Iceland") is None


def test_delete_after_verified(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    write(folder / "a.jpg.xmp", media.xmp())
    write(folder / "Thumbs.db", b"junk")
    write(folder / "sub" / "b.jpg", media.jpeg(DATED, salt="b"))
    rc, out = run("--delete", folder)
    assert rc == 0 and not folder.exists()
    assert immich.by_name("a.jpg")["sidecar"]


def test_delete_keeps_other_files(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    write(folder / "notes.txt", b"keep me")
    rc, _ = run("--delete", folder)
    assert rc == 0 and (folder / "notes.txt").exists() and not (folder / "a.jpg").exists()


def test_no_delete_when_an_upload_fails(run, immich, folder, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip the retry pauses
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    photo(folder, "b.jpg", "2019:07:14 15:31:00")
    immich.fail_names.add("b.jpg")
    rc, out = run("--delete", folder)
    assert rc == 1 and "1 failed" in out and "Not deleting anything" in out
    assert (folder / "a.jpg").exists()
    assert "| 1 failed |" in run.log.read_text()


def test_bad_api_key(run, immich, folder, monkeypatch):
    monkeypatch.setenv("IMMICH_API_KEY", "wrong")
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    assert run(folder)[0] == 2


# --------------------------------------------------------------------------- dates


def test_undated_files_stop_the_folder(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    photo(folder, "scan.jpg")
    rc, out = run(folder)
    assert rc == 1 and "no date: scan.jpg" in out and "Stopped before uploading" in out
    assert not immich.assets
    assert "| stopped: 1 without date |" in run.log.read_text()


def test_allow_undated(run, immich, folder):
    photo(folder, "scan.jpg")
    rc, out = run("--allow-undated", folder)
    assert rc == 0 and immich.by_name("scan.jpg")["date"] is None


def test_undated_file_already_in_immich_does_not_stop(run, immich, folder):
    p = photo(folder, "scan.jpg")
    immich.add(p.read_bytes(), "scan.jpg")
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    rc, out = run(folder)
    assert rc == 0 and "1 uploaded, 1 already in Immich" in out


def test_date_on_undated_files_one_second_apart(run, immich, folder):
    photo(folder, "scan2.jpg")
    photo(folder, "scan1.jpg")
    rc, out = run("--date", "1998-08-14+00:00", folder)
    assert rc == 0 and "Set in Immich and checked: date on 2 files" in out
    assert immich.by_name("scan1.jpg")["date"] == "1998-08-14T12:00:00.000Z"
    assert immich.by_name("scan2.jpg")["date"] == "1998-08-14T12:00:01.000Z"


def test_date_never_touches_files_already_in_immich(run, immich, folder):
    old = photo(folder, "old.jpg")
    immich.add(old.read_bytes(), "old.jpg")
    photo(folder, "new.jpg")
    rc, out = run("--date", "1998", "--place", "41.9, 12.5", folder)
    assert rc == 0 and "already in Immich keep their date and place" in out
    assert immich.by_name("old.jpg")["date"] is None and immich.by_name("old.jpg")["lat"] is None
    assert immich.by_name("new.jpg")["date"] and immich.by_name("new.jpg")["lat"] == 41.9


def test_mixed_dates_ask_and_no_uploads_nothing(run, immich, folder):
    photo(folder, "a.jpg", "2015:05:05 05:05:05")
    photo(folder, "scan.jpg")
    rc, out = run("--date", "1998", folder, answer=False)
    assert rc == 1 and "already has a date" in run.questions[0]
    assert not immich.assets


def test_mixed_dates_yes_sets_only_the_undated(run, immich, folder):
    photo(folder, "a.jpg", "2015:05:05 05:05:05")
    photo(folder, "scan.jpg")
    rc, _ = run("--date", "1998-08-14+00:00", folder, answer=True)
    assert rc == 0
    assert immich.by_name("a.jpg")["date"] is None  # keeps its EXIF date
    assert immich.by_name("scan.jpg")["date"] == "1998-08-14T12:00:00.000Z"


def test_matching_date_is_no_conflict(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    photo(folder, "scan.jpg")
    rc, out = run("--date", "2019-07", folder)  # no question: a.jpg's date is a more precise 2019-07
    assert rc == 0 and "1 file already has a date matching --date 2019-07 and keeps it" in out
    assert immich.by_name("a.jpg")["date"] is None


def test_force_overwrites_without_asking_but_keeps_matching_dates(run, immich, folder):
    old = photo(folder, "old.jpg", "2015:05:05 05:05:05")
    immich.add(old.read_bytes(), "old.jpg")
    photo(folder, "match.jpg", "2019:07:20 10:00:00")
    photo(folder, "scan.jpg")
    rc, out = run("--force", "--date", "2019-07+00:00", folder)
    assert rc == 0 and not run.questions
    assert immich.by_name("old.jpg")["date"]  # --force is a re-upload: also files in Immich
    assert immich.by_name("match.jpg")["date"] is None
    assert immich.by_name("scan.jpg")["date"]


def test_place_only_on_files_without_gps(run, immich, folder):
    photo(folder, "gps.jpg", "2019:07:14 15:30:00", gps=(64.1466, -21.9426))
    photo(folder, "nogps.jpg", "2019:07:14 15:31:00")
    rc, _ = run("--place", "41.9, 12.5", folder, answer=True)
    assert rc == 0 and len(run.questions) == 1  # mixed: one has a location
    assert immich.by_name("gps.jpg")["lat"] is None and immich.by_name("nogps.jpg")["lat"] == 41.9


def test_date_from_file_name_needs_no_question(run, immich, folder):
    photo(folder, "IMG-20200101-WA0001.jpg")
    photo(folder, "IMG_20200102_153012.jpg")
    rc, out = run("--date-from-name", folder)
    assert rc == 0 and "2 files from their file name" in out
    assert immich.by_name("IMG_20200102_153012.jpg")["date"]


def test_date_from_folder_name_asks(run, immich, tmp_path):
    folder = tmp_path / "2019-07 Iceland"
    photo(folder, "a.jpg")
    rc, _ = run("--date-from-name", folder, answer=False)
    assert rc == 1 and "dated from the folder name" in run.questions[0] and not immich.assets
    rc, _ = run("--date-from-name", folder, answer=True)
    assert rc == 0 and immich.by_name("a.jpg")["date"]


def test_no_terminal_means_no(run, immich, folder, monkeypatch, mod, capsys):
    photo(folder, "a.jpg", "2015:05:05 05:05:05")
    photo(folder, "scan.jpg")
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert mod.confirm("Go on?") is False
    assert "cannot ask: no terminal" in capsys.readouterr().out


# --------------------------------------------------------------------------- listings


def test_list_files_without_server(run, no_server, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00", gps=(64.1466, -21.9426))
    photo(folder, "scan.jpg")
    rc, out = run("-L", folder)
    assert rc == 1  # a run would stop
    assert "2019-07-14 15:30:00" in out and "64.14660, -21.94260" in out
    assert "NO DATE: stops the folder" in out and "Checking the local files only: no Immich URL" in out
    assert "Album 'Iceland' (not checked)" in out


def test_list_files_with_server(run, immich, folder):
    p = photo(folder, "old.jpg")
    immich.add(p.read_bytes(), "old.jpg")
    photo(folder, "IMG_20200102_153012.jpg")
    rc, out = run("-L", "--date-from-name", folder)
    assert rc == 0 and "in Immich, left as is" in out
    row = next(l for l in out.splitlines() if "IMG_20200102_153012.jpg" in l)
    assert "-> 2020-01-02 15:30:12" in row and "file name" in row and "upload, date from file name" in row
    assert not immich.album("Iceland")  # changes nothing


def test_list_albums_and_tags(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    run("-t", "Travel/Iceland", folder)
    rc, out = run("--list-albums", "--list-tags")
    assert rc == 0 and "Iceland" in out and "Travel" in out


def test_help_runs_as_a_program():
    r = subprocess.run([sys.executable, str(SCRIPT), "-h"], capture_output=True, text=True)
    assert r.returncode == 0 and "--date-from-name" in r.stdout


def test_dry_run_summary(run, immich, folder):
    old = photo(folder, "old.jpg", "2019:07:14 11:06:00")
    immich.add(old.read_bytes(), "old.jpg")
    photo(folder, "a.jpg", "2019:07:14 16:35:00")
    write(folder / "Thumbs.db", b"junk")
    rc, out = run("-n", "-t", "Events", "--delete", folder)
    assert rc == 0
    assert "Would create tag: Events" in out
    assert "Album 'Iceland' (new), tag Events" in out
    assert "2 files (" in out and "taken 2019-07-14 11:06 to 16:35; 1 already in Immich, 1 to upload" in out
    assert "Dates, of them: no file lacks a date" in out
    assert "--delete: once all 2 are verified, deletes them, 1 junk file; then the folder" in out


def test_dry_run_existing_album_and_kept_folder(run, immich, folder):
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    write(folder / "notes.txt", b"keep")
    immich.albums["x"] = {"name": "Iceland", "ids": set()}
    rc, out = run("-n", "--delete", folder)
    assert "Album 'Iceland' (exists)" in out
    assert "deletes them; the folder stays (1 other file in it)" in out


def test_dry_run_tells_what_would_ask(run, immich, folder):
    photo(folder, "a.jpg", "2015:05:05 05:05:05")
    photo(folder, "scan.jpg")
    rc, out = run("-n", "--date", "1998", folder)
    assert rc == 1 and "A run would ask first: some files have a date/place and others not" in out
    assert not run.questions  # a dry run never asks


def test_dry_run_with_a_bad_key_still_lists(run, immich, folder, monkeypatch):
    monkeypatch.setenv("IMMICH_API_KEY", "wrong")
    photo(folder, "a.jpg", "2019:07:14 15:30:00")
    rc, out = run("-n", folder)
    assert rc == 2 and "a.jpg" in out and "Immich refused the API key" in run.err


def test_dry_run_shows_planned_date_and_place_in_the_columns(run, immich, folder):
    photo(folder, "scan.jpg")
    rc, out = run("-n", "--date", "1998-08-14+00:00", "--place", "41.9, 12.5", folder)
    row = next(l for l in out.splitlines() if "scan.jpg" in l)
    assert "-> 1998-08-14 12:00:00 +00:00" in row and "--date" in row and "-> 41.90000, 12.50000" in row
