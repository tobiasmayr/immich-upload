#!/usr/bin/env python3
"""Upload photo folders to Immich, check every file arrived, and log each folder.

For each folder given:
  1. Find all photos and videos in it, including subfolders (the file types
     come from the Immich server itself). An .xmp sidecar next to a file is
     sent along with it.
  2. Read each file's capture date. If any file has none, stop before
     uploading (Immich would date it by the file time), unless --date gives
     one or --allow-undated is set. --place gives files without GPS a location.
  3. Upload the files Immich does not have yet. Files that are already there
     (same SHA-1 checksum) are not sent again.
  4. Check that every file is now in Immich under its exact checksum and not
     in the trash.
  5. Add all of them to an album (--album, else the one the log already has
     for the folder, else the folder's name), and tag
     them with any --tag given (missing tags are created). Set --date and
     --place on the files that lack them, and read both back.
  6. Record the folder in a Markdown log, one row per folder, updated on every
     run. A folder logged as verified is skipped next time, unless its number
     of files changed (use --force to redo it anyway).
  7. With --delete, and only when every file checked out, delete the
     uploaded files, their sidecars and system junk (Thumbs.db, desktop.ini,
     ...), then the folder if nothing else is left in it. Other files are
     kept and listed.

Needs only the Python standard library (3.9+), so it runs on Linux and Windows.

Settings come from the command line or these environment variables:
  IMMICH_URL         e.g. https://photos.example.org
  IMMICH_API_KEY     permissions: asset.upload, asset.read, album.read,
                     album.create, albumAsset.create; for --tag also
                     tag.read, tag.create, tag.asset; for --date or
                     --place also asset.update
  IMMICH_UPLOAD_LOG  path of the Markdown log (optional)
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


QT_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
XMP_DATE = re.compile(rb'(?:exif:DateTimeOriginal|xmp:CreateDate)\s*(?:=\s*["\']|>)\s*(\d{4})-(\d{2})-(\d{2})')
XMP_LAT = re.compile(rb'exif:GPSLatitude\s*(?:=\s*["\']|>)\s*\d')
XMP_LON = re.compile(rb'exif:GPSLongitude\s*(?:=\s*["\']|>)\s*\d')


def valid_ymd(y, m, d) -> str | None:
    try:
        y, m, d = int(y), int(m), int(d)
        return dt.date(y, m, d).isoformat() if y >= 1800 else None
    except ValueError:
        return None


def exif_date(raw: bytes) -> str | None:
    m = re.match(rb"\s*(\d{4})[:\-](\d{2})[:\-](\d{2})", raw)
    return valid_ymd(*m.groups()) if m else None


def merge(a: Meta, b: Meta) -> Meta:
    return Meta(a.date or b.date, a.gps or b.gps, a.readable or b.readable)


def xmp_meta(data: bytes) -> Meta:
    m = XMP_DATE.search(data)
    return Meta(valid_ymd(*m.groups()) if m else None, bool(XMP_LAT.search(data) and XMP_LON.search(data)))


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
    date = None
    for tags, tag in ((exif, 0x9003), (exif, 0x9004), (ifd0, 0x9003), (gps, 0x001D)):
        if tag in tags and (date := exif_date(value(tags[tag]))):
            break
    has_gps = 0x0002 in gps and 0x0004 in gps and (degrees(gps[0x0002]) != 0 or degrees(gps[0x0004]) != 0)
    return Meta(date, has_gps)


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
                meta.date = (QT_EPOCH + dt.timedelta(seconds=secs)).date().isoformat()
            break
    f.seek(start)
    moov = f.read(end - start)
    if not meta.date and b"com.apple.quicktime.creationdate" in moov:
        i = moov.find(b"com.apple.quicktime.creationdate")
        m = re.search(rb"(\d{4})-(\d{2})-(\d{2})T", moov[i:])
        meta.date = valid_ymd(*m.groups()) if m else None
    for key in (b"\xa9xyz", b"com.apple.quicktime.location.ISO6709"):
        i = moov.find(key)
        if i >= 0:
            m = re.search(rb"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", moov[i : i + 200])
            if m and (float(m.group(1)) != 0 or float(m.group(2)) != 0):
                meta.gps = True
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
            side = xmp_meta(sidecar.read_bytes()[: 4 * 1024 * 1024])
            meta = Meta(meta.date or side.date, meta.gps or side.gps, meta.readable or bool(side.date))
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

    # 0. capture dates (and GPS when --place is given): stop before uploading files without a date
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        for m, md in zip(s.media, ex.map(lambda m: read_meta(m.path, m.sidecar), s.media)):
            m.meta = md
    undated = [m for m in s.media if not m.meta.date]
    dates = sorted(m.meta.date for m in s.media if m.meta.date)
    if dates:
        print(f"  Taken {dates[0]}" + (f" to {dates[-1]}" if dates[-1] != dates[0] else "")
              + (f"; {len(undated)} without a date" if undated else ""))
    else:
        print("  No file has a date")
    for m in undated[:20]:
        print(f"    no date: {m.rel}" + ("" if m.meta.readable else "  (file type not checked)"))
    if len(undated) > 20:
        print(f"    ... and {len(undated) - 20} more")
    if undated and not args.date and not args.allow_undated:
        print(f"  Stopped before uploading: Immich would date {'this file' if len(undated) == 1 else 'these files'} "
              "by the file time. Set one with --date, or upload anyway with --allow-undated.")
        if not args.dry_run:
            log.record(folder, album=album, tags=[], files=len(s.media), new=0,
                       status=f"stopped: {len(undated)} without date", deleted="no",
                       now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
            print(f"  Logged to {args.log}")
        return False
    fix_date = undated if args.date else []
    fix_place = [m for m in s.media if not m.meta.gps] if args.place else []
    if args.date and not undated:
        print("  Every file has a date; --date is not needed here.")
    if fix_date:
        print(f"  {'Will set' if not args.dry_run else 'Would set'} the date {args.date.isoformat()} on "
              f"{files(len(fix_date))}" + (", one second apart in file name order" if len(fix_date) > 1 else ""))
    if args.place:
        print(f"  {files(len(fix_place))} without a location"
              + (f": {'will' if not args.dry_run else 'would'} set {args.place_name}" if fix_place else ""))

    # 1. checksums
    prog = Progress(len(s.media), total, "checksums")
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        for m, h in zip(s.media, ex.map(lambda m: sha1(m.path), s.media)):
            m.sha1 = h
            prog.step(m.size)

    # 2. what Immich already has
    by_id = {str(i): m for i, m in enumerate(s.media)}
    for cid, res in api.check([(cid, m.sha1) for cid, m in by_id.items()]).items():
        m = by_id[cid]
        if res["action"] == "reject" and res.get("reason") == "duplicate":
            m.state, m.asset_id = "existing", res.get("assetId")
        elif res["action"] == "reject":
            m.state, m.error = "failed", f"rejected by Immich: {res.get('reason')}"
    todo = [m for m in s.media if m.state == "pending"]
    existing = sum(m.state == "existing" for m in s.media)
    print(f"  {existing} already in Immich, {len(todo)} to upload")

    if args.dry_run:
        for m in todo[:20]:
            print(f"    would upload: {m.rel}")
        if len(todo) > 20:
            print(f"    ... and {len(todo) - 20} more")
        print("  Dry run: nothing uploaded, logged or deleted.")
        return True

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

    # 7. date and place, on files that lack them (Immich keeps such edits even when it re-reads the file)
    meta_ok = True
    if fix_date or fix_place:
        meta_ok = set_date_place(api, args, [m for m in fix_date if m.state != "failed"],
                                 [m for m in fix_place if m.state != "failed"], problems)

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


def set_date_place(api: Immich, args, fix_date: list[MediaFile], fix_place: list[MediaFile], problems) -> bool:
    """Set --date (one second apart, in file name order) and --place, then read them back from Immich."""
    want: dict[str, dict] = {}
    for m in fix_date:
        if m.asset_id not in want:  # the same photo twice in a folder gets one date
            want[m.asset_id] = {"date": args.date + dt.timedelta(seconds=len(want)), "rel": m.rel}
    errors = []

    def put_date(item):
        aid, w = item
        try:
            api.request("PUT", f"/assets/{aid}", {"dateTimeOriginal": w["date"].isoformat()})
        except ApiError as e:
            errors.append(f"{w['rel']}: date not set ({e})")

    with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(put_date, want.items()))
    place_ids = list(dict.fromkeys(m.asset_id for m in fix_place))
    lat, lon = args.place
    for i in range(0, len(place_ids), BATCH):
        try:
            api.request("PUT", "/assets", {"ids": place_ids[i : i + BATCH], "latitude": lat, "longitude": lon})
        except ApiError as e:
            errors.append(f"place not set on {len(place_ids[i:i + BATCH])} files ({e})")
    rel = {m.asset_id: m.rel for m in fix_date + fix_place}

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
        if aid in place_ids and not (abs((x.get("latitude") or 999) - lat) < 1e-4
                                     and abs((x.get("longitude") or 999) - lon) < 1e-4):
            return f"{rel[aid]}: location reads back as {x.get('latitude')}, {x.get('longitude')}"
        return None

    if not errors:
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as ex:
            errors += [e for e in ex.map(verify, list(dict.fromkeys(list(want) + place_ids))) if e]
    problems += errors
    if not errors:
        done = ([f"date on {files(len(want))}"] if want else []) + ([f"location on {files(len(place_ids))}"] if place_ids else [])
        print(f"  Set and checked: {', '.join(done)}")
    return not errors


def parse_date(text: str) -> dt.datetime:
    """YYYY, YYYY-MM, YYYY-MM-DD, optionally with a time (HH:MM[:SS]) and a UTC offset."""
    t = text.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}", t):
        t += "-01-01"
    elif re.fullmatch(r"\d{4}-\d{2}", t):
        t += "-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        t += " 12:00"  # noon, so no time zone can move it to another day
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date: {text!r} (use e.g. 2019-07-14 or \"2019-07-14 15:30\")")
    return d if d.tzinfo else d.astimezone()  # no offset given: this computer's time zone


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
        start, end = (a.get(k, "")[:10] for k in ("startDate", "endDate"))
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
               "immich-upload.py --list-albums --list-tags")
    ap.add_argument("folders", nargs="*", type=Path, metavar="FOLDER", help="folder(s) to upload, with subfolders")
    ap.add_argument("-l", "--list-albums", action="store_true",
                    help="list the albums in Immich and exit (uploads nothing)")
    ap.add_argument("--list-tags", action="store_true", help="list the tags in Immich and exit (uploads nothing)")
    ap.add_argument("-a", "--album",
                    help="album name (default: the album the log has for the folder, else the folder's name)")
    ap.add_argument("-t", "--tag", action="append", default=[], metavar="TAG",
                    help="tag every file with TAG; repeat for more tags; nest with '/', e.g. Travel/Iceland. "
                         "Missing tags are created")
    ap.add_argument("--date", type=parse_date, metavar="DATE",
                    help="date for files that have none, e.g. 2019-07-14, \"2019-07-14 15:30\", 2019-07 or 1987 "
                         "(without a time: 12:00; without an offset: this computer's time zone)")
    ap.add_argument("--place", metavar="PLACE",
                    help="location for files that have none: \"64.1466, -21.9426\" or a place name, "
                         "looked up on OpenStreetMap, e.g. \"Reykjavik, Iceland\"")
    ap.add_argument("--allow-undated", action="store_true",
                    help="upload files without a date anyway (Immich then uses the file time)")
    ap.add_argument("--delete", action="store_true",
                    help="delete the uploaded files and the folder once everything is verified")
    ap.add_argument("--log", type=Path, default=default_log(),
                    help=f"Markdown log of uploaded folders (default: {default_log()})")
    ap.add_argument("-n", "--dry-run", action="store_true", help="only show what would be uploaded")
    ap.add_argument("-f", "--force", action="store_true", help="redo folders the log lists as verified")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="parallel uploads (default: 4)")
    ap.add_argument("--server", default=os.environ.get("IMMICH_URL"), help="Immich URL (or IMMICH_URL)")
    ap.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="API key (or IMMICH_API_KEY)")
    args = ap.parse_args()

    if not args.server or not args.api_key:
        ap.error("set the Immich URL and API key (IMMICH_URL / IMMICH_API_KEY or --server / --api-key)")
    if args.list_albums or args.list_tags:
        api = Immich(args.server, args.api_key)
        rc = list_albums(api) if args.list_albums else 0
        if args.list_albums and args.list_tags:
            print()
        return max(rc, list_tags(api) if args.list_tags else 0)
    if args.place:
        lat, lon, args.place_name = parse_place(args.place)
        args.place = (lat, lon)
        print(f"Place: {args.place_name}")
    args.tag = list(dict.fromkeys(t.strip().strip("/") for t in args.tag if t.strip().strip("/")))
    if not args.folders:
        ap.error("give at least one FOLDER (or --list-albums)")
    args.jobs = max(1, args.jobs)
    args.log = args.log.expanduser().resolve()
    folders = []
    for f in args.folders:
        f = f.expanduser().resolve()
        if not f.is_dir():
            ap.error(f"not a folder: {f}")
        folders.append(f)
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
            if args.dry_run:
                if missing:
                    print(f"Would create tag(s): {', '.join(missing)}")
            else:
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Nothing is deleted for an unfinished folder; run it again to continue.")
        sys.exit(130)
