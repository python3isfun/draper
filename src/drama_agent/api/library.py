"""The asset library's read API (phase 1 of docs/entity-reorganization.md).

Characters and sets, each with what it owns (looks, states, items), its current pictures
and where it is used; a story's casting; and a manual sync. The library mirrors each story's
latest revision and is synced before every read, so it follows hand edits and new pictures.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..library import asset_list, asset_view, sync_library
from ..library.matching import matches
from ..library.models import Casting, asset_id
from ..library.reuse import ReuseError, reuse_anchor


class LinkRequest(BaseModel):
    story_id: str  # the story's character or location id
    asset_id: str  # another story's character or set


def register(app: FastAPI, *, repo: Any, media: Any = None) -> None:
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

    @app.get("/v1/drama-packages/{package_id}/casting/matches")
    async def casting_matches(package_id: str) -> list[dict[str, Any]]:
        """Each of the story's characters and sets, its link if a person made one, and the
        other stories' characters or sets that may be the same (by name), best first."""
        sync_library(repo)
        if repo.get_package(package_id) is None:
            raise HTTPException(404, "package not found")
        return matches(repo, package_id)

    @app.post("/v1/drama-packages/{package_id}/casting")
    async def link_casting(package_id: str, req: LinkRequest) -> dict[str, Any]:
        """Cast the story's character or set as another story's: its own copy is hidden and
        its looks or states join that asset. Kept by every sync until unlinked."""
        sync_library(repo)
        row = next((c for c in repo.list_casting(package_id=package_id)
                    if c.story_id == req.story_id), None)  # fmt: skip
        if row is None or row.kind not in ("character", "set"):
            raise HTTPException(404, f"{req.story_id} is not a character or location of this story")
        target = repo.get_library_asset(req.asset_id)
        if target is None or target.kind != row.kind:
            raise HTTPException(404, f"no {row.kind} {req.asset_id} in the library")
        if target.origin_package_id == package_id:
            raise HTTPException(409, "link it to another story's asset, not one of this story's")
        if target.merged_into is not None:
            raise HTTPException(409, f"{target.name} is itself linked to another asset")
        if target.archived:
            raise HTTPException(409, f"{target.name} belongs to an archived story")
        repo.save_casting(Casting(package_id=package_id, story_id=req.story_id,
                                  asset_id=target.asset_id, kind=row.kind, source="user"))  # fmt: skip
        sync_library(repo)
        return {"story_id": req.story_id, "asset_id": target.asset_id, "name": target.name}

    @app.post("/v1/drama-packages/{package_id}/casting/{story_id}/reuse")
    async def reuse_shared(package_id: str, story_id: str) -> dict[str, Any]:
        """Use the linked asset's current face (a character) or main set view (a set) as
        this story's: copied in as its pick. What the story drew from its old one goes out
        of date; redraw it with "Redraw out-of-date"."""
        sync_library(repo)
        try:
            done = reuse_anchor(repo, media, package_id, story_id)
        except ReuseError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"image_id": done.image.image_id, "from_image_id": done.source_image_id,
                "outdated": done.outdated}  # fmt: skip

    @app.delete("/v1/drama-packages/{package_id}/casting/{story_id}")
    async def unlink_casting(package_id: str, story_id: str) -> dict[str, Any]:
        """Undo a link: the story's character or set is its own again."""
        row = next((c for c in repo.list_casting(package_id=package_id)
                    if c.story_id == story_id), None)  # fmt: skip
        if row is None or row.source != "user":
            raise HTTPException(404, f"{story_id} is not linked")
        own = asset_id(row.kind, package_id, story_id)
        repo.save_casting(Casting(package_id=package_id, story_id=story_id, asset_id=own,
                                  kind=row.kind, source="story"))  # fmt: skip
        sync_library(repo)
        return {"story_id": story_id, "asset_id": own}
