"""An in-process stand-in for the parts of the Immich v3 API that immich-upload.py uses.

It keeps assets by SHA-1 like Immich does, so duplicates, trashed files, albums, tags and
date/place edits behave like on a real server, without one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

API_KEY = "test-key"


class FakeImmich:
    def __init__(self, media_ext: set[str]):
        self.media_ext = media_ext
        self.assets: dict[str, dict] = {}  # sha1 -> {id, name, trashed, sidecar, date, lat, lon}
        self.albums: dict[str, dict] = {}  # id -> {name, ids}
        self.tags: dict[str, dict] = {}  # value -> {id, name, value, parentId, assets}
        self.fail_names: set[str] = set()  # uploads of these file names answer 500
        self.refuse_restore = False  # restoring from the trash answers 403, as without asset.delete
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def start(self) -> "FakeImmich":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- helpers for tests
    def by_name(self, name: str) -> dict:
        return next(a for a in self.assets.values() if a["name"] == name)

    def add(self, data: bytes, name: str, **fields) -> dict:
        """Put a file in Immich as if it had been uploaded before."""
        a = {"id": str(uuid.uuid4()), "name": name, "trashed": False, "sidecar": False,
             "date": None, "lat": None, "lon": None, **fields}
        self.assets[hashlib.sha1(data).hexdigest()] = a
        return a

    def album(self, name: str) -> dict | None:
        return next((a for a in self.albums.values() if a["name"] == name), None)

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None):
                b = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def authorized(self):
                if self.headers.get("x-api-key") == API_KEY:
                    return True
                self.body()
                self.send(401, {"message": "Invalid API key"})
                return False

            def do_GET(self):
                if not self.authorized():
                    return
                path = urlsplit(self.path).path
                if path == "/api/server/media-types":
                    ext = sorted(fake.media_ext)
                    return self.send(200, {"image": [e for e in ext if e not in VIDEO], "video": [e for e in ext if e in VIDEO],
                                           "sidecar": [".xmp"]})
                if path == "/api/albums":
                    name = parse_qs(urlsplit(self.path).query).get("name", [None])[0]
                    return self.send(200, [{"id": i, "albumName": a["name"], "assetCount": len(a["ids"]), "shared": False,
                                            "startDate": None, "endDate": None}
                                           for i, a in fake.albums.items() if name is None or a["name"] == name])
                if path == "/api/tags":
                    return self.send(200, [{k: v for k, v in t.items() if k != "assets"} for t in fake.tags.values()])
                m = re.fullmatch(r"/api/assets/([0-9a-f-]{36})", path)
                if m:
                    a = next((a for a in fake.assets.values() if a["id"] == m.group(1)), None)
                    if not a:
                        return self.send(404, {"message": "Not found"})
                    return self.send(200, {"id": a["id"], "exifInfo": {"dateTimeOriginal": a["date"],
                                                                       "latitude": a["lat"], "longitude": a["lon"]}})
                self.send(404, {"message": "Not found"})

            def do_POST(self):
                if not self.authorized():
                    return
                b = self.body()
                if self.path == "/api/assets/bulk-upload-check":
                    res = []
                    for item in json.loads(b)["assets"]:
                        a = fake.assets.get(item["checksum"])
                        res.append({"id": item["id"], "action": "reject", "reason": "duplicate", "assetId": a["id"],
                                    "isTrashed": a["trashed"]} if a else {"id": item["id"], "action": "accept"})
                    return self.send(200, {"results": res})
                if self.path == "/api/trash/restore/assets":
                    if fake.refuse_restore:
                        return self.send(403, {"message": "Missing required permission: asset.delete"})
                    ids = set(json.loads(b)["ids"])
                    for a in fake.assets.values():
                        if a["id"] in ids:
                            a["trashed"] = False
                    return self.send(200, {"count": len(ids)})
                if self.path == "/api/albums":
                    i = str(uuid.uuid4())
                    fake.albums[i] = {"name": json.loads(b)["albumName"], "ids": set()}
                    return self.send(201, {"id": i, "albumName": fake.albums[i]["name"]})
                if self.path == "/api/assets":
                    boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
                    parts = {}
                    for part in b.split(b"--" + boundary)[1:-1]:
                        head, _, data = part.partition(b"\r\n\r\n")
                        parts[re.search(rb'name="([^"]+)"', head).group(1).decode()] = data[:-2]
                    for field in ("assetData", "fileCreatedAt", "fileModifiedAt", "filename"):
                        if field not in parts:
                            return self.send(400, {"message": f"{field} missing"})
                    h = hashlib.sha1(parts["assetData"]).hexdigest()
                    if h != self.headers.get("x-immich-checksum"):
                        return self.send(400, {"message": "checksum header does not match"})
                    name = parts["filename"].decode()
                    if name in fake.fail_names:
                        return self.send(500, {"message": "boom"})
                    if h in fake.assets:
                        return self.send(200, {"id": fake.assets[h]["id"], "status": "duplicate"})
                    a = fake.add(parts["assetData"], name, sidecar="sidecarData" in parts)
                    return self.send(201, {"id": a["id"], "status": "created"})
                self.send(404, {"message": "Not found"})

            def do_PUT(self):
                if not self.authorized():
                    return
                d = json.loads(self.body() or b"{}")
                if self.path == "/api/assets":  # bulk: place
                    for a in fake.assets.values():
                        if a["id"] in d["ids"]:
                            a["lat"], a["lon"] = d["latitude"], d["longitude"]
                    return self.send(204)
                m = re.fullmatch(r"/api/assets/([0-9a-f-]{36})", self.path)
                if m:
                    for a in fake.assets.values():
                        if a["id"] == m.group(1):
                            t = dt.datetime.fromisoformat(d["dateTimeOriginal"]).astimezone(dt.timezone.utc)
                            a["date"] = t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    return self.send(200, {"id": m.group(1)})
                if self.path == "/api/tags":
                    out = []
                    for value in d["tags"]:
                        parent, names = None, value.split("/")
                        for i in range(len(names)):
                            v = "/".join(names[: i + 1])
                            if v not in fake.tags:
                                fake.tags[v] = {"id": str(uuid.uuid4()), "name": names[i], "value": v,
                                                "parentId": parent, "assets": set()}
                            parent = fake.tags[v]["id"]
                        out.append({k: x for k, x in fake.tags[value].items() if k != "assets"})
                    return self.send(200, out)
                if self.path == "/api/tags/assets":
                    for t in fake.tags.values():
                        if t["id"] in d["tagIds"]:
                            t["assets"].update(d["assetIds"])
                    return self.send(200, {"count": len(d["assetIds"])})
                m = re.fullmatch(r"/api/albums/([^/]+)/assets", self.path)
                if m and m.group(1) in fake.albums:
                    album = fake.albums[m.group(1)]
                    out = [{"id": i, "success": i not in album["ids"], **({"error": "duplicate"} if i in album["ids"] else {})}
                           for i in d["ids"]]
                    album["ids"].update(d["ids"])
                    return self.send(200, out)
                self.send(404, {"message": "Not found"})

        return H


VIDEO = {".3gp", ".3gpp", ".avi", ".flv", ".insv", ".m2t", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpe",
         ".mpeg", ".mpg", ".mts", ".mxf", ".ts", ".vob", ".webm", ".wmv"}
