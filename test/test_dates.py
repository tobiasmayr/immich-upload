"""--date, --place and --date-from-name: parsing, matching and the name rules."""

import argparse
import datetime as dt
from types import SimpleNamespace

import pytest

import media
from conftest import write


# --------------------------------------------------------------------------- --date


@pytest.mark.parametrize("text, when, prec", [
    ("1998", "1998-01-01 12:00:00", 4),
    ("1998-08", "1998-08-01 12:00:00", 7),
    ("1998-08-14", "1998-08-14 12:00:00", 10),
    ("1998-08-14 15:30", "1998-08-14 15:30:00", 16),
    ("1998-08-14T15:30:12", "1998-08-14 15:30:12", 19),
])
def test_parse_date(mod, text, when, prec):
    d, p = mod.parse_date(text)
    assert d.strftime("%Y-%m-%d %H:%M:%S") == when
    assert p == prec
    assert d.tzinfo is not None  # no offset given: this computer's zone


def test_parse_date_offset(mod):
    d, _ = mod.parse_date("2019-07-14 15:30+02:00")
    assert d.utcoffset() == dt.timedelta(hours=2)


@pytest.mark.parametrize("text", ["yesterday", "2019-13", "14.07.2019"])
def test_parse_date_rejects(mod, text):
    with pytest.raises(argparse.ArgumentTypeError):
        mod.parse_date(text)


@pytest.mark.parametrize("date, own, agrees", [
    ("2019", "2019-07-14 15:30:00", True),
    ("2019-07", "2019-07-14 15:30:00", True),
    ("2019-07-14", "2019-07-14 15:30:00", True),
    ("2019-07-14 15:30", "2019-07-14 15:30:00", True),
    ("2019-07-14 16:00", "2019-07-14 15:30:00", False),
    ("2019-08", "2019-07-14 15:30:00", False),
    ("2019-07", "2019", False),  # less precise than --date: no match
    ("2019-07", None, False),
])
def test_agrees(mod, date, own, agrees):
    d, prec = mod.parse_date(date)
    assert mod.agrees(own, SimpleNamespace(date=d, date_prec=prec)) is agrees


# --------------------------------------------------------------------------- --place


def test_parse_place_coordinates(mod):
    assert mod.parse_place("64.1466, -21.9426")[:2] == (64.1466, -21.9426)
    assert mod.parse_place("41.9 12.5")[:2] == (41.9, 12.5)


def test_parse_place_out_of_range(mod):
    with pytest.raises(SystemExit):
        mod.parse_place("91, 0")


# --------------------------------------------------------------------------- dates in names


@pytest.mark.parametrize("name, when, has_time", [
    ("IMG_20190714_153012.jpg", "2019-07-14 15:30:12", True),
    ("PXL_20190714_153012345.mp4", "2019-07-14 15:30:12", True),
    ("IMG-20190714-WA0001.jpg", "2019-07-14 12:00:00", False),
    ("2019-07-14 15.30.12.jpg", "2019-07-14 15:30:12", True),
    ("Screenshot_2019-07-14-15-30-12.png", "2019-07-14 15:30:12", True),
    ("signal-2019-07-14-153012.jpg", "2019-07-14 15:30:12", True),
])
def test_name_dates(mod, name, when, has_time):
    (d, t), *_ = mod.name_dates(name)
    assert (d.strftime("%Y-%m-%d %H:%M:%S"), t) == (when, has_time)


@pytest.mark.parametrize("name", ["DSC01234.jpg", "IMG_20191332_000000.jpg", "1234201907141.jpg", "photo.jpg"])
def test_no_name_date(mod, name):
    assert mod.name_dates(name) == []


@pytest.mark.parametrize("name, first, last", [
    ("2019-07-14 Iceland", "2019-07-14", "2019-07-14"),
    ("2019-07 Iceland", "2019-07-01", "2019-07-31"),
    ("2019_12 Christmas", "2019-12-01", "2019-12-31"),
    ("Rome 1998", "1998-01-01", "1998-12-31"),
    ("2005-03-06 - 2005-03-04 Paris", "2005-03-04", "2005-03-06"),
])
def test_folder_period(mod, name, first, last):
    a, b, _ = mod.folder_period(name)
    assert (a.isoformat(), b.isoformat()) == (first, last)


def test_folder_without_date(mod):
    assert mod.folder_period("Holidays") is None


def plan(mod, root):
    s = mod.scan(root, mod.MEDIA_EXT, {".xmp"})
    mod.read_all(s.media, 2)
    dates, src, notes = mod.dates_from_names(root, s.media, [m for m in s.media if not m.meta.date], s.media)
    return {r: d.strftime("%Y-%m-%d %H:%M:%S") for r, d in dates.items()}, src, notes


