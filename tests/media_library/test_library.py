import io
import json
import multiprocessing
from pathlib import Path
import socket
import time
import wave

from PIL import Image
import pytest
import requests

from lib.media_library import MediaLibrary
from lib.pixabay_client import PixabayClient, PixabayError, process_lock
from tools.media_library import MediaLibrary as Tool


class Clock:
    def __init__(self):
        self.now = 1000
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, status=200, data=None, body=b"", headers=None):
        self.status_code, self.data, self.body = status, data, body
        self.headers = headers or {}

    def json(self):
        return self.data

    def iter_content(self, size):
        yield self.body

    def close(self):
        pass


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 16)).save(buffer, format="PNG")
    return buffer.getvalue()


def client(root, responses):
    clock = Clock()
    calls = []

    def transport(url, **kwargs):
        calls.append((clock.time(), url, kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    return PixabayClient(root, transport=transport, clock=clock.time, sleep=clock.sleep), calls, clock


def test_inventory_search_is_database_only(tmp_path, monkeypatch):
    path = tmp_path / "科技画面.png"
    path.write_bytes(png())
    library = MediaLibrary(tmp_path)
    assert library.search_local("科技") == []
    assert library.sync()["indexed"] == 1
    row = library.search_local("科技")[0]
    library.update_metadata(row["id"], tags=["人工智能"], description="芯片", usage_restrictions="仅限非商业用途")
    assert library.sync()["unchanged"] == 1
    monkeypatch.setattr(Path, "rglob", lambda *a: pytest.fail("Search scanned directories"))
    assert library.search_local("人工智能 芯片", kind="image", orientation="horizontal")[0]["usage_restrictions"] == "仅限非商业用途"
    assert not library.search_local("科技", min_width=100)
    path.unlink()
    assert library.get(row["id"])["status"] == "missing"
    assert not library.search_local("科技")


def test_audio_export_and_metadata_preservation(tmp_path):
    path = tmp_path / "背景.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 800)
    library = MediaLibrary(tmp_path)
    assert library.sync()["indexed"] == 1
    row = library.search_local("背景", kind="audio")[0]
    library.update_metadata(row["id"], license="custom", description="保留")
    path.touch()
    library.sync()
    assert library.get(row["id"])["description"] == "保留"
    project = tmp_path / "project"
    entry = library.export(row["id"], project, "scene-1")
    assert (project / entry["path"]).is_file()
    import jsonschema
    schema = json.loads(Path("schemas/artifacts/asset_manifest.schema.json").read_text())
    jsonschema.validate({"version": "1.0", "assets": [entry]}, schema)


def test_cache_spacing_and_secret_not_persisted(tmp_path):
    api, calls, clock = client(tmp_path, [Response(data={"hits": []}) for _ in range(3)])
    api.search("image", {"q": "city", "key": "never-persist"})
    api.search("image", {"q": "city"})
    api.search("video", {"q": "city"})
    assert len(calls) == 2
    assert calls[1][0] - calls[0][0] >= 5
    assert b"offline-test-secret" not in api.db_path.read_bytes()
    assert b"never-persist" not in api.db_path.read_bytes()
    clock.now += 86400
    api.search("image", {"q": "city"})
    assert len(calls) == 3


def test_cooldown_persists_across_clients(tmp_path):
    api, calls, clock = client(tmp_path, [Response(429, headers={"Retry-After": "120"})])
    with pytest.raises(PixabayError) as error:
        api.search("image", {"q": "city"})
    assert error.value.retry_at == 1120
    another = PixabayClient(tmp_path, transport=lambda *a, **k: pytest.fail("Cooldown bypass"), clock=clock.time, sleep=clock.sleep)
    with pytest.raises(PixabayError):
        another.search("video", {"q": "other"})
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_stops_without_retry(tmp_path, status):
    api, calls, _ = client(tmp_path, [Response(status)])
    with pytest.raises(PixabayError):
        api.search("image", {"q": "city"})
    assert len(calls) == 1


def test_retry_is_bounded_and_redacted(tmp_path):
    api, calls, clock = client(tmp_path, [requests.ConnectionError("secret URL"), Response(503), Response(502)])
    with pytest.raises(PixabayError) as error:
        api.search("image", {"q": "city"})
    assert "secret" not in str(error.value)
    assert len(calls) == 3
    assert 30 in clock.waits and 60 in clock.waits


def test_selected_download_recovery_and_duplicates(tmp_path, monkeypatch):
    hits = [{"id": n, "largeImageURL": f"https://cdn.pixabay.com/{n}.jpg", "tags": "城市", "imageWidth": 32, "imageHeight": 16} for n in (1, 2)]
    api, calls, _ = client(tmp_path, [Response(data={"hits": hits}), Response(body=png()), Response(body=png())])
    library = MediaLibrary(tmp_path, client=api)
    candidates = library.search_pixabay("城市")
    assert len(calls) == 1  # search never downloads
    assert "download_url" not in candidates[0]
    original = library._save
    monkeypatch.setattr(library, "_save", lambda row: (_ for _ in ()).throw(OSError("database fault")))
    with pytest.raises(OSError):
        library.download_selected([candidates[0]["id"]])
    monkeypatch.setattr(library, "_save", original)
    first = library.download_selected([candidates[0]["id"]])[0]
    assert len(calls) == 2  # recovery registers completed file without redownloading
    assert library.download_selected([first["id"]])[0]["id"] == first["id"]
    second = library.download_selected([candidates[1]["id"]])[0]
    assert second["duplicate_of"] == first["id"]
    assert len(library.search_local("城市")) == 2


def test_bad_download_not_available(tmp_path):
    api, _, _ = client(tmp_path, [Response(body=b"broken")])
    destination = tmp_path / "invalid.mp4"
    with pytest.raises(ValueError):
        api.download("https://cdn.pixabay.com/file.mp4", destination)
    assert not destination.exists()
    assert not destination.with_suffix(".mp4.part").exists()


def test_missing_root_and_audio_remote_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        MediaLibrary(tmp_path / "missing")
    api, calls, _ = client(tmp_path, [])
    with pytest.raises(ValueError, match="audio"):
        api.search("audio", {})
    assert calls == []


def test_tool_registration_and_offline_guard(tmp_path):
    from tools.tool_registry import ToolRegistry
    import tools.media_library as module
    registry = ToolRegistry()
    assert "media_library" in registry.register_module(module)
    result = Tool().execute({"operation": "search_local", "root": str(tmp_path), "query": "空"})
    assert result.success and result.data["result"] == []
    with pytest.raises(AssertionError, match="forbidden"):
        socket.create_connection(("example.invalid", 443))


def lock_worker(directory, events):
    # Spawned processes do not inherit pytest fixtures. They perform file locking
    # only, but block sockets here as well to keep the offline invariant explicit.
    def deny(*a, **k):
        raise AssertionError("Networking forbidden")
    socket.socket.connect = deny
    with process_lock(Path(directory) / "test.lock"):
        events.put(("start", time.monotonic()))
        time.sleep(0.1)
        events.put(("end", time.monotonic()))


def test_cross_process_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    processes = [context.Process(target=lock_worker, args=(str(tmp_path), events)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        if process.is_alive():
            process.terminate()
            pytest.fail("Lock deadlock")
        assert process.exitcode == 0
    records = sorted([events.get(timeout=3) for _ in range(4)], key=lambda r: r[1])
    assert [r[0] for r in records] == ["start", "end", "start", "end"]


def test_legacy_tools_use_shared_client(tmp_path, monkeypatch):
    import lib.pixabay_client as module
    from tools.graphics.pixabay_image import PixabayImage
    from tools.video.pixabay_video import PixabayVideo
    from tools.video.stock_sources.pixabay_video import PixabayVideoSource
    from tools.video.stock_sources.base import SearchFilters
    calls = []

    class FakeClient:
        def search(self, kind, params):
            calls.append(("search", kind))
            return {"hits": [{"id": 1, "largeImageURL": "https://cdn.pixabay.com/i.jpg",
                              "videos": {"large": {"url": "https://cdn.pixabay.com/v.mp4", "width": 32, "height": 16}}}]}

        def download(self, url, path):
            calls.append(("download", url))
            Path(path).write_bytes(png())
            return Path(path)

    monkeypatch.setattr(module, "PixabayClient", FakeClient)
    assert PixabayImage().execute({"query": "test", "output_path": str(tmp_path / "i.jpg")}).success
    assert PixabayVideo().execute({"query": "test", "output_path": str(tmp_path / "v.mp4")}).success
    adapter = PixabayVideoSource()
    candidate = adapter.search("test", SearchFilters())[0]
    adapter.download(candidate, tmp_path / "a.mp4")
    assert len(calls) == 6


def test_interrupted_download_and_changed_invalid_file(tmp_path):
    class Interrupted(Response):
        def iter_content(self, size):
            yield b"partial"
            raise requests.ConnectionError("interrupted")
    api, calls, _ = client(tmp_path, [Interrupted(), Interrupted(), Interrupted()])
    with pytest.raises(PixabayError):
        api.download("https://cdn.pixabay.com/file.jpg", tmp_path / "file.jpg")
    assert len(calls) == 3
    assert not list(tmp_path.glob("*.part"))
    path = tmp_path / "existing.png"
    path.write_bytes(png())
    library = MediaLibrary(tmp_path)
    library.sync()
    path.write_bytes(b"invalid")
    assert library.sync()["errors"]
    assert library.search_local("existing") == []


def test_local_gap_to_remote_search_without_download(tmp_path, monkeypatch):
    # Agent relevance is an instruction-layer decision; execute its prescribed
    # zero-result branch with mocked transport, never invent a Python orchestrator.
    import lib.media_library as module
    api, calls, _ = client(tmp_path, [Response(data={"hits": [{"id": 5, "largeImageURL": "https://cdn.pixabay.com/5.jpg"}]})])
    monkeypatch.setattr(module, "PixabayClient", lambda root: api)
    tool = Tool()
    local = tool.execute({"operation": "search_local", "query": "city"})
    if not local.data["result"]:
        remote = tool.execute({"operation": "search_pixabay", "query": "city"})
    assert remote.success and remote.data["result"][0]["source_id"] == "5"
    assert len(calls) == 1
    assert not (tmp_path / "pixabay").exists()


def test_video_sync_and_remote_filters(tmp_path):
    import subprocess
    output = tmp_path / "sample.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=32x16:d=0.1",
                    "-c:v", "mpeg4", str(output)], check=True, capture_output=True)
    library = MediaLibrary(tmp_path)
    assert library.sync()["indexed"] == 1
    assert library.search_local("sample", kind="video", min_width=32, max_duration=1)
    api, _, _ = client(tmp_path, [Response(data={"hits": [{"id": 6, "duration": 3,
        "videos": {"large": {"url": "https://cdn.pixabay.com/v.mp4", "width": 1920, "height": 1080}}}]})])
    library.client = api
    assert library.search_pixabay("city", "video", min_width=1920)
    assert not library.search_pixabay("city", "video", min_duration=10)
