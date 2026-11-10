"""SQLite media inventory. Search never scans directories or calls the network."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from lib.pixabay_client import PixabayClient, process_lock, resource_root

EXTENSIONS = {"image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"},
              "video": {".mp4", ".mov", ".mkv", ".webm", ".avi"},
              "audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}}


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def probe_media(path):
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(path) as image:
            width, height = image.size
            fmt = image.format.lower()
            image.verify()
        return {"kind": "image", "width": width, "height": height, "format": fmt}
    except (UnidentifiedImageError, OSError):
        pass
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Unreadable media file")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not video and not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError("No playable media stream")
    if video and (not video.get("width") or not video.get("height")):
        raise ValueError("Invalid video dimensions")
    if not video and not any(s.get("codec_type") == "audio" and int(s.get("sample_rate") or 0) > 0 for s in streams):
        raise ValueError("Invalid audio stream")
    return {"kind": "video" if video else "audio", "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "duration": float(info.get("format", {}).get("duration") or 0),
            "format": info.get("format", {}).get("format_name")}


class MediaLibrary:
    def __init__(self, root=None, *, client=None):
        self.root = resource_root(root)
        self.directory = self.root / ".library"
        self.directory.mkdir(exist_ok=True)
        self.db_path = self.directory / "media.sqlite3"
        self.client = client
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, path TEXT UNIQUE, status TEXT NOT NULL,
                    search_text TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, status TEXT, path TEXT, error TEXT);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _save(self, row):
        text = " ".join(str(row.get(k) or "") for k in
                        ("path", "title", "tags", "source_tags", "description", "source_description")).casefold()
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?)",
                       (row["id"], row["path"], row.get("status", "available"), text, json.dumps(row, ensure_ascii=False)))
        return row

    def get(self, media_id):
        with self.db() as db:
            result = db.execute("SELECT payload FROM media WHERE id=?", (media_id,)).fetchone()
        if not result:
            raise KeyError("Unknown media ID")
        row = json.loads(result[0])
        if not Path(row["path"]).is_file():
            row["status"] = "missing"
            self._save(row)
        return row

    def sync(self):
        report = {"indexed": 0, "unchanged": 0, "missing": 0, "errors": []}
        with process_lock(self.directory / "inventory.lock"):
            with self.db() as db:
                previous = {r[0]: json.loads(r[1]) for r in db.execute("SELECT path,payload FROM media")}
            for path in self.root.rglob("*"):
                if self.directory in path.parents or not path.is_file() or path.is_symlink():
                    continue
                if path.suffix.lower() not in set().union(*EXTENSIONS.values()):
                    continue
                path = path.resolve()
                old = previous.get(str(path), {})
                try:
                    stat = path.stat()
                    if old.get("mtime_ns") == stat.st_mtime_ns and old.get("size") == stat.st_size and old.get("status") == "available":
                        report["unchanged"] += 1
                        continue
                    row = {"id": "local:" + hashlib.sha256(str(path).casefold().encode()).hexdigest()[:24],
                           "title": path.stem, "tags": [], "description": "", "source_description": "",
                           "license": "未知", "provider": "local", "acquired_at": time.time(), **old,
                           **probe_media(path), "path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                           "sha256": digest(path), "status": "available"}
                    self._save(row)
                    report["indexed"] += 1
                except Exception as exc:
                    if old:
                        self._save({**old, "status": "invalid"})
                    report["errors"].append({"path": str(path), "error": type(exc).__name__})
            for path, old in previous.items():
                if not Path(path).is_file():
                    self._save({**old, "status": "missing"})
                    report["missing"] += 1
        return report

    @staticmethod
    def matches(row, filters):
        if filters.get("kind") and row.get("kind") != filters["kind"]:
            return False
        for name, field, lower in (("min_width", "width", True), ("min_height", "height", True),
                                   ("min_duration", "duration", True), ("max_duration", "duration", False)):
            if filters.get(name) is not None:
                value = row.get(field)
                if value is None or (value < filters[name] if lower else value > filters[name]):
                    return False
        orientation = filters.get("orientation")
        if orientation and orientation != "all":
            w, h = row.get("width"), row.get("height")
            if not w or not h:
                return False
            actual = "horizontal" if w > h else "vertical" if h > w else "square"
            if actual != orientation:
                return False
        return True

    def search_local(self, query="", limit=20, **filters):
        terms = query.casefold().split()
        sql = "SELECT payload FROM media WHERE status='available'"
        args = []
        for term in terms:
            sql += " AND instr(search_text, ?) > 0"
            args.append(term)
        with self.db() as db:
            rows = [json.loads(r[0]) for r in db.execute(sql, args)]
        rows = [r for r in rows if self.matches(r, filters)]
        rows.sort(key=lambda r: (-sum(3 * (t in str(r.get("tags", "")).casefold()) +
                                      2 * (t in r.get("title", "").casefold()) for t in terms), r["id"]))
        return rows[:max(1, min(int(limit), 100))]

    def update_metadata(self, media_id, **fields):
        if set(fields) - {"title", "tags", "description", "license", "license_url", "usage_restrictions", "source_url", "creator"}:
            raise ValueError("Only descriptive metadata may be edited")
        if "tags" in fields and (not isinstance(fields["tags"], list) or not all(isinstance(t, str) for t in fields["tags"])):
            raise ValueError("tags must be a list of strings")
        return self._save({**self.get(media_id), **fields})

    def search_pixabay(self, query, kind="image", **filters):
        client = self.client or PixabayClient(self.root)
        data = client.search(kind, {"q": query, "page": 1, "per_page": 20})
        candidates = []
        for hit in data.get("hits", []):
            if kind == "image":
                require_dimensions = any(filters.get(k) for k in ("min_width", "min_height", "orientation"))
                version = "web" if hit.get("webformatURL") and (require_dimensions or not hit.get("largeImageURL")) else "large"
                url = hit.get("webformatURL") if version == "web" else hit.get("largeImageURL")
                # Original-image dimensions do not describe the downloadable rendition.
                width = hit.get("webformatWidth") if version == "web" else None
                height = hit.get("webformatHeight") if version == "web" else None
            else:
                renditions = hit.get("videos", {})
                version = next((v for v in ("large", "medium", "small", "tiny") if renditions.get(v, {}).get("url")), None)
                if not version:
                    continue
                rendition = renditions[version]
                url, width, height = rendition["url"], rendition.get("width"), rendition.get("height")
            if not url:
                continue
            source_id = str(hit["id"])
            if not source_id.isascii() or not source_id.isdigit():
                continue
            row = {"id": f"pixabay:{kind}:{source_id}:{version}", "provider": "pixabay", "source_id": source_id,
                   "version": version, "kind": kind, "download_url": url, "width": width, "height": height,
                   "original_width": hit.get("imageWidth"), "original_height": hit.get("imageHeight"),
                   "duration": hit.get("duration"), "title": "", "source_tags": hit.get("tags", ""),
                   "tags": [], "description": "", "source_description": "", "creator": hit.get("user", ""),
                   "source_url": hit.get("pageURL", ""), "license": "Pixabay Content License",
                   "license_url": "https://pixabay.com/service/license-summary/"}
            if self.matches(row, filters):
                candidates.append(row)
        with self.db() as db:
            db.executemany("INSERT OR REPLACE INTO candidates VALUES (?,?)", [(r["id"], json.dumps(r)) for r in candidates])
        return [{k: v for k, v in r.items() if k != "download_url"} for r in candidates]

    def download_selected(self, candidate_ids):
        if not candidate_ids:
            raise ValueError("Select candidate IDs before downloading")
        results = []
        with process_lock(self.directory / "inventory.lock"):
            for media_id in dict.fromkeys(candidate_ids):
                try:
                    existing = self.get(media_id)
                    if existing.get("status") == "available":
                        results.append(existing)
                        continue
                except KeyError:
                    pass
                with self.db() as db:
                    found = db.execute("SELECT payload FROM candidates WHERE id=?", (media_id,)).fetchone()
                if not found:
                    raise KeyError("Unknown candidate; search first")
                row = json.loads(found[0])
                suffix = ".jpg" if row["kind"] == "image" else ".mp4"
                path = self.root / "pixabay" / row["kind"] / f"{row['source_id']}_{row['version']}{suffix}"
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO downloads VALUES (?,?,?,NULL)", (media_id, "pending", str(path)))
                try:
                    if path.exists():
                        try:
                            probe_media(path)
                        except (ValueError, OSError):
                            path.replace(path.with_suffix(path.suffix + ".invalid"))
                    if not path.exists():
                        (self.client or PixabayClient(self.root)).download(row["download_url"], path)
                    technical = probe_media(path)
                    if technical["kind"] != row["kind"]:
                        raise ValueError("Downloaded media type mismatch")
                    stat = path.stat()
                    row.pop("download_url", None)
                    row.update(technical)
                    row.update(path=str(path), sha256=digest(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                               status="available", acquired_at=time.time())
                    with self.db() as db:
                        for (payload,) in db.execute("SELECT payload FROM media WHERE status='available'"):
                            other = json.loads(payload)
                            if other.get("sha256") == row["sha256"] and other["id"] != media_id:
                                row["duplicate_of"] = other["id"]
                                break
                    self._save(row)
                    with self.db() as db:
                        db.execute("UPDATE downloads SET status='complete',error=NULL WHERE id=?", (media_id,))
                    results.append(row)
                except Exception as exc:
                    with self.db() as db:
                        db.execute("UPDATE downloads SET status='failed',error=? WHERE id=?", (type(exc).__name__, media_id))
                    raise
        return results

    def export(self, media_id, project_dir, scene_id):
        row = self.get(media_id)
        if row["status"] != "available":
            raise ValueError("Selected media is unavailable; search again")
        project = Path(project_dir).resolve()
        relative = Path("assets") / row["kind"] / (hashlib.sha256(media_id.encode()).hexdigest()[:24] + Path(row["path"]).suffix)
        destination = project / relative
        if not destination.resolve().is_relative_to(project):
            raise ValueError("Project asset path escapes project")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(row["path"], destination)
        return {"id": media_id, "type": row["kind"], "path": relative.as_posix(), "source_tool": "media_library",
                "scene_id": scene_id, "provider": row["provider"], "license": row.get("license", "未知"),
                "original_url": row.get("source_url", "")}