def test_folder_date(mod, tmp_path):
    root = tmp_path / "2019-07 Iceland"
    write(root / "a.jpg", media.jpeg(salt="a"))
    write(root / "b.jpg", media.jpeg(salt="b"))
    write(root / "c.jpg", media.jpeg(media.tiff("2019:07:20 10:00:00")))  # inside the month: no conflict
    dates, src, notes = plan(mod, root)
    assert dates == {"a.jpg": "2019-07-01 12:00:00", "b.jpg": "2019-07-01 12:00:01"}
    assert src["a.jpg"] == "folder '2019-07 Iceland'" and not notes


def test_folder_date_refined_by_file_name(mod, tmp_path):
    root = tmp_path / "2019-07 Iceland"
    write(root / "IMG_20190715_101010.jpg", media.jpeg(salt="a"))
    write(root / "IMG-20180101-WA0001.jpg", media.jpeg(salt="b"))  # outside the folder's month: folder date
    dates, src, _ = plan(mod, root)
    assert dates["IMG_20190715_101010.jpg"] == "2019-07-15 10:10:10"
    assert src["IMG_20190715_101010.jpg"] == "file name"
    assert dates["IMG-20180101-WA0001.jpg"] == "2019-07-01 12:00:00"


def test_subfolder_inherits_folder_date(mod, tmp_path):
    root = tmp_path / "Rome 1998"
    write(root / "day 1" / "a.jpg", media.jpeg())
    assert plan(mod, root)[0] == {"day 1/a.jpg": "1998-01-01 12:00:00"}


def test_folder_conflict_falls_back_to_file_names(mod, tmp_path):
    root = tmp_path / "Rome 1998"
    write(root / "c.jpg", media.jpeg(media.tiff("2019:07:14 15:30:00")))  # not 1998
    write(root / "IMG-19980810-WA0003.jpg", media.jpeg())
    dates, src, notes = plan(mod, root)
    assert dates == {"IMG-19980810-WA0003.jpg": "1998-08-10 12:00:00"}
    assert "folder 'Rome 1998' (1998) not used" in notes[0]


def test_file_names_need_every_file_dated(mod, tmp_path):
    write(tmp_path / "IMG_20200101_120000.jpg", media.jpeg(salt="a"))
    write(tmp_path / "scan.jpg", media.jpeg(salt="b"))  # neither EXIF nor name date
    dates, _, notes = plan(mod, tmp_path)
    assert dates == {}
    assert "no date in the name or EXIF, e.g. scan.jpg" in notes[0]


def test_file_names_must_not_contradict_exif(mod, tmp_path):
    write(tmp_path / "IMG_20190101_000000.jpg", media.jpeg(media.tiff("2018:01:02 03:04:05")))
    write(tmp_path / "IMG_20200101_120000.jpg", media.jpeg())
    dates, _, notes = plan(mod, tmp_path)
    assert dates == {}
    assert "contradicts the name" in notes[0]


def test_file_names_without_time_keep_their_order(mod, tmp_path):
    for n in ("IMG-20200101-WA0002.jpg", "IMG-20200101-WA0001.jpg", "IMG-20200102-WA0001.jpg"):
        write(tmp_path / n, media.jpeg(salt=n))
    write(tmp_path / "IMG_20180102_030405.jpg", media.jpeg(media.tiff("2018:01:02 03:04:05")))  # agrees
    dates, _, notes = plan(mod, tmp_path)
    assert dates == {"IMG-20200101-WA0001.jpg": "2020-01-01 12:00:00",
                     "IMG-20200101-WA0002.jpg": "2020-01-01 12:00:01",
                     "IMG-20200102-WA0001.jpg": "2020-01-02 12:00:00"}
    assert not notes


def test_files_in_immich_need_no_name_date(mod, tmp_path):
    write(tmp_path / "old.jpg", media.jpeg(salt="old"))  # undated, but already in Immich
    write(tmp_path / "IMG_20200102_153012.jpg", media.jpeg(salt="new"))
    s = mod.scan(tmp_path, mod.MEDIA_EXT, {".xmp"})
    mod.read_all(s.media, 2)
    new = [m for m in s.media if m.rel != "old.jpg"]
    dates, _, notes = mod.dates_from_names(tmp_path, s.media, new, new)
    assert list(dates) == ["IMG_20200102_153012.jpg"] and not notes


@pytest.mark.parametrize("text, when, prec", [
    ("1998-08-14+00:00", "1998-08-14 12:00:00+0000", 10),
    ("1998-08Z", "1998-08-01 12:00:00+0000", 7),
    ("2019-07-14 15:30 -05:00", "2019-07-14 15:30:00-0500", 16),
])
def test_parse_date_offset_after_any_precision(mod, text, when, prec):
    d, p = mod.parse_date(text)
    assert (d.strftime("%Y-%m-%d %H:%M:%S%z"), p) == (when, prec)
