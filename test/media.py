"""Tiny photo and video files with exactly the metadata a test needs.

None of them is a viewable image: they only carry the structures the date reader looks at.
`salt` makes otherwise equal files differ, so they get different checksums.
"""

from __future__ import annotations

import datetime as dt
import struct

QT_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)


def _rationals(*vals: tuple[int, int]) -> bytes:
    return b"".join(struct.pack(">II", n, d) for n, d in vals)


def _dms(deg: float) -> bytes:
    deg = abs(deg)
    d = int(deg)
    m = int((deg - d) * 60)
    s = round(((deg - d) * 60 - m) * 60 * 10000)
    return _rationals((d, 1), (m, 1), (s, 10000))


def tiff(date: str | None = None, *, create: str | None = None, modify: str | None = None,
         offset: str | None = None, gps: tuple[float, float] | None = None,
         make: str | None = None, model: str | None = None) -> bytes:
    """Big-endian TIFF/EXIF block. Dates as EXIF writes them: "2019:07:14 15:30:00"."""
    def ascii(s: str) -> tuple[int, int, bytes]:
        b = s.encode() + b"\0"
        return 2, len(b), b

    ifd0, exif, gpsd = [], [], []
    if make:
        ifd0.append((0x010F, *ascii(make)))
    if model:
        ifd0.append((0x0110, *ascii(model)))
    if modify:
        ifd0.append((0x0132, *ascii(modify)))
    if date:
        exif.append((0x9003, *ascii(date)))
    if create:
        exif.append((0x9004, *ascii(create)))
    if offset:
        exif.append((0x9011, *ascii(offset)))
    if gps:
        lat, lon = gps
        gpsd += [(0x0001, *ascii("N" if lat >= 0 else "S")), (0x0002, 5, 3, _dms(lat)),
                 (0x0003, *ascii("E" if lon >= 0 else "W")), (0x0004, 5, 3, _dms(lon))]

    # layout: header | IFD0 | Exif IFD | GPS IFD | data
    size = lambda entries: 2 + 12 * len(entries) + 4
    n0 = len(ifd0) + bool(exif) + bool(gpsd)
    off0 = 8
    off_exif = off0 + 2 + 12 * n0 + 4
    off_gps = off_exif + (size(exif) if exif else 0)
    data_at = off_gps + (size(gpsd) if gpsd else 0)
    data = bytearray()

    def block(entries) -> bytes:
        nonlocal data
        out = struct.pack(">H", len(entries))
        for tag, typ, count, value in sorted(entries):
            if len(value) <= 4:
                out += struct.pack(">HHI", tag, typ, count) + value.ljust(4, b"\0")
            else:
                out += struct.pack(">HHII", tag, typ, count, data_at + len(data))
                data += value + (b"\0" if len(value) % 2 else b"")
        return out + b"\0\0\0\0"

    if exif:
        ifd0.append((0x8769, 4, 1, struct.pack(">I", off_exif)))
    if gpsd:
        ifd0.append((0x8825, 4, 1, struct.pack(">I", off_gps)))
    body = block(ifd0) + (block(exif) if exif else b"") + (block(gpsd) if gpsd else b"")
    return b"MM\0\x2a" + struct.pack(">I", off0) + body + bytes(data)


def xmp(date: str | None = None, gps: tuple[str, str] | None = None) -> bytes:
    """An XMP packet. date like "2019-07-14T15:30:00+02:00", gps like ("64,8.796N", "21,56.556W")."""
    attrs = ""
    if date:
        attrs += f' exif:DateTimeOriginal="{date}"'
    if gps:
        attrs += f' exif:GPSLatitude="{gps[0]}" exif:GPSLongitude="{gps[1]}"'
    return (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/"{attrs}/></rdf:RDF></x:xmpmeta>').encode()


def _app1(payload: bytes) -> bytes:
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def jpeg(exif: bytes | None = None, *, xmp_packet: bytes | None = None, salt: str = "") -> bytes:
    out = b"\xff\xd8"
    if exif:
        out += _app1(b"Exif\0\0" + exif)
    if xmp_packet:
        out += _app1(b"http://ns.adobe.com/xap/1.0/\0" + xmp_packet)
    return out + b"\xff\xda\0\x08" + salt.encode() + b"scan" + b"\xff\xd9"


def png(exif: bytes | None = None, salt: str = "") -> bytes:
    import zlib

    def chunk(typ: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data))

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    if exif:
        out += chunk(b"eXIf", exif)
    return out + chunk(b"tEXt", b"salt\0" + salt.encode()) + chunk(b"IEND", b"")


def webp(exif: bytes | None = None, salt: str = "") -> bytes:
    body = b"WEBP" + b"VP8L" + struct.pack("<I", 6) + b"\x2f" + salt.encode()[:5].ljust(5, b"\0")
    if exif:
        body += b"EXIF" + struct.pack("<I", len(exif)) + exif + (b"\0" if len(exif) % 2 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def mp4(created: dt.datetime | None = None, *, apple_date: str | None = None, iso6709: str | None = None,
        brand: bytes = b"isom", salt: str = "") -> bytes:
    """created goes into mvhd (0 = not set); apple_date like "2019-07-14T15:30:00+0200";
    iso6709 like "+64.1466-021.9426/"."""
    secs = int((created - QT_EPOCH).total_seconds()) if created else 0
    mvhd = b"\0\0\0\0" + struct.pack(">IIII", secs, secs, 1000, 1000) + b"\0" * 80
    moov = _box(b"mvhd", mvhd)
    meta = b""
    if apple_date:
        meta += b"com.apple.quicktime.creationdate" + b"\0\0\0\x10data" + apple_date.encode()
    if iso6709:
        meta += _box(b"\xa9xyz", b"\0\x12\x15\xc7" + iso6709.encode())
    if meta:
        moov += _box(b"udta", meta)
    return _box(b"ftyp", brand + b"\0\0\0\0" + brand) + _box(b"moov", moov) + _box(b"mdat", salt.encode())
