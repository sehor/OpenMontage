"""Agent-facing local-first inventory; relevance decisions belong to the agent."""
from lib.media_library import MediaLibrary as Inventory
from lib.pixabay_client import PixabayError
from tools.base_tool import BaseTool, ToolResult, ToolTier


class MediaLibrary(BaseTool):
    name = "media_library"
    version = "1.0.0"
    tier = ToolTier.SOURCE
    capability = "media_search"
    capabilities = ["local_media_search", "stock_media_search", "media_inventory"]
    description = "Search the local database first; search Pixabay for gaps; download only selected IDs."
    usage_location = "skills/creative/media-library-usage.md"
    input_schema = {"type": "object", "required": ["operation"], "properties": {
        "operation": {"enum": ["sync", "search_local", "search_pixabay", "download_selected", "update_metadata", "get", "export"]},
        "root": {"type": "string"}, "query": {"type": "string"}, "kind": {"enum": ["image", "video", "audio"]},
        "candidate_ids": {"type": "array", "items": {"type": "string"}}, "media_id": {"type": "string"},
        "project_dir": {"type": "string"}, "scene_id": {"type": "string"}, "fields": {"type": "object"},
        "filters": {"type": "object"}, "limit": {"type": "integer"}}}

    def execute(self, inputs):
        try:
            library = Inventory(inputs.get("root"))
            operation = inputs["operation"]
            if operation == "sync":
                result = library.sync()
            elif operation == "search_local":
                result = library.search_local(inputs.get("query", ""), inputs.get("limit", 20),
                                              **{**inputs.get("filters", {}), **({"kind": inputs["kind"]} if "kind" in inputs else {})})
            elif operation == "search_pixabay":
                result = library.search_pixabay(inputs["query"], inputs.get("kind", "image"), **inputs.get("filters", {}))
            elif operation == "download_selected":
                result = library.download_selected(inputs["candidate_ids"])
            elif operation == "update_metadata":
                result = library.update_metadata(inputs["media_id"], **inputs["fields"])
            elif operation == "get":
                result = library.get(inputs["media_id"])
            elif operation == "export":
                result = library.export(inputs["media_id"], inputs["project_dir"], inputs["scene_id"])
            else:
                raise ValueError("Unknown media library operation")
            return ToolResult(success=True, data={"result": result})
        except PixabayError as exc:
            return ToolResult(success=False, error=str(exc), data={"retry_at": exc.retry_at})
        except Exception as exc:
            return ToolResult(success=False, error=f"Media library operation failed: {type(exc).__name__}")
