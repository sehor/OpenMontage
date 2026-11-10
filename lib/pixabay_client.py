"""Single, persistent, cross-process gate for Pixabay API and CDN traffic."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

DEFAULT_ROOT = Path(r"F:\videomake\resources")


def resource_root(root=None):
    path = Path(root or os.environ.get("MEDIA_LIBRARY_ROOT") or DEFAULT_ROOT).resolve()
    if not path.is_dir():
        raise FileNotFoundError("Media resource directory is unavailable")
    return path


@contextmanager
def process_lock(path, sleep=time.sleep):
    """OS lock survives neither process death nor exceptions; no worker threads."""
    with open(path, "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class PixabayError(RuntimeError):
    def __init__(self, message, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


class PixabayClient:
    def __init__(self, root=None, *, transport=None, clock=time.time, sleep=time.sleep):
        self.directory = resource_root(root) / ".library"
        self.directory.mkdir(exist_ok=True)
        self.clock, self.sleep = clock, sleep
        self.transport = transport or requests.get
        self.db_path = self.directory / "media.sqlite3"
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS request_state (id INTEGER PRIMARY KEY, next_at REAL NOT NULL, cooldown REAL NOT NULL);
                INSERT OR IGNORE INTO request_state VALUES (1, 0, 0);
                CREATE TABLE IF NOT EXISTS query_cache (key TEXT PRIMARY KEY, created REAL, payload TEXT);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _request(self, url, consume, **kwargs):
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host == "pixabay.com" or host.endswith(".pixabay.com")):
            raise PixabayError("Unsupported Pixabay URL")
        for attempt in range(3):
            with self.db() as db:
                next_at, cooldown = db.execute("SELECT next_at,cooldown FROM request_state WHERE id=1").fetchone()
            if cooldown > self.clock():
                raise PixabayError("Pixabay cooling down", cooldown)
            self.sleep(max(0, next_at - self.clock()))
            with self.db() as db:
                db.execute("UPDATE request_state SET next_at=? WHERE id=1", (self.clock() + 5,))
            response = None
            try:
                response = self.transport(url, timeout=(15, 120), allow_redirects=False, **kwargs)
                status = response.status_code
                if status == 429:
                    waits = [60.0]
                    for key in ("Retry-After", "X-RateLimit-Reset"):
                        try:
                            waits.append(float(response.headers.get(key, 0)))
                        except (TypeError, ValueError):
                            if key == "Retry-After":
                                try:
                                    waits.append(parsedate_to_datetime(response.headers[key]).timestamp() - self.clock())
                                except (KeyError, TypeError, ValueError, OverflowError):
                                    pass
                    retry_at = self.clock() + max(waits)
                    with self.db() as db:
                        db.execute("UPDATE request_state SET cooldown=? WHERE id=1", (retry_at,))
                    raise PixabayError("Pixabay rate limited", retry_at)
                if status >= 500:
                    raise requests.ConnectionError("Provider unavailable")
                if status != 200:
                    raise PixabayError(f"Pixabay HTTP {status}; request stopped")
                return consume(response)
            except requests.RequestException:
                if attempt == 2:
                    raise PixabayError("Pixabay network request failed") from None
            finally:
                if response is not None:
                    response.close()
            self.sleep((30, 60)[attempt])

    def search(self, kind, params):
        if kind not in ("image", "video"):
            raise ValueError("Pixabay supports image and video only; audio is local-only")
        params = {k: v for k, v in params.items() if k != "key"}
        params.setdefault("per_page", 20)
        params.setdefault("page", 1)
        params["per_page"] = max(3, min(20, int(params["per_page"])))
        params.setdefault("safesearch", "true")
        cache_key = json.dumps([kind, params], sort_keys=True, ensure_ascii=False)
        with process_lock(self.directory / "pixabay.lock", self.sleep):
            with self.db() as db:
                row = db.execute("SELECT created,payload FROM query_cache WHERE key=?", (cache_key,)).fetchone()
            if row and self.clock() - row[0] < 86400:
                return json.loads(row[1])
            from lib.env_loader import load_env
            load_env()
            key = os.environ.get("PIXABAY_API_KEY")
            if not key:
                raise PixabayError("PIXABAY_API_KEY is not configured")
            url = "https://pixabay.com/api/" + ("videos/" if kind == "video" else "")
            data = self._request(url, lambda r: r.json(), params={**params, "key": key})
            with self.db() as db:
                db.execute("INSERT OR REPLACE INTO query_cache VALUES (?,?,?)", (cache_key, self.clock(), json.dumps(data)))
            return data

    def download(self, url, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".part")

        def consume(response):
            size = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(65536):
                    if chunk:
                        output.write(chunk)
                        size += len(chunk)
            expected = response.headers.get("Content-Length")
            if not size or (expected and size != int(expected)):
                raise PixabayError("Incomplete media download")
            return temporary

        with process_lock(self.directory / "pixabay.lock", self.sleep):
            try:
                self._request(url, consume, stream=True)
                from lib.media_library import probe_media
                probe_media(temporary)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return path
