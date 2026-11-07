"""The date/GPS reader: the same dates Immich would find, and nothing it would ignore."""

import datetime as dt

import media
from conftest import write


def meta(mod, tmp_path, name, data, sidecar=None):
    p = write(tmp_path / name, data)
    side = write(tmp_path / (name + ".xmp"), sidecar) if sidecar else None
    return mod.read_meta(p, side)


def test_jpeg_date_offset_and_camera(mod, tmp_path):
    m = meta(mod, tmp_path, "a.jpg", media.jpeg(media.tiff("2019:07:14 15:30:12", offset="+02:00",
                                                           make="Canon", model="Canon EOS 5D")))
    assert m.date == "2019-07-14"
    assert m.when == "2019-07-14 15:30:12 +02:00"
    assert m.src == "EXIF DateTimeOriginal"
    assert m.camera == "Canon EOS 5D"  # the make is not repeated
    assert not m.gps


def test_create_date_counts(mod, tmp_path):
    m = meta(mod, tmp_path, "a.jpg", media.jpeg(media.tiff(create="2018:01:02 03:04:05")))
    assert (m.date, m.src) == ("2018-01-02", "EXIF CreateDate")


def test_modify_date_does_not_count(mod, tmp_path):
    # Immich ignores EXIF ModifyDate, so a file with only that is undated
    m = meta(mod, tmp_path, "a.jpg", media.jpeg(media.tiff(modify="2019:07:14 15:30:00")))
    assert m.date is None and m.readable


def test_zero_date_is_no_date(mod, tmp_path):
    assert meta(mod, tmp_path, "a.jpg", media.jpeg(media.tiff("0000:00:00 00:00:00"))).date is None


def test_no_exif(mod, tmp_path):
    m = meta(mod, tmp_path, "a.jpg", media.jpeg())
    assert m.date is None and m.readable and not m.gps


def test_gps_with_sign(mod, tmp_path):
    m = meta(mod, tmp_path, "a.jpg", media.jpeg(media.tiff("2019:07:14 12:00:00", gps=(-33.8568, -70.6483))))
    assert m.gps
    assert abs(m.where[0] + 33.8568) < 1e-4 and abs(m.where[1] + 70.6483) < 1e-4


def test_embedded_xmp(mod, tmp_path):
    m = meta(mod, tmp_path, "a.jpg", media.jpeg(xmp_packet=media.xmp("2017-03-03T10:00:00")))
    assert (m.date, m.src) == ("2017-03-03", "XMP exif:DateTimeOriginal")


def test_sidecar_dates_and_places_a_file(mod, tmp_path):
    m = meta(mod, tmp_path, "a.jpg", media.jpeg(), sidecar=media.xmp("2016-06-06T06:06:06", gps=("64,8.796N", "21,56.556W")))
    assert (m.date, m.src) == ("2016-06-06", "sidecar exif:DateTimeOriginal")
    assert m.gps and abs(m.where[0] - 64.1466) < 1e-3 and abs(m.where[1] + 21.9426) < 1e-3


def test_png_webp_tiff(mod, tmp_path):
    exif = media.tiff("2015:05:05 05:05:05")
    assert meta(mod, tmp_path, "a.png", media.png(exif)).date == "2015-05-05"
    assert meta(mod, tmp_path, "a.webp", media.webp(exif)).date == "2015-05-05"
    assert meta(mod, tmp_path, "a.tif", exif).date == "2015-05-05"
    assert meta(mod, tmp_path, "b.png", media.png()).date is None


def test_mp4_creation_time(mod, tmp_path):
    m = meta(mod, tmp_path, "a.mp4", media.mp4(dt.datetime(2012, 2, 2, 12, tzinfo=dt.timezone.utc)))
    assert (m.date, m.when, m.src) == ("2012-02-02", "2012-02-02 12:00:00 UTC", "QuickTime CreateDate")


def test_mp4_without_creation_time(mod, tmp_path):
    m = meta(mod, tmp_path, "a.mp4", media.mp4())
    assert m.date is None and m.readable


def test_mov_apple_date_and_location(mod, tmp_path):
    m = meta(mod, tmp_path, "a.mov", media.mp4(apple_date="2019-07-14T15:30:00+0200", iso6709="+64.1466-021.9426/",
                                               brand=b"qt  "))
    assert (m.date, m.src) == ("2019-07-14", "QuickTime CreationDate")
    assert m.where == (64.1466, -21.9426)


def test_unknown_format_is_not_readable(mod, tmp_path):
    m = meta(mod, tmp_path, "a.gif", b"GIF89a" + b"\0" * 20)
    assert m.date is None and not m.readable
