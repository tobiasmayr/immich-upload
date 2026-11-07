#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Mayr
#
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY;
# see the LICENSE file or <https://www.gnu.org/licenses/> for details.
"""Upload photo folders to Immich, check every file arrived, and log each folder.

For each folder given:
  1. Find all photos and videos in it, including subfolders (the file types
     come from the Immich server itself). An .xmp sidecar next to a file is
     sent along with it.
  2. Find out which files Immich already has (same SHA-1 checksum). Those
     are not sent again, and their date and place are left alone.
  3. Read the capture date of each file to upload. If any has none, stop
     before uploading (Immich would date it by the file time), unless
     --date-from-name finds one in the folder or file name, --date gives one,
     or --allow-undated is set. --place gives a location to files
     without GPS. If some files have a date (or place) and others not, the
     script lists them and asks before going on.
  4. Upload, then check that every file is in Immich under its exact
     checksum and not in the trash.
  5. Add all of them to an album (--album, else the one the log already has
     for the folder, else the folder's name), and tag them with any --tag
     given (missing tags are created). Set --date and --place in Immich on
     the files that lack them, and read both back. The files themselves are
     not changed, so a later run still finds them by checksum.
  6. Record the folder in a Markdown log, one row per folder, updated on every
     run. A folder logged as verified is skipped next time, unless its number
     of files changed.
  7. With --delete, and only when every file checked out, delete the
     uploaded files, their sidecars and system junk (Thumbs.db, desktop.ini,
     ...), then the folder if nothing else is left in it. Other files are
     kept and listed.

--force redoes a folder as a re-upload: also verified folders, and --date /
--place then apply to every file, also those already in Immich, overwriting
dates and places they have, without asking.

--dry-run (-n, or -L / --list-files) changes nothing: it lists each file in the
folders with its date (and which tag it came from), location and camera, and
what a run with the same options would do: album, tags, dates, places, and
what --delete would remove. With a server set, it also checks the API key and
which files Immich already has; without one, it works from the local files.

Needs only the Python standard library (3.9+), so it runs on Linux and Windows.

Settings come from the command line or these environment variables:
  IMMICH_URL           e.g. https://photos.example.org
  IMMICH_API_KEY       permissions: asset.upload, asset.read, album.read,
                       album.create, albumAsset.create; for --tag also
                       tag.read, tag.create, tag.asset; for --date or
                       --place also asset.update
  IMMICH_API_KEY_FILE  a file holding the key instead (see --api-key-file)
  IMMICH_UPLOAD_LOG    path of the Markdown log (optional)

Without any of these, the key is read from immich-upload/api-key in the user's
config folder (~/.config on Linux, %APPDATA% on Windows), if that file exists.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import http.client
import json
import os
import platform
import re
import ssl
import stat
import struct
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

CHUNK = 1024 * 1024
BATCH = 500  # ids per bulk request
JUNK = {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db", ".picasa.ini", "picasa.ini"}
LOG_NAME = "immich-uploads.md"
LOG_INTRO = (
    "# Immich uploads\n\n"
    "Folders uploaded with `immich-upload.py`, one row per folder. A `verified` folder\n"
    "is skipped on the next run unless its number of files changed or `--force` is given.\n"
    "\"Uploaded\" counts the files this script sent for the folder over all runs.\n"
)
LOG_COLUMNS = ["Folder", "Album", "Tags", "Files", "Uploaded", "Status", "Deleted", "First run", "Last run"]


# --------------------------------------------------------------------------- API


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Immich:
    def __init__(self, url: str, api_key: str, timeout: float = 600):
        url = url.strip().rstrip("/")
        if not url.endswith("/api"):
            url += "/api"
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise SystemExit(f"Not a valid Immich URL: {url}")
        self.scheme, self.host, self.port, self.base = u.scheme, u.hostname, u.port, u.path
        self.api_key = api_key
        self.timeout = timeout
        self.ctx = ssl.create_default_context() if u.scheme == "https" else None

    def _conn(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def request(self, method, path, body=None, *, query=None, headers=None, stream=None, length=None):
        """JSON request, or a streamed body when `stream` (an iterator of bytes) is given."""
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        hdrs = {"x-api-key": self.api_key, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))
        hdrs.update(headers or {})
        if stream is not None:
            hdrs["Content-Length"] = str(length)
        conn = self._conn()
        try:
            conn.putrequest(method, url)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders()
            if stream is not None:
                for chunk in stream:
                    conn.send(chunk)
            elif body is not None:
                conn.send(data)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        if resp.status >= 400:
            try:
                msg = json.loads(raw).get("message", raw[:300])
            except Exception:
                msg = raw[:300].decode(errors="replace")
            raise ApiError(resp.status, str(msg))
        return json.loads(raw) if raw else None

    # -- endpoints

    def media_types(self) -> tuple[set[str], set[str]]:
        t = self.request("GET", "/server/media-types")
        return {e.lower() for e in t["image"] + t["video"]}, {e.lower() for e in t["sidecar"]}

    def check(self, items: list[tuple[str, str]]) -> dict[str, dict]:
        """items = [(client_id, sha1_hex)] -> {client_id: result}"""
        out = {}
        for i in range(0, len(items), BATCH):
            part = items[i : i + BATCH]
            r = self.request("POST", "/assets/bulk-upload-check",
                             {"assets": [{"id": cid, "checksum": c} for cid, c in part]})
            for res in r["results"]:
                out[res["id"]] = res
        return out

    def upload(self, f: "MediaFile") -> dict:
        boundary = "----immich-upload-" + uuid.uuid4().hex
        stamp = iso(f.mtime)
        fields = {"fileCreatedAt": stamp, "fileModifiedAt": stamp, "filename": f.path.name}
        files = [("assetData", f.path)]
        if f.sidecar:
            files.append(("sidecarData", f.sidecar))

        head = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in fields.items()
        )
        parts = []  # (preamble, path)
        for name, p in files:
            fname = p.name.replace('"', "_")
            pre = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                   f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
            parts.append((pre, p))
        tail = f"--{boundary}--\r\n".encode()
        length = len(head) + len(tail) + sum(len(pre) + p.stat().st_size + 2 for pre, p in parts)

        def stream():
            yield head
            for pre, p in parts:
                yield pre
                with open(p, "rb") as fh:
                    while chunk := fh.read(CHUNK):
                        yield chunk
                yield b"\r\n"
            yield tail

        return self.request("POST", "/assets", stream=stream(), length=length,
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                     "x-immich-checksum": f.sha1})

    def album_id(self, name: str, create: bool) -> str | None:
        for a in self.request("GET", "/albums", query={"name": name, "isOwned": "true"}) or []:
            if a["albumName"] == name:
                return a["id"]
        if not create:
            return None
        return self.request("POST", "/albums", {"albumName": name})["id"]

    def tags(self) -> list[dict]:
        return self.request("GET", "/tags") or []

    def upsert_tags(self, values: list[str]) -> dict[str, str]:
        """Create tags (and their parents) as needed -> {value: tag id}"""
        return {t["value"]: t["id"] for t in self.request("PUT", "/tags", {"tags": values})}

    def tag_assets(self, tag_ids: list[str], ids: list[str]) -> None:
        for i in range(0, len(ids), BATCH):
            self.request("PUT", "/tags/assets", {"tagIds": tag_ids, "assetIds": ids[i : i + BATCH]})

    def add_to_album(self, album: str, ids: list[str]) -> list[dict]:
        bad = []
        for i in range(0, len(ids), BATCH):
            for r in self.request("PUT", f"/albums/{album}/assets", {"ids": ids[i : i + BATCH]}):
                if not r["success"] and r.get("error") != "duplicate":  # duplicate = already in it
                    bad.append(r)
        return bad


# --------------------------------------------------------------------------- photo dates and GPS
#
# A small reader for the date and GPS tags Immich itself uses (it runs exiftool on the server):
# EXIF DateTimeOriginal / CreateDate / GPSDateStamp, GPS position, QuickTime creation dates,
# and the same tags in XMP (embedded, or in the .xmp sidecar). EXIF "ModifyDate" does not
# count, as in Immich. Without one of these, Immich falls back to the file's time.


@dataclass
class Meta:
    date: str | None = None  # YYYY-MM-DD of the capture date, None = no date
    gps: bool = False
    readable: bool = True  # False = file format this script cannot look into
    # for the dry run's listing only:
    when: str | None = None  # the date as stored, e.g. "2019-07-14 15:30:12 +02:00"
    src: str | None = None  # where the date came from, e.g. "EXIF DateTimeOriginal"
    where: tuple[float, float] | None = None  # latitude, longitude
    camera: str | None = None


QT_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
XMP_DATE = re.compile(rb'(exif:DateTimeOriginal|xmp:CreateDate)\s*(?:=\s*["\']|>)\s*'
                      rb'((\d{4})-(\d{2})-(\d{2})(?:T[\d:.]+)?(?:Z|[+-]\d{2}:\d{2})?)')
XMP_LAT = re.compile(rb'exif:GPSLatitude\s*(?:=\s*["\']|>)\s*(\d[\d,.]*[NSns]?)')
XMP_LON = re.compile(rb'exif:GPSLongitude\s*(?:=\s*["\']|>)\s*(\d[\d,.]*[EWew]?)')


def valid_ymd(y, m, d) -> str | None:
    try:
        y, m, d = int(y), int(m), int(d)
        return dt.date(y, m, d).isoformat() if y >= 1800 else None
    except ValueError:
        return None


def exif_date(raw: bytes) -> str | None:
    m = re.match(rb"\s*(\d{4})[:\-](\d{2})[:\-](\d{2})", raw)
    return valid_ymd(*m.groups()) if m else None


def exif_when(raw: bytes, offset: bytes = b"") -> str:
    """EXIF "2019:07:14 15:30:12" plus an OffsetTime "+02:00" -> "2019-07-14 15:30:12 +02:00"."""
    t = raw.split(b"\0")[0].decode("ascii", "replace").strip()
    t = t[:10].replace(":", "-") + t[10:]
    off = offset.split(b"\0")[0].decode("ascii", "replace").strip()
    return f"{t} {off}".strip()


def merge(a: Meta, b: Meta) -> Meta:
    first = a if a.date else b
    return Meta(a.date or b.date, a.gps or b.gps, a.readable or b.readable, first.when, first.src,
                a.where or b.where, a.camera or b.camera)


def xmp_coord(text: bytes) -> float | None:
    """XMP GPS: "64,8.796N", "64,8,47.8N" or "64.1466"."""
    t = text.decode("ascii", "replace").strip()
    sign = -1 if t[-1:] in "SsWw" else 1
    try:
        parts = [float(x) for x in t.rstrip("NSEWnsew").split(",")]
    except ValueError:
        return None
    return sign * sum(v / 60 ** i for i, v in enumerate(parts[:3]))


def xmp_meta(data: bytes, where: str = "XMP") -> Meta:
    m = XMP_DATE.search(data)
    lat, lon = XMP_LAT.search(data), XMP_LON.search(data)
    meta = Meta(valid_ymd(*m.groups()[2:]) if m else None, bool(lat and lon))
    if meta.date:
        meta.when = m.group(2).decode().replace("T", " ")
        meta.src = f"{where} {m.group(1).decode()}"
    if lat and lon:
        y, x = xmp_coord(lat.group(1)), xmp_coord(lon.group(1))
        meta.where = (y, x) if y is not None and x is not None else None
    return meta


def tiff_meta(f, base: int) -> Meta:
    """EXIF in TIFF layout at file offset `base` (JPEG APP1, TIFF/DNG/NEF/CR2/ARW..., HEIC, PNG, WebP)."""
    f.seek(base)
    head = f.read(8)
    if head[:2] == b"II":
        e = "<"
    elif head[:2] == b"MM":
        e = ">"
    else:
        return Meta()
    size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

    def ifd(off):
        if not 0 < off < 1 << 31:
            return {}
        f.seek(base + off)
        raw = f.read(2)
        if len(raw) < 2:
            return {}
        n = min(int.from_bytes(raw, "little" if e == "<" else "big"), 1000)
        data = f.read(12 * n)
        tags = {}
        for i in range(len(data) // 12):
            tag, typ, cnt = struct.unpack(e + "HHI", data[i * 12 : i * 12 + 8])
            tags[tag] = (typ, cnt, data[i * 12 + 8 : i * 12 + 12])
        return tags

    def value(entry):
        typ, cnt, raw = entry
        n = size.get(typ, 1) * min(cnt, 4096)
        if n <= 4:
            return raw[:n]
        f.seek(base + struct.unpack(e + "I", raw)[0])
        return f.read(n)

    def pointer(tags, tag):
        return struct.unpack(e + "I", value(tags[tag])[:4])[0] if tag in tags else 0

    def degrees(entry):
        if entry[0] != 5:
            return 0.0
        nums = struct.unpack(e + "6I", value(entry)[:24])
        return sum(nums[i] / nums[i + 1] / 60 ** (i // 2) for i in (0, 2, 4) if nums[i + 1])

    ifd0 = ifd(struct.unpack(e + "I", head[4:8])[0])
    exif, gps = ifd(pointer(ifd0, 0x8769)), ifd(pointer(ifd0, 0x8825))
    if not gps and 0x0002 in ifd0 and 0x0004 in ifd0:  # a bare GPS directory (Canon CR3's CMT4)
        gps = ifd0
    meta = Meta()
    for tags, tag, off_tag, name in ((exif, 0x9003, 0x9011, "EXIF DateTimeOriginal"),
                                     (exif, 0x9004, 0x9012, "EXIF CreateDate"),
                                     (ifd0, 0x9003, 0x9011, "EXIF DateTimeOriginal"),
                                     (gps, 0x001D, None, "GPS date")):
        if tag in tags and (date := exif_date(raw := value(tags[tag]))):
            meta.date = date
            offset = value(exif[off_tag]) if off_tag in exif else b""
            meta.when, meta.src = exif_when(raw, offset), name
            break
    if 0x0002 in gps and 0x0004 in gps:
        lat, lon = degrees(gps[0x0002]), degrees(gps[0x0004])
        if lat != 0 or lon != 0:
            ref = lambda t: value(gps[t])[:1] if t in gps else b""
            meta.gps = True
            meta.where = (-lat if ref(0x0001) in (b"S", b"s") else lat, -lon if ref(0x0003) in (b"W", b"w") else lon)
    text = lambda t: value(ifd0[t]).split(b"\0")[0].decode("utf-8", "replace").strip() if t in ifd0 else ""
    make, model = text(0x010F), text(0x0110)
    meta.camera = (model if model.lower().startswith(make.lower().split(" ")[0]) else f"{make} {model}").strip() or None
    return meta


def jpeg_meta(f, start: int = 0) -> Meta:
    meta = Meta()
    pos = start + 2
    while True:
        f.seek(pos)
        b = f.read(4)
        if len(b) < 4 or b[0] != 0xFF:
            break
        marker = b[1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0xD9, 0xDA):  # end of image, start of scan: no more metadata
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            pos += 2
            continue
        length = struct.unpack(">H", b[2:4])[0]
        if marker == 0xE1:
            hdr = f.read(29)
            if hdr.startswith(b"Exif\0\0"):
                meta = merge(meta, tiff_meta(f, pos + 10))
            elif hdr.startswith(b"http://ns.adobe.com/xap/1.0/"):
                f.seek(pos + 4)
                meta = merge(meta, xmp_meta(f.read(length - 2)))
        pos += 2 + length
    return meta


def boxes(f, start: int, end: int):
    """ISO base media boxes (MP4, MOV, HEIC, AVIF, CR3) between two offsets: (type, payload start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, typ = struct.unpack(">I4s", f.read(8))
        hl = 8
        if size == 1:
            size, hl = struct.unpack(">Q", f.read(8))[0], 16
        elif size == 0:
            size = end - pos
        if size < hl:
            break
        yield typ, pos + hl, min(pos + size, end)
        pos += size


def heif_meta(f, start: int, end: int) -> Meta:
    """HEIC/AVIF: the 'Exif' item listed in the meta box."""
    meta = Meta()
    exif_ids, locs = set(), {}
    for typ, p, q in boxes(f, start + 4, end):  # meta is a full box: skip version/flags
        f.seek(p)
        if typ == b"iinf":
            v = f.read(1)[0]
            f.read(3)
            f.read(2 if v == 0 else 4)
            for t2, p2, _ in boxes(f, f.tell(), q):
                if t2 == b"infe":
                    f.seek(p2)
                    v2 = f.read(4)[0]
                    if v2 >= 2:
                        item = int.from_bytes(f.read(2 if v2 == 2 else 4), "big")
                        f.read(2)
                        if f.read(4) == b"Exif":
                            exif_ids.add(item)
        elif typ == b"iloc":
            v = f.read(4)[0]
            a, b2 = f.read(2)
            off_size, len_size, base_size, idx_size = a >> 4, a & 15, b2 >> 4, (b2 & 15 if v in (1, 2) else 0)
            rd = lambda n: int.from_bytes(f.read(n), "big") if n else 0
            for _ in range(rd(2 if v < 2 else 4)):
                item = rd(2 if v < 2 else 4)
                if v in (1, 2):
                    rd(2)
                rd(2)
                base_off = rd(base_size)
                extents = [(rd(idx_size), rd(off_size), rd(len_size))[1] for _ in range(rd(2))]
                if extents:
                    locs[item] = base_off + extents[0]
    for item in exif_ids:
        if item in locs:
            f.seek(locs[item])
            skip = struct.unpack(">I", f.read(4))[0]
            meta = merge(meta, tiff_meta(f, locs[item] + 4 + skip))
    return meta


def mp4_meta(f, start: int, end: int) -> Meta:
    """MP4/MOV: creation time in mvhd, Apple creation date and GPS (ISO 6709) in the metadata."""
    if end - start > 64 * 1024 * 1024:
        return Meta(readable=False)
    meta = Meta()
    for typ, p, q in boxes(f, start, end):
        if typ == b"mvhd":
            f.seek(p)
            v = f.read(4)[0]
            secs = int.from_bytes(f.read(8 if v == 1 else 4), "big")
            if secs > 67 * 365 * 86400:  # after 1971; 0 means "not set"
                t = QT_EPOCH + dt.timedelta(seconds=secs)
                meta.date, meta.when, meta.src = t.date().isoformat(), t.strftime("%Y-%m-%d %H:%M:%S UTC"), "QuickTime CreateDate"
            break
    f.seek(start)
    moov = f.read(end - start)
    if not meta.date and b"com.apple.quicktime.creationdate" in moov:
        i = moov.find(b"com.apple.quicktime.creationdate")
        m = re.search(rb"((\d{4})-(\d{2})-(\d{2})T[\d:]+(?:[+-]\d{2}:?\d{2}|Z)?)", moov[i:])
        meta.date = valid_ymd(*m.groups()[1:]) if m else None
        if meta.date:
            meta.when, meta.src = m.group(1).decode().replace("T", " "), "QuickTime CreationDate"
    for key in (b"\xa9xyz", b"com.apple.quicktime.location.ISO6709"):
        i = moov.find(key)
        if i >= 0:
            m = re.search(rb"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", moov[i : i + 200])
            if m and (float(m.group(1)) != 0 or float(m.group(2)) != 0):
                meta.gps, meta.where = True, (float(m.group(1)), float(m.group(2)))
    return meta


def read_meta(path: Path, sidecar: Path | None = None) -> Meta:
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            head = f.read(96)
            if head[:2] == b"\xff\xd8":
                meta = jpeg_meta(f)
            elif head[:2] in (b"II", b"MM"):
                meta = tiff_meta(f, 0)
            elif head.startswith(b"FUJIFILMCCD-RAW"):  # RAF: a JPEG with the EXIF sits inside
                meta = jpeg_meta(f, struct.unpack(">I", head[84:88])[0])
            elif head.startswith(b"\x89PNG\r\n\x1a\n"):
                meta, pos = Meta(), 8
                while pos + 8 <= size:
                    f.seek(pos)
                    n, typ = struct.unpack(">I4s", f.read(8))
                    if typ == b"eXIf":
                        meta = merge(meta, tiff_meta(f, pos + 8 + (6 if f.read(6) == b"Exif\0\0" else 0)))
                    elif typ == b"iTXt" and f.read(17) == b"XML:com.adobe.xmp":
                        f.seek(pos + 8)
                        meta = merge(meta, xmp_meta(f.read(n)))
                    elif typ == b"IEND":
                        break
                    pos += 12 + n
            elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                meta, pos = Meta(), 12
                while pos + 8 <= size:
                    f.seek(pos)
                    typ, n = struct.unpack("<4sI", f.read(8))
                    if typ == b"EXIF":
                        meta = merge(meta, tiff_meta(f, pos + 8 + (6 if f.read(6) == b"Exif\0\0" else 0)))
                    elif typ == b"XMP ":
                        meta = merge(meta, xmp_meta(f.read(n)))
                    pos += 8 + n + (n & 1)
            elif head[4:8] in (b"ftyp", b"moov", b"wide", b"mdat", b"free", b"skip", b"pnot"):
                meta, readable = Meta(), False
                for typ, p, q in boxes(f, 0, size):
                    if typ == b"meta":
                        meta, readable = merge(meta, heif_meta(f, p, q)), True
                    elif typ == b"moov":
                        m = mp4_meta(f, p, q)
                        meta, readable = merge(meta, m), readable or m.readable
                        if head[8:12] == b"crx ":  # Canon CR3: EXIF in CMT2, GPS in CMT4 boxes
                            f.seek(p)
                            blob = f.read(min(q - p, 4 * 1024 * 1024))
                            for tag in (b"CMT2", b"CMT4"):
                                i = blob.find(tag)
                                if i >= 0:
                                    meta = merge(meta, tiff_meta(f, p + i + 4))
                meta.readable = readable
            else:
                meta = Meta(readable=False)
    except (OSError, struct.error, ValueError, IndexError, OverflowError):
        meta = Meta(readable=False)
    if sidecar:
        try:
            side = xmp_meta(sidecar.read_bytes()[: 4 * 1024 * 1024], "sidecar")
            readable = meta.readable or bool(side.date)
            meta = merge(meta, side)
            meta.readable = readable
        except OSError:
            pass
    return meta


# --------------------------------------------------------------------------- files


@dataclass
class MediaFile:
    path: Path
    rel: str
    size: int
    mtime: float
    sidecar: Path | None = None
    sha1: str = ""
    asset_id: str | None = None
    state: str = "pending"  # new | existing | failed
    error: str = ""
    meta: Meta = field(default_factory=Meta)


@dataclass
class Scan:
    media: list[MediaFile] = field(default_factory=list)
    sidecars: list[Path] = field(default_factory=list)
    junk: list[Path] = field(default_factory=list)
    other: list[Path] = field(default_factory=list)


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def scan(root: Path, media_ext: set[str], sidecar_ext: set[str]) -> Scan:
    s = Scan()
    all_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        all_files += [Path(dirpath) / n for n in sorted(filenames)]
    # sidecars by lower-cased path, so IMG_1.JPG finds IMG_1.jpg.XMP too
    sidecars = {str(p).lower(): p for p in all_files if p.suffix.lower() in sidecar_ext}
    used = set()
    for p in all_files:
        ext = p.suffix.lower()
        if ext in media_ext:
            st = p.stat()
            m = MediaFile(p, str(p.relative_to(root)), st.st_size, st.st_mtime)
            # IMG_1.jpg.xmp (darktable, digiKam) or IMG_1.xmp (Lightroom)
            for cand in (p.with_name(p.name + ".xmp"), p.with_name(p.stem + ".xmp")):
                key = str(cand).lower()
                if key in sidecars and key not in used:
                    m.sidecar = sidecars[key]
                    used.add(key)
                    break
            s.media.append(m)
        elif ext in sidecar_ext:
            continue
        elif p.name.lower() in JUNK or p.name.startswith("._"):
            s.junk.append(p)
        else:
            s.other.append(p)
    for key, p in sidecars.items():
        (s.sidecars if key in used else s.other).append(p)
    return s


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def files(n: int) -> str:
    return f"{n} file" if n == 1 else f"{n} files"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


# --------------------------------------------------------------------------- log


def default_key_file() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "immich-upload" / "api-key"


def read_key_file(path: Path, ap) -> str:
    """The API key from a file: its first line, spaces stripped. Warns if others may read it."""
    path = path.expanduser()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        ap.error(f"cannot read the API key file {path}: {e.strerror or e}")
    key = (text.strip().splitlines() or [""])[0].strip()
    if not key:
        ap.error(f"the API key file {path} is empty")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        print(f"Warning: others on this computer can read {path}. Fix with: chmod 600 '{path}'", file=sys.stderr)
    return key


def api_key(args, ap) -> str | None:
    """--api-key, --api-key-file, IMMICH_API_KEY, IMMICH_API_KEY_FILE, then the default key file."""
    if args.api_key and args.api_key_file:
        ap.error("give either --api-key or --api-key-file, not both")
    if args.api_key:
        return args.api_key
    if args.api_key_file:
        return read_key_file(Path(args.api_key_file), ap)
    if os.environ.get("IMMICH_API_KEY"):
        return os.environ["IMMICH_API_KEY"]
    if os.environ.get("IMMICH_API_KEY_FILE"):
        return read_key_file(Path(os.environ["IMMICH_API_KEY_FILE"]), ap)
    return read_key_file(default_key_file(), ap) if default_key_file().is_file() else None


def default_log() -> Path:
    if os.environ.get("IMMICH_UPLOAD_LOG"):
        return Path(os.environ["IMMICH_UPLOAD_LOG"]).expanduser()
    return documents_dir() / LOG_NAME


def documents_dir() -> Path:
    home = Path.home()
    system = platform.system()
    if system == "Windows":
        try:  # the real Documents folder, also when it was moved or redirected
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

            g = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
            guid = GUID(g.fields[0], g.fields[1], g.fields[2], (ctypes.c_ubyte * 8)(*g.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                return Path(path)
        except Exception:
            pass
        return home / "Documents"
    if system == "Linux":  # XDG user dirs, e.g. a localised "Dokumente"
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
        try:
            for line in cfg.read_text().splitlines():
                if line.startswith("XDG_DOCUMENTS_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(home))
                    if val and Path(val) != home:
                        return Path(val)
        except OSError:
            pass
    return home / "Documents"


def cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def split_row(line: str) -> list[str]:
    inner = line.strip()[1:-1] if line.strip().endswith("|") else line.strip()[1:]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", inner)]


class Log:
    """The Markdown log: free text, then one table row per folder. Text around the table is kept."""

    def __init__(self, path: Path):
        self.path = path
        self.before, self.after = LOG_INTRO, ""
        self.rows: dict[str, dict] = {}  # norm(folder) -> {column: value}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        i = next((n for n, l in enumerate(lines) if l.startswith("|") and "Folder" in split_row(l)), None)
        if i is None:  # no table yet: keep the whole text above the new one
            self.before = "\n".join(lines).rstrip() + "\n"
            return
        self.before = "\n".join(lines[:i]).rstrip() + "\n"
        header = split_row(lines[i])
        j = i + 2
        while j < len(lines) and lines[j].startswith("|"):
            r = dict(zip(header, split_row(lines[j])))
            self._merge_old(r)
            j += 1
        self.after = "\n".join(lines[j:]).strip()

    def _merge_old(self, r: dict) -> None:
        """Read a row; rows of the older one-row-per-run layout are folded together."""
        folder = r.get("Folder", "").strip("`")
        if not folder:
            return
        if "Date" in r:  # old layout: Date | Folder | Album | Files | New | Already in Immich | Status | Deleted | Tags
            prev = self.rows.get(norm(folder))
            runs = (run_count(prev["Last run"]) if prev else 0) + 1
            r = {"Folder": r["Folder"], "Album": r.get("Album", ""),
                 "Tags": join_tags((prev or {}).get("Tags", ""), r.get("Tags", "")),
                 "Files": r.get("Files", ""),
                 "Uploaded": str(int_or0((prev or {}).get("Uploaded")) + int_or0(r.get("New"))),
                 "Status": r.get("Status", ""), "Deleted": r.get("Deleted", ""),
                 "First run": (prev or {}).get("First run") or r["Date"],
                 "Last run": last_run(r["Date"], runs)}
        self.rows[norm(folder)] = r

    def get(self, folder: Path) -> dict | None:
        return self.rows.get(norm(str(folder)))

    def record(self, folder: Path, *, album, tags, files, new, status, deleted, now) -> None:
        prev = self.get(folder) or {}
        self.rows[norm(str(folder))] = {
            "Folder": f"`{folder}`", "Album": album, "Tags": join_tags(prev.get("Tags", ""), ", ".join(tags)),
            "Files": str(files), "Uploaded": str(int_or0(prev.get("Uploaded")) + new), "Status": status,
            "Deleted": deleted if deleted != "no" or not prev.get("Deleted") else prev["Deleted"],
            "First run": prev.get("First run") or now,
            "Last run": last_run(now, run_count(prev.get("Last run", "")) + 1),
        }
        self.save()

    def save(self) -> None:
        out = [self.before.rstrip() + "\n", "| " + " | ".join(LOG_COLUMNS) + " |",
               "| " + " | ".join("---" for _ in LOG_COLUMNS) + " |"]
        for _, r in sorted(self.rows.items(), key=lambda kv: kv[1]["Folder"].casefold()):
            out.append("| " + " | ".join(cell(r.get(c, "")) for c in LOG_COLUMNS) + " |")
        text = "\n".join(out) + "\n" + (f"\n{self.after}\n" if self.after else "")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, self.path)  # never leaves a half-written log behind


def int_or0(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def run_count(last: str) -> int:
    m = re.search(r"\((\d+) runs\)", last or "")
    return int(m.group(1)) if m else (1 if last else 0)


def last_run(when: str, runs: int) -> str:
    return when if runs <= 1 else f"{when} ({runs} runs)"


def join_tags(*lists: str) -> str:
    tags = [t.strip() for l in lists for t in l.split(",") if t.strip()]
    return ", ".join(dict.fromkeys(tags))


def norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


# --------------------------------------------------------------------------- main work


class Progress:
    def __init__(self, total_files: int, total_bytes: int, label: str):
        self.n = self.bytes = 0
        self.total_files, self.total_bytes, self.label = total_files, total_bytes, label
        self.lock = threading.Lock()
        self.last = 0.0
        self.start = time.monotonic()

    def step(self, size: int, force=False):
        with self.lock:
            self.n += 1
            self.bytes += size
            now = time.monotonic()
            if force or now - self.last > 2 or self.n == self.total_files:
                self.last = now
                rate = self.bytes / max(now - self.start, 0.001)
                print(f"\r  {self.label} {self.n}/{self.total_files} files, "
                      f"{human(self.bytes)} of {human(self.total_bytes)} ({human(rate)}/s)   ",
                      end="", flush=True)
                if self.n == self.total_files:
                    print()


def process(folder: Path, args, api: Immich, media_ext, sidecar_ext, log: Log) -> bool:
    row = log.get(folder)
    album = args.album or (row or {}).get("Album") or folder.name  # a rerun keeps the folder's album
    print(f"\n== {folder}  ->  album '{album}'" + (f", tags {', '.join(args.tag)}" if args.tag else ""))
    s = scan(folder, media_ext, sidecar_ext)
    if row and row.get("Status") == "verified" and not (args.force or args.delete):
        if row.get("Files") == str(len(s.media)):
            print(f"  Already uploaded and verified ({row['Files']} files, see the log). "
                  "Skipping; use --force to redo.")
            return True
        print(f"  Verified before with {row.get('Files')} files, now {len(s.media)}: checking again.")

    total = sum(m.size for m in s.media)
    print(f"  {len(s.media)} photos/videos ({human(total)}), {len(s.sidecars)} sidecars, "
          f"{len(s.junk)} junk files, {len(s.other)} other files")
    for p in s.other[:20]:
        print(f"    not uploaded: {p.relative_to(folder)}")
    if len(s.other) > 20:
        print(f"    ... and {len(s.other) - 20} more")
    if not s.media:
        print("  Nothing to upload.")
        return True

    # 1. checksums, 2. what Immich already has
    in_immich(s.media, args, api)
    by_id = {str(i): m for i, m in enumerate(s.media)}
    todo = [m for m in s.media if m.state == "pending"]
    existing = [m for m in s.media if m.state == "existing"]
    print(f"  {len(existing)} already in Immich, {len(todo)} to upload")

    # 3. dates and places. Files already in Immich keep theirs, unless --force (a re-upload) is given.
    cands = [m for m in s.media if m.state != "failed"] if args.force else todo
    if existing and not args.force and (args.date or args.place):
        print(f"  The {files(len(existing))} already in Immich keep their date and place (--force to redo them).")
    plan = meta_plan(cands, args, folder, s.media)
    if not plan_ok(plan, args):
        print("  Skipped, nothing uploaded or changed.")
        return False
    if plan.stop:
        print(f"  Stopped before uploading: Immich would date {'this file' if len(plan.undated) == 1 else 'these files'}"
              " by the file time. Give a date with --date, or upload anyway with --allow-undated.")
        log.record(folder, album=album, tags=[], files=len(s.media), new=0,
                   status=f"stopped: {len(plan.undated)} without date", deleted="no",
                   now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        print(f"  Logged to {args.log}")
        return False

    # 3. upload
    def up(m: MediaFile):
        for attempt in range(1, 4):
            try:
                r = api.upload(m)
                m.asset_id = r["id"]
                m.state = "new" if r["status"] == "created" else "existing"
                return
            except ApiError as e:
                if e.status < 500 and e.status != 429:
                    m.state, m.error = "failed", str(e)
                    return
                m.error = str(e)
            except (OSError, http.client.HTTPException) as e:
                m.error = f"{type(e).__name__}: {e}"
            time.sleep(2 * attempt)
        m.state = "failed"

    if todo:
        prog = Progress(len(todo), sum(m.size for m in todo), "upload")
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
            for m in todo:
                ex.submit(lambda m=m: (up(m), prog.step(m.size)))

    # 4. verify: every checksum must now map to the asset we have, and not be in the trash
    problems = []
    ok_ids = []
    for cid, res in api.check([(cid, m.sha1) for cid, m in by_id.items() if m.state != "failed"]).items():
        m = by_id[cid]
        if res["action"] != "reject" or res.get("reason") != "duplicate":
            m.state, m.error = "failed", "not found in Immich after upload"
        elif m.asset_id and res.get("assetId") != m.asset_id:
            m.state, m.error = "failed", f"checksum belongs to another asset ({res.get('assetId')})"
        elif res.get("isTrashed"):
            m.state, m.error = "failed", "in Immich, but in the trash (restore it there first)"
        else:
            m.asset_id = res.get("assetId") or m.asset_id
            ok_ids.append(m.asset_id)
    for m in s.media:
        if m.state == "failed":
            problems.append(f"{m.rel}: {m.error}")

    # 5. album
    album_ok = True
    if ok_ids:
        try:
            aid = api.album_id(album, create=True)
            bad = api.add_to_album(aid, list(dict.fromkeys(ok_ids)))
            for b in bad:
                problems.append(f"album: asset {b['id']}: {b.get('error')} {b.get('errorMessage', '')}".strip())
            album_ok = not bad
        except ApiError as e:
            problems.append(f"album '{album}': {e}")
            album_ok = False

    # 6. tags
    tags_ok = True
    if ok_ids and args.tag_ids:
        try:
            api.tag_assets(list(args.tag_ids.values()), list(dict.fromkeys(ok_ids)))
        except ApiError as e:
            problems.append(f"tags: {e}")
            tags_ok = False

    # 7. date and place, set in Immich; the files stay as they are, so their checksums still match
    meta_ok = True
    if plan.dates or plan.places:
        meta_ok = set_date_place(api, args, plan, [m for m in s.media if m.state != "failed"], problems)

    new = sum(m.state == "new" for m in s.media)
    existing = sum(m.state == "existing" for m in s.media)
    failed = sum(m.state == "failed" for m in s.media)
    verified_all = failed == 0 and album_ok and tags_ok and meta_ok
    print(f"  Result: {new} uploaded, {existing} already in Immich, {failed} failed"
          + ("" if album_ok else ", album incomplete") + ("" if tags_ok else ", tags not set")
          + ("" if meta_ok else ", date/place not set"))
    for p in problems[:30]:
        print(f"    ! {p}")
    if len(problems) > 30:
        print(f"    ! ... and {len(problems) - 30} more")

    # 8. delete
    deleted = "no"
    if args.delete and not verified_all:
        print("  Not deleting anything: not every file checked out.")
    elif args.delete:
        deleted = delete(folder, s)

    status = ("verified" if verified_all else f"{failed} failed" if failed
              else "album incomplete" if not album_ok else "tags not set" if not tags_ok
              else "date/place not set")
    log.record(folder, album=album, tags=args.tag, files=len(s.media), new=new, status=status,
               deleted=deleted, now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    print(f"  Logged to {args.log}")
    return verified_all


# --------------------------------------------------------------------------- --date / --place, set in Immich

# The photo and video types Immich takes (from its mime.ts), for a dry run without a server.
MEDIA_EXT = set(
    ".3fr .ari .arw .cap .cin .cr2 .cr3 .crw .dcr .dng .erf .fff .iiq .k25 .kdc .mrw .nef .nrw .orf .ori .pef "
    ".psd .raf .raw .rw2 .rwl .sr2 .srf .srw .x3f .avif .bmp .gif .jfif .jpeg .jpg .png .webp .heic .heif .hif "
    ".insp .jp2 .jpe .jxl .mpo .svg .tif .tiff .3gp .3gpp .avi .flv .insv .m2t .m2ts .m4v .mkv .mov .mp4 .mpe "
    ".mpeg .mpg .mts .mxf .ts .vob .webm .wmv".split())


def in_immich(media: list[MediaFile], args, api: Immich) -> None:
    """Checksum every file and mark the ones Immich already has (state 'existing') or rejects ('failed')."""
    prog = Progress(len(media), sum(m.size for m in media), "checksums")
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        for m, h in zip(media, ex.map(lambda m: sha1(m.path), media)):
            m.sha1 = h
            prog.step(m.size)
    by_id = {str(i): m for i, m in enumerate(media)}
    for cid, res in api.check([(cid, m.sha1) for cid, m in by_id.items()]).items():
        m = by_id[cid]
        if res["action"] == "reject" and res.get("reason") == "duplicate":
            m.state, m.asset_id = "existing", res.get("assetId")
        elif res["action"] == "reject":
            m.state, m.error = "failed", f"rejected by Immich: {res.get('reason')}"


def read_all(media: list[MediaFile], jobs: int) -> None:
    with concurrent.futures.ThreadPoolExecutor(jobs) as ex:
        for m, md in zip(media, ex.map(lambda m: read_meta(m.path, m.sidecar), media)):
            m.meta = md


@dataclass
class MetaPlan:
    cands: list[MediaFile]  # the files whose date/place count: to upload, or with --force all of them
    undated: list[MediaFile]
    nogps: list[MediaFile]
    have_date: list[MediaFile]  # with --date, when some files have a date and others not
    have_place: list[MediaFile]  # with --place, likewise
    dates: dict[str, dt.datetime]  # rel -> date to set (from --date-from-name or --date)
    places: set[str]  # rel of the files to get the place
    stop: bool  # files without a date and neither --date nor --allow-undated
    named: dict[str, str] = field(default_factory=dict)  # rel -> where --date-from-name found its date
    notes: list[str] = field(default_factory=list)  # why --date-from-name did not use a name
    agree: list[MediaFile] = field(default_factory=list)  # their own date is --date or more precise: kept


# Dates in names, for --date-from-name: "IMG_20190714_153012", "IMG-20190714-WA0001", "2019-07-14 15.30.12",
# "Screenshot_2019-07-14-15-30-12"; folders like "2019-07-14 Iceland", "2019-07 Iceland", "Rome 1998",
# or a range "2019-07-14 - 2019-07-20".
NAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d\d)([-_.]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])"
                       r"(?:[-_ T.]?([01]\d|2[0-3])[-_.:h]?([0-5]\d)[-_.:m]?([0-5]\d)|(?!\d))")
NAME_YM = re.compile(r"(?<!\d)((?:19|20)\d\d)[-_. ](0[1-9]|1[0-2])(?!\d)")
NAME_Y = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")
DAY = dt.timedelta(days=1)


def name_dates(text: str) -> list[tuple[dt.datetime, bool]]:
    """All dates in a name, each with whether it has a time of day."""
    out = []
    for m in NAME_DATE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(3)), int(m.group(4))
        if not 1900 <= y <= dt.date.today().year + 1:
            continue
        try:
            if m.group(5):
                out.append((dt.datetime(y, mo, d, int(m.group(5)), int(m.group(6)), int(m.group(7))), True))
            else:
                out.append((dt.datetime(y, mo, d, 12), False))
        except ValueError:
            pass
    return out


def folder_period(name: str) -> tuple[dt.date, dt.date, str] | None:
    """The day, month, year or range a folder name gives: (first day, last day, as written)."""
    full = name_dates(name)
    if full:
        a, b = full[0][0].date(), full[min(1, len(full) - 1)][0].date()
        a, b = min(a, b), max(a, b)
        return a, b, a.isoformat() if a == b else f"{a.isoformat()} to {b.isoformat()}"
    this_year = dt.date.today().year + 1
    m = NAME_YM.search(name)
    if m and 1900 <= int(m.group(1)) <= this_year:
        y, mo = int(m.group(1)), int(m.group(2))
        nxt = dt.date(y + (mo == 12), mo % 12 + 1, 1)
        return dt.date(y, mo, 1), nxt - DAY, f"{y}-{mo:02d}"
    m = NAME_Y.search(name)
    if m and 1900 <= int(m.group(1)) <= this_year:
        y = int(m.group(1))
        return dt.date(y, 1, 1), dt.date(y, 12, 31), str(y)
    return None


def near(day: str, a: dt.date, b: dt.date) -> bool:
    """An EXIF day (YYYY-MM-DD) inside a..b, give or take a day (time zones, parties past midnight)."""
    d = dt.date.fromisoformat(day)
    return a - DAY <= d <= b + DAY


def dates_from_names(root: Path, media: list[MediaFile], wanted: list[MediaFile], cands: list[MediaFile]):
    """--date-from-name for the files in `wanted` (those without a date).

    1. A folder with a date in its name dates the undated files in it (and its subfolders), if no file
       in it has an EXIF date outside that day, month, year or range. A file whose own name has a date
       inside that period gets the date from its name instead, as it is more precise.
    2. The files no folder dated get the date in their file name, if every one of them not yet in Immich has a date in
       its name or EXIF, and no EXIF date contradicts the name.
    Returns {rel: date}, {rel: where it came from} and notes on why names were not used."""
    dates, src, notes = {}, {}, []
    if not wanted:
        return dates, src, notes
    want = {m.rel for m in wanted}
    groups: dict[str, list[MediaFile]] = {}
    periods = {}
    for m in media:
        parts = [root.name] + list(Path(m.rel).parts[:-1])
        for i in range(len(parts), 0, -1):  # the innermost folder with a date in its name
            if (p := folder_period(parts[i - 1])):
                key = "/".join(parts[:i])
                periods[key] = p
                groups.setdefault(key, []).append(m)
                break

    covered = set()
    for key, group in sorted(groups.items()):
        a, b, label = periods[key]
        folder = key.split("/")[-1]
        bad = [m for m in group if m.meta.date and not near(m.meta.date, a, b)]
        if bad:
            notes.append(f"folder '{folder}' ({label}) not used: {files(len(bad))} in it {'has' if len(bad) == 1 else 'have'} "
                         f"an EXIF date outside it, e.g. {bad[0].rel} ({bad[0].meta.date})")
            continue
        covered |= {m.rel for m in group}
        n = 0
        for m in sorted((m for m in group if m.rel in want), key=lambda m: m.rel.lower()):
            own = [d for d, _ in name_dates(m.path.name) if a <= d.date() <= b]
            if own:
                dates[m.rel], src[m.rel] = own[0], "file name"
            else:  # noon of the first day, one second apart in file name order
                dates[m.rel] = dt.datetime.combine(a, dt.time(12)) + dt.timedelta(seconds=n)
                src[m.rel] = f"folder '{folder}'"
                n += 1

    rest = [m for m in media if m.rel not in covered]
    if any(m.rel in want for m in rest):
        named = {m.rel: name_dates(m.path.name) for m in rest}
        counted = {m.rel for m in cands}  # files already in Immich need no date
        missing = [m for m in rest if m.rel in counted and not m.meta.date and not named[m.rel]]
        contra = [m for m in rest if m.meta.date and named[m.rel]
                  and not near(m.meta.date, named[m.rel][0][0].date(), named[m.rel][0][0].date())]
        if missing:
            notes.append(f"file names not used: {files(len(missing))} {'has' if len(missing) == 1 else 'have'} "
                         f"no date in the name or EXIF, e.g. {missing[0].rel}")
        elif contra:
            m = contra[0]
            notes.append(f"file names not used: {files(len(contra))} {'has' if len(contra) == 1 else 'have'} an EXIF date "
                         f"that contradicts the name, e.g. {m.rel} (EXIF {m.meta.date})")
        else:
            per_day: dict[dt.date, int] = {}
            for m in sorted((m for m in rest if m.rel in want), key=lambda m: m.rel.lower()):
                d, has_time = named[m.rel][0]
                if not has_time:  # e.g. WhatsApp: noon, one second apart in file name order
                    k = per_day.get(d.date(), 0)
                    per_day[d.date()] = k + 1
                    d += dt.timedelta(seconds=k)
                dates[m.rel], src[m.rel] = d, "file name"
    return {r: d.astimezone() for r, d in dates.items()}, src, notes


def agrees(when: str | None, args) -> bool:
    """A date ("YYYY-MM-DD[ HH:MM:SS...]") that is --date or a more precise form of it: --date 2019-07
    agrees with 2019-07-14 15:30, but not with 2019-08-01 nor with a plain 2019 (less precise)."""
    want = args.date.strftime("%Y-%m-%d %H:%M:%S")[: args.date_prec]
    return bool(when) and len(when) >= args.date_prec and when[: args.date_prec] == want


def meta_plan(cands: list[MediaFile], args, root: Path, media: list[MediaFile]) -> MetaPlan:
    """What the run will do about dates and places. `cands` are the files that count (the ones to upload,
    with --force all); `media` are all files of the folder, which --date-from-name checks against."""
    from_name = args.date_from_name and not (args.force and args.date)  # --force --date: one date for all
    read_all(media if from_name else cands, args.jobs)
    named_dates, named, notes = ({}, {}, [])
    if from_name:
        named_dates, named, notes = dates_from_names(root, media, [m for m in cands if not m.meta.date], cands)
    undated = [m for m in cands if not m.meta.date and m.rel not in named_dates]
    nogps = [m for m in cands if not m.meta.gps]

    def when(m):  # the file's own date, as precise as it is known
        if m.rel in named_dates:
            return f"{named_dates[m.rel]:%Y-%m-%d %H:%M:%S}"
        return (m.meta.when or m.meta.date or "")[:19] if m.meta.date else None

    agree = [m for m in cands if args.date and when(m) and agrees(when(m), args)]  # these keep their date
    set_date = ([m for m in cands if m not in agree] if args.force else undated) if args.date else []
    dates = {}  # one second apart in file name order, so scans keep their order in the timeline
    for m in sorted(set_date, key=lambda m: m.rel.lower()):
        dates.setdefault(m.rel, args.date + dt.timedelta(seconds=len(dates)))
    dates.update(named_dates)
    places = {m.rel for m in (cands if args.force else nogps)} if args.place else set()
    have_date = [m for m in cands if when(m) and m not in agree] if args.date and undated else []
    have_place = [m for m in cands if m.meta.gps] if args.place and nogps else []
    stop = bool(undated) and not args.date and not args.allow_undated
    return MetaPlan(cands, undated, nogps, have_date, have_place, dates, places, stop, named, notes, agree)


def plan_ok(plan: MetaPlan, args) -> bool:
    """Print the dates of the files to upload and what --date/--place will do; ask if that is mixed."""
    dates = sorted(m.meta.date for m in plan.cands if m.meta.date)
    if dates:
        print(f"  Taken {dates[0]}" + (f" to {dates[-1]}" if dates[-1] != dates[0] else "")
              + (f"; {len(plan.undated)} without a date" if plan.undated else ""))
    elif plan.cands:
        print("  No file has a date")
    if args.date_from_name:
        by_src: dict[str, int] = {}
        for src in plan.named.values():
            by_src[src] = by_src.get(src, 0) + 1
        if by_src:
            print("  Taking dates from names: "
                  + ", ".join(f"{files(n)} from {src if src != 'file name' else 'their file name'}"
                              for src, n in sorted(by_src.items())))
        for note in plan.notes:
            print(f"  --date-from-name: {note}")
    show("no date:", [m.rel + ("" if m.meta.readable else "  (file type not checked)") for m in plan.undated], 20)
    if plan.stop:
        return True
    will = "Will set"
    if args.date and plan.agree:
        print(f"  {files(len(plan.agree))} already {'has' if len(plan.agree) == 1 else 'have'} a date matching "
              f"--date {args.date.strftime('%Y-%m-%d %H:%M:%S')[:args.date_prec]} and "
              f"{'keeps' if len(plan.agree) == 1 else 'keep'} it")
    if args.date:
        fill = [r for r in plan.dates if r not in plan.named]
        if fill:
            over = sum(bool(m.meta.date) for m in plan.cands if m.rel in fill)
            print(f"  {will} the date {args.date.isoformat()} on {files(len(fill))}"
                  + (", one second apart in file name order" if len(fill) > 1 else "")
                  + (f" (--force: overwriting the date of {over})" if over else ""))
        else:
            print("  Every file to upload has a date; --date is not needed here.")
    if args.place:
        if plan.places:
            over = sum(m.meta.gps for m in plan.cands if m.rel in plan.places)
            print(f"  {will} the place {args.place_name} on {files(len(plan.places))}"
                  + (f" (--force: overwriting the location of {over})" if over else ""))
        else:
            print("  Every file to upload has a location; --place is not needed here.")
    from_folder = [r for r, src in plan.named.items() if src.startswith("folder")]
    if args.force or not (plan.have_date or plan.have_place or from_folder):
        return True
    warn, asks = [], []
    if from_folder:
        warn.append(f"{files(len(from_folder))} dated from the folder name only")
        show("from folder:", [f"{r}  -> {plan.dates[r]:%Y-%m-%d %H:%M:%S}  ({plan.named[r]})" for r in from_folder])
        asks.append("use these folder dates")
    if plan.have_date or plan.have_place:
        if plan.have_date:
            warn.append(f"{len(plan.have_date)} of {files(len(plan.cands))} already "
                        f"{'has' if len(plan.have_date) == 1 else 'have'} a date")
            show("has a date:", [f"{m.rel}  ({m.meta.when or m.meta.date})" for m in plan.have_date])
        if plan.have_place:
            warn.append(f"{len(plan.have_place)} of {files(len(plan.cands))} already "
                        f"{'has' if len(plan.have_place) == 1 else 'have'} a location")
            show("has a location:", [m.rel for m in plan.have_place])
        asks.append("set --date/--place only on the others")
    return confirm(f"{'; '.join(warn)}. Upload, and {' and '.join(asks)}?")


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        print(f"  ! {question} -> no (cannot ask: no terminal). Use --force to go ahead without asking.")
        return False
    try:
        return input(f"  ! {question} [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
    except EOFError:
        return False


def show(label: str, items: list[str], limit: int = 10) -> None:
    for x in items[:limit]:
        print(f"    {label} {x}")
    if len(items) > limit:
        print(f"    ... and {len(items) - limit} more")


def set_date_place(api: Immich, args, plan: MetaPlan, done: list[MediaFile], problems) -> bool:
    """Set the planned dates and place in Immich, then read them back. Immich keeps such edits in its
    database and an .xmp next to its copy; the original stays as it is."""
    want: dict[str, dict] = {}
    place_ids = []
    for m in done:
        if not m.asset_id:
            continue
        if m.rel in plan.dates and m.asset_id not in want:  # the same photo twice in a folder gets one date
            want[m.asset_id] = {"date": plan.dates[m.rel], "rel": m.rel}
        if m.rel in plan.places and m.asset_id not in place_ids:
            place_ids.append(m.asset_id)
    rel = {m.asset_id: m.rel for m in done if m.asset_id}
    errors = []

    def put_date(item):
        aid, w = item
        try:
            api.request("PUT", f"/assets/{aid}", {"dateTimeOriginal": w["date"].isoformat()})
        except ApiError as e:
            errors.append(f"{w['rel']}: date not set ({e})")

    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(put_date, want.items()))
    if place_ids:
        lat, lon = args.place
        for i in range(0, len(place_ids), BATCH):
            try:
                api.request("PUT", "/assets", {"ids": place_ids[i : i + BATCH], "latitude": lat, "longitude": lon})
            except ApiError as e:
                errors.append(f"place not set on {files(len(place_ids[i:i + BATCH]))} ({e})")

    def verify(aid):
        try:
            x = (api.request("GET", f"/assets/{aid}") or {}).get("exifInfo") or {}
        except ApiError as e:
            return f"{rel[aid]}: cannot read back ({e})"
        if aid in want:
            got = x.get("dateTimeOriginal") or ""
            try:
                ok = abs(dt.datetime.fromisoformat(got.replace("Z", "+00:00")) - want[aid]["date"]) < dt.timedelta(seconds=2)
            except ValueError:
                ok = False
            if not ok:
                return f"{rel[aid]}: date reads back as {got or 'nothing'}"
        if aid in place_ids and not (abs((x.get("latitude") or 999) - args.place[0]) < 1e-4
                                     and abs((x.get("longitude") or 999) - args.place[1]) < 1e-4):
            return f"{rel[aid]}: location reads back as {x.get('latitude')}, {x.get('longitude')}"
        return None

    if not errors:
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
            errors += [e for e in ex.map(verify, list(dict.fromkeys(list(want) + place_ids))) if e]
    problems += errors
    if not errors:
        parts = ([f"date on {files(len(want))}"] if want else []) + ([f"location on {files(len(place_ids))}"] if place_ids else [])
        if parts:
            print(f"  Set in Immich and checked: {', '.join(parts)}")
    return not errors


def parse_date(text: str) -> tuple[dt.datetime, int]:
    """YYYY, YYYY-MM, YYYY-MM-DD, optionally with a time (HH:MM[:SS]) and a UTC offset.
    Returns the date and how much of it was given: 4 (year), 7 (month), 10 (day), 16 (minute) or
    19 (second) characters of "YYYY-MM-DD HH:MM:SS"."""
    t = text.strip()
    m = re.fullmatch(r"(.*?)\s*(Z|[+-]\d{2}:\d{2})", t)  # an offset also works after a plain date
    t, offset = (m.group(1), m.group(2).replace("Z", "+00:00")) if m else (t, "")
    prec = 19 if re.match(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", t) else 16
    if re.fullmatch(r"\d{4}", t):
        t, prec = t + "-01-01", 4
    elif re.fullmatch(r"\d{4}-\d{2}", t):
        t, prec = t + "-01", 7
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        t, prec = t + " 12:00", min(prec, 10)  # noon, so no time zone can move it to another day
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", t):
            raise ValueError
        d = dt.datetime.fromisoformat(t + offset)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date: {text!r} (use e.g. 2019-07-14 or \"2019-07-14 15:30\")")
    return (d if d.tzinfo else d.astimezone()), prec  # no offset given: this computer's time zone


def parse_place(text: str) -> tuple[float, float, str]:
    """'64.1466, -21.9426' or a place name, looked up on OpenStreetMap."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*", text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise SystemExit(f"Coordinates out of range: {text}")
        return lat, lon, f"{lat:.5f}, {lon:.5f}"
    import urllib.request
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": text, "format": "jsonv2", "limit": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "immich-upload.py (personal photo migration)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            hits = json.load(r)
    except OSError as e:
        raise SystemExit(f"Could not look up the place {text!r} on OpenStreetMap ({e}). Give coordinates instead.")
    if not hits:
        raise SystemExit(f"OpenStreetMap does not know the place {text!r}. Try another spelling or coordinates.")
    lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
    return lat, lon, f"{hits[0]['display_name']} ({lat:.5f}, {lon:.5f})"


def delete(folder: Path, s: Scan) -> str:
    removed = 0
    for p in [m.path for m in s.media] + s.sidecars + s.junk:
        try:
            try:
                p.unlink()
            except PermissionError:  # read-only file on Windows
                os.chmod(p, stat.S_IWRITE)
                p.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"    ! could not delete {p}: {e}")
    # empty folders, deepest first
    for dirpath, _, _ in sorted(os.walk(folder), key=lambda t: -len(t[0])):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    if folder.exists():
        left = sum(len(f) for _, _, f in os.walk(folder))
        print(f"  Deleted {removed} files; the folder stays because {left} other files are still in it.")
        return f"files only, {left} others kept"
    print(f"  Deleted the folder ({removed} files).")
    return "yes"


def list_albums(api: Immich) -> int:
    try:
        albums = api.request("GET", "/albums") or []
    except ApiError as e:
        print(f"Immich refused the request ({e}). Check the URL and the API key (it needs album.read).", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Cannot reach Immich: {e}", file=sys.stderr)
        return 2
    if not albums:
        print("No albums.")
        return 0

    def dates(a):
        start, end = ((a.get(k) or "")[:10] for k in ("startDate", "endDate"))  # empty albums have none
        if not start:
            return ""
        return start if start == end else f"{start} to {end}"

    rows = [(a["albumName"], str(a.get("assetCount", "")), dates(a), "shared" if a.get("shared") else "")
            for a in sorted(albums, key=lambda a: a["albumName"].casefold())]
    head = ("Album", "Assets", "Dates", "")
    widths = [max(len(r[i]) for r in rows + [head]) for i in range(4)]
    fmt = f"{{:<{widths[0]}}}  {{:>{widths[1]}}}  {{:<{widths[2]}}}  {{}}"
    print(fmt.format(*head).rstrip())
    for r in rows:
        print(fmt.format(*r).rstrip())
    total = sum(a.get("assetCount", 0) for a in albums)
    print(f"\n{len(albums)} albums, {total} assets in them (an asset can be in several albums)")
    return 0


def list_tags(api: Immich) -> int:
    try:
        tags = api.tags()
    except ApiError as e:
        print(f"Immich refused the request ({e}). Check the URL and the API key (it needs tag.read).", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Cannot reach Immich: {e}", file=sys.stderr)
        return 2
    if not tags:
        print("No tags.")
        return 0
    for t in sorted(tags, key=lambda t: t["value"].casefold()):
        depth = t["value"].count("/")
        print("  " * depth + t["name"] + (f"  ({t['color']})" if t.get("color") else ""))
    print(f"\n{len(tags)} tags. Use the full path with --tag, e.g. --tag \"{max(tags, key=lambda t: t['value'].count('/'))['value']}\"")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # odd file names must not crash a Windows console
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(
        description="Upload folders of photos and videos to Immich, verify them and log each folder.",
        epilog="Examples: immich-upload.py --dry-run -t Travel/Iceland \"D:\\Photos\\2019 Iceland\"   |   "
               "immich-upload.py --date 1998-08 --place \"Rome, Italy\" \"D:\\Scans\\Rome 1998\"   |   "
               "immich-upload.py --list-albums --list-tags")
    ap.add_argument("folders", nargs="*", type=Path, metavar="FOLDER", help="folder(s) to upload, with subfolders")
    ap.add_argument("-l", "--list-albums", action="store_true",
                    help="list the albums in Immich and exit (uploads nothing)")
    ap.add_argument("--list-tags", action="store_true", help="list the tags in Immich and exit (uploads nothing)")
    ap.add_argument("-L", "--list-files", action="store_true", help="the same as --dry-run")
    ap.add_argument("-a", "--album",
                    help="album name (default: the album the log has for the folder, else the folder's name)")
    ap.add_argument("-t", "--tag", action="append", default=[], metavar="TAG",
                    help="tag every file with TAG; repeat for more tags; nest with '/', e.g. Travel/Iceland. "
                         "Missing tags are created")
    ap.add_argument("--date", type=parse_date, metavar="DATE",
                    help="set this date in Immich on the uploaded files that have none, e.g. 2019-07-14, "
                         "\"2019-07-14 15:30\", 2019-07 or 1987 (without a time: 12:00; without an offset: "
                         "this computer's time zone). Asks first if some files have a date and others not. "
                         "Files already in Immich keep theirs")
    ap.add_argument("--place", metavar="PLACE",
                    help="set this location in Immich on the uploaded files that have none: "
                         "\"64.1466, -21.9426\" or a place name, looked up on OpenStreetMap, e.g. "
                         "\"Reykjavik, Iceland\". Asks like --date")
    ap.add_argument("--date-from-name", action="store_true",
                    help="date files that have none from the folder name (\"2019-07 Iceland\", \"Rome 1998\"), "
                         "if no file in that folder has an EXIF date outside it; else from the file name "
                         "(IMG_20190714_153012, IMG-20190714-WA0001), if every file has a date in its name or "
                         "EXIF and none contradicts its name. --date covers what is left")
    ap.add_argument("--allow-undated", action="store_true",
                    help="upload files without a date anyway (Immich then uses the file time)")
    ap.add_argument("--delete", action="store_true",
                    help="delete the uploaded files and the folder once everything is verified")
    ap.add_argument("--log", type=Path, default=default_log(),
                    help=f"Markdown log of uploaded folders (default: {default_log()})")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="change nothing: list each file with its date, location and camera, and what a run with "
                         "the same options would do (with a server set: also which files Immich has)")
    ap.add_argument("-f", "--force", action="store_true",
                    help="redo folders the log lists as verified, as a re-upload: --date/--place then apply to "
                         "every file, also those already in Immich, overwriting their date/place, without asking")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="parallel uploads (default: 4)")
    ap.add_argument("--server", default=os.environ.get("IMMICH_URL"), help="Immich URL (or IMMICH_URL)")
    ap.add_argument("--api-key", help="API key (or IMMICH_API_KEY). Visible to others in the process list: "
                                      "prefer --api-key-file")
    ap.add_argument("--api-key-file", metavar="FILE",
                    help=f"file holding the API key (or IMMICH_API_KEY_FILE; default: {default_key_file()}, if it exists)")
    args = ap.parse_args()
    args.api_key = api_key(args, ap)
    args.date, args.date_prec = args.date or (None, 0)

    if args.dry_run or args.list_files:
        return preview_main(ap, args)
    if not args.server:
        ap.error("no Immich URL: set IMMICH_URL or use --server")
    if not args.api_key:
        ap.error(f"no API key: save it in {default_key_file()}, or use --api-key-file or IMMICH_API_KEY")
    if args.list_albums or args.list_tags:
        api = Immich(args.server, args.api_key)
        rc = list_albums(api) if args.list_albums else 0
        if args.list_albums and args.list_tags:
            print()
        return max(rc, list_tags(api) if args.list_tags else 0)
    args.tag = list(dict.fromkeys(t.strip().strip("/") for t in args.tag if t.strip().strip("/")))
    folders = check_folders(ap, args)
    if args.place:
        lat, lon, args.place_name = parse_place(args.place)
        args.place = (lat, lon)
        print(f"Place: {args.place_name}")
    if args.album is None and len({f.name for f in folders}) < len(folders):
        print("Note: some folders share a name, so they will share an album.")

    api = Immich(args.server, args.api_key)
    try:
        media_ext, sidecar_ext = api.media_types()
        api.request("GET", "/albums", query={"name": "immich-upload key check"})  # checks the key works
    except ApiError as e:
        print(f"Immich refused the request ({e}). Check the URL and the API key's permissions.", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Cannot reach Immich at {args.server}: {e}", file=sys.stderr)
        return 2

    args.tag_ids = {}
    if args.tag:
        try:
            existing = {t["value"] for t in api.tags()}
            missing = [t for t in args.tag if t not in existing]
            args.tag_ids = api.upsert_tags(args.tag)
            if missing:
                print(f"Created tag(s): {', '.join(missing)}")
        except ApiError as e:
            print(f"Cannot set up the tags ({e}). The API key needs tag.read, tag.create and tag.asset.",
                  file=sys.stderr)
            return 2

    log = Log(args.log)
    ok = True
    for f in folders:
        try:
            ok = process(f, args, api, media_ext, sidecar_ext, log) and ok
        except ApiError as e:
            print(f"  Immich error, folder not finished: {e}")
            ok = False
    return 0 if ok else 1


def preview(folder: Path, args, log: Log, api: Immich | None, media_ext, sidecar_ext) -> bool:
    """-n / -L: each file with its date, place and camera, and what a run with the same options would do.
    Returns False if that run would stop or ask before uploading."""
    s = scan(folder, media_ext, sidecar_ext)
    read_all(s.media, args.jobs)
    row = log.get(folder)
    album = args.album or (row or {}).get("Album") or folder.name
    print(f"\n== {folder}")
    state = "not checked"
    if api:
        try:
            state = "exists" if api.album_id(album, create=False) else "new"
        except ApiError as e:
            state = f"cannot check: {e}"
    print(f"  Album '{album}' ({state})" + (f", {'tag' if len(args.tag) == 1 else 'tags'} {', '.join(args.tag)}"
                                           if args.tag else ""))
    if row:
        print(f"  Log: {row.get('Status')}, {row.get('Uploaded')} of {row.get('Files')} files uploaded by this "
              f"script, last run {row.get('Last run')}")
    skip = (row and row.get("Status") == "verified" and row.get("Files") == str(len(s.media))
            and not (args.force or args.delete))
    if api and s.media and not skip:
        in_immich(s.media, args, api)
    cands = [m for m in s.media if m.state != "failed"] if args.force else [m for m in s.media if m.state == "pending"]
    plan = meta_plan(cands, args, folder, s.media)

    def what(m: MediaFile) -> str:
        if skip:
            return "skip (verified)"
        if m.state == "failed":
            return m.error
        out = ["in Immich" if m.state == "existing" else "upload"]
        if m not in plan.cands:
            return "in Immich, left as is"
        if plan.stop:
            return "NO DATE: stops the folder" if not m.meta.date else "ok, but the folder stops"
        if m.rel in plan.named:
            out.append(f"date from {plan.named[m.rel]} {plan.dates[m.rel]:%Y-%m-%d %H:%M:%S}")
        elif m.rel in plan.dates:
            out.append(f"{'overwrite' if m.meta.date else 'set'} date {plan.dates[m.rel]:%Y-%m-%d %H:%M:%S}")
        elif m in plan.agree:
            out.append("keeps its date (matches --date)")
        elif not m.meta.date:
            out.append("no date")
        if m.rel in plan.places:
            out.append(f"{'overwrite' if m.meta.gps else 'set'} place")
        return ", ".join(out)

    def place(meta: Meta) -> str:
        if meta.where:
            return f"{meta.where[0]:.5f}, {meta.where[1]:.5f}"
        return "yes" if meta.gps else "-"

    rows = [["File", "Size", "Taken", "Date from", "Location", "Camera", "Plan"]]
    for m in s.media:
        taken = m.meta.when or ("-" if m.meta.readable else "(not checked)")
        rows.append([m.rel + (" +xmp" if m.sidecar else ""), human(m.size), taken, m.meta.src or "-",
                     place(m.meta), m.meta.camera or "-", what(m)])
    if len(rows) > 1:
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for n, r in enumerate(rows):
            line = "  ".join(c.rjust(w) if i == 1 and n else c.ljust(w) for i, (c, w) in enumerate(zip(r, widths)))
            print("  " + line.rstrip())
            if n == 0:
                print("  " + "  ".join("-" * w for w in widths))
    for p in s.other:
        print(f"  not a photo/video, not uploaded: {p.relative_to(folder)}")
    for p in s.junk:
        print(f"  system junk{', deleted by --delete' if args.delete else ''}: {p.relative_to(folder)}")

    # summary: what is there, then what a run would do about it
    taken = sorted(m.meta.when[:16] for m in s.media if m.meta.when)
    line = f"  {files(len(s.media))} ({human(sum(m.size for m in s.media))})"
    if taken:
        first, last = taken[0], taken[-1]
        line += f", taken {first}" + (f" to {last[11:] if last[:10] == first[:10] else last}" if last != first else "")
    if skip:
        print(line + "; skipped: verified in the log (--force to redo)")
        return True
    existing = sum(m.state == "existing" for m in s.media)
    failed = [m for m in s.media if m.state == "failed"]
    todo = sum(m.state == "pending" for m in s.media)
    line += (f"; {existing} already in Immich, {todo} to upload" if api else f"; {todo} to upload")
    print(line + (f", {len(failed)} rejected" if failed else ""))
    counted = "of them" if not args.force else "with --force, of all"

    if plan.stop:
        print(f"  {files(len(plan.undated))} without a date: the upload stops here "
              "(use --date, --date-from-name or --allow-undated)")
    elif plan.undated and args.allow_undated:
        print(f"  {files(len(plan.undated))} without a date, uploaded anyway (--allow-undated)")
    elif plan.cands:
        set_by_date = len([r for r in plan.dates if r not in plan.named])
        parts = []
        if plan.named:
            parts.append(f"{len(plan.named)} dated from names")
        if set_by_date:
            parts.append(f"{set_by_date} get --date")
        if plan.agree:
            parts.append(f"{len(plan.agree)} keep a date matching --date")
        print(f"  Dates, {counted}: " + ("; ".join(parts) if parts else "no file lacks a date"))
    for note in plan.notes:
        print(f"  --date-from-name: {note}")
    if plan.cands and not plan.stop:
        if args.place:
            print(f"  Place, {counted}: {files(len(plan.places))} get {args.place_name}")
        elif plan.nogps:
            print(f"  {files(len(plan.nogps))} without a location (--place would set one)")

    asks = []
    if not args.force and not plan.stop:
        if any(v.startswith("folder") for v in plan.named.values()):
            asks.append("some dates come from a folder name")
        if plan.have_date or plan.have_place:
            asks.append("some files have a date/place and others not")
    if asks:
        print(f"  A run would ask first: {'; '.join(asks)}")

    if args.delete:
        if plan.stop or failed:
            print("  --delete: nothing, as the upload would not go through")
        else:
            n = len(s.other)
            kept = f"the folder stays ({n} other {'file' if n == 1 else 'files'} in it)" if n else "then the folder"
            plural = lambda k, word: f"{k} {word}{'' if k == 1 else 's'}"
            extra = ", ".join(x for x in (plural(len(s.sidecars), "sidecar") if s.sidecars else "",
                                         plural(len(s.junk), "junk file") if s.junk else "") if x)
            print(f"  --delete: once all {len(s.media)} are verified, deletes them"
                  + (f", {extra}" if extra else "") + f"; {kept}")
    if not api:
        print("  (Which files Immich already has was not checked: no server.)")
    return not plan.stop and not failed and not asks


def preview_main(ap, args) -> int:
    """-n / -L: show what a run would do. Works without a server, then only from the local files."""
    print("Dry run: nothing is uploaded, changed, logged or deleted.")
    args.tag = list(dict.fromkeys(t.strip().strip("/") for t in args.tag if t.strip().strip("/")))
    folders = check_folders(ap, args)
    if args.place:
        lat, lon, args.place_name = parse_place(args.place)
        args.place = (lat, lon)
        print(f"Place: {args.place_name}")
    api, media_ext, sidecar_ext, rc = None, MEDIA_EXT, {".xmp"}, 0
    missing = "no Immich URL" if not args.server else "no API key" if not args.api_key else None
    if missing:
        print(f"Checking the local files only: {missing}.")
    else:
        try:
            api = Immich(args.server, args.api_key)
            media_ext, sidecar_ext = api.media_types()
            api.request("GET", "/albums", query={"name": "immich-upload key check"})  # checks the key works
        except ApiError as e:
            print(f"Immich refused the API key ({e}); checking the local files only.", file=sys.stderr)
            api, media_ext, sidecar_ext, rc = None, MEDIA_EXT, {".xmp"}, 2
        except OSError as e:
            print(f"Cannot reach Immich at {args.server} ({e}); checking the local files only.", file=sys.stderr)
            api, media_ext, sidecar_ext, rc = None, MEDIA_EXT, {".xmp"}, 2
    if api and args.tag:
        try:
            have = {t["value"] for t in api.tags()}
            new = [t for t in args.tag if t not in have]
            if new:
                print(f"Would create {'tag' if len(new) == 1 else 'tags'}: {', '.join(new)}")
        except ApiError as e:
            print(f"Cannot read the tags ({e}). The API key needs tag.read, tag.create and tag.asset.",
                  file=sys.stderr)
            rc = 2
    log = Log(args.log)
    ok = True
    for f in folders:
        ok = preview(f, args, log, api, media_ext, sidecar_ext) and ok
    return rc or (0 if ok else 1)


def check_folders(ap, args) -> list[Path]:
    if not args.folders:
        ap.error("give at least one FOLDER")
    args.jobs = max(1, args.jobs)
    args.log = args.log.expanduser().resolve()
    folders = []
    for f in args.folders:
        f = f.expanduser().resolve()
        if not f.is_dir():
            ap.error(f"not a folder: {f}")
        folders.append(f)
    return folders


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Nothing is deleted for an unfinished folder; run it again to continue.")
        sys.exit(130)
