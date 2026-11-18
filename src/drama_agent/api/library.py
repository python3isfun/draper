"""The asset library's read API (phase 1 of docs/entity-reorganization.md).

Characters and sets, each with what it owns (looks, states, items), its current pictures
and where it is used; a story's casting; and a manual sync. The library mirrors each story's
latest revision and is synced before every read, so it follows hand edits and new pictures.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from ..library import asset_list, asset_view, sync_library


def register(app: FastAPI, *, repo: Any) -> None:
    def _kind_or_404(asset_id: str, kind: str) -> Any:
        sync_library(repo)
        a = repo.get_library_asset(asset_id)
        if a is None or a.kind != kind:
            raise HTTPException(404, f"no {kind} {asset_id} in the library")
        return a

    @app.post("/v1/library/sync")
    async def library_sync(force: bool = False) -> dict[str, int]:
        """Mirror every story's latest revision into the library now."""
        return vars(sync_library(repo, force=force))

    @app.get("/v1/library/characters")
    async def library_characters() -> list[dict[str, Any]]:
        """Every character: name, sheet, its pictures, how many looks and items it has and
        how many stories cast it."""
        sync_library(repo)
        return asset_list(repo, "character")

    @app.get("/v1/library/characters/{asset_id}")
    async def library_character(asset_id: str) -> dict[str, Any]:
        """A character with its looks and items, each with its current pictures and the
        stories and shots that use it."""
        return asset_view(repo, _kind_or_404(asset_id, "character"))

    @app.get("/v1/library/sets")
    async def library_sets() -> list[dict[str, Any]]:
        """Every set: name, sheet, its pictures, how many states and items it has and how
        many stories cast it."""
        sync_library(repo)
        return asset_list(repo, "set")

    @app.get("/v1/library/sets/{asset_id}")
    async def library_set(asset_id: str) -> dict[str, Any]:
        """A set with its states and items (its dressing), each with its current pictures
        and the stories and shots that use it."""
        return asset_view(repo, _kind_or_404(asset_id, "set"))

    @app.get("/v1/drama-packages/{package_id}/casting")
    async def story_casting(package_id: str) -> list[dict[str, Any]]:
        """The story's characters, looks, locations, states and props, each with the library
        asset it is cast as."""
        sync_library(repo)
        rows = repo.list_casting(package_id=package_id)
        if not rows and repo.get_package(package_id) is None:
            raise HTTPException(404, "package not found")
        names = {a.asset_id: a.name for a in repo.list_library_assets()}
        return [{**c.model_dump(), "asset_name": names.get(c.asset_id, "")} for c in rows]
