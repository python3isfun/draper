"""Global image numbers (@1234, image_ids.py): look them up and show their pictures."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..image_ids import MAX_REFS, Found, parse, resolve


class ImageIdsRequest(BaseModel):
    refs: list[str] = Field(max_length=MAX_REFS)


class ImageInfo(BaseModel):
    gid: int
    ref: str
    what: str
    likeness: bool | None
    url: str
    thumb_url: str


def register(app: FastAPI, ctx: Any) -> None:
    repo = ctx.repo
    _file = ctx._file

    @app.post("/v1/image-ids", response_model=dict[str, int])
    async def image_ids(req: ImageIdsRequest) -> dict[str, int]:
        """The global number of each picture (refs as in image_ids.py), handing out new
        numbers to pictures shown for the first time."""
        if bad := [r for r in req.refs if parse(r) is None]:
            raise HTTPException(422, f"not an image ref: {bad[0]}")
        return repo.image_ids(list(dict.fromkeys(req.refs)))

    def _found(gid: int) -> Found:
        ref = repo.image_ref(gid)
        found = resolve(repo, ref) if ref else None
        if found is None:
            raise HTTPException(404, f"no image @{gid}")
        return found

    @app.get("/v1/images/{gid}", response_model=ImageInfo)
    async def image_info(gid: int) -> ImageInfo:
        found = _found(gid)
        return ImageInfo(
            gid=gid,
            ref=found.ref,
            what=found.what,
            likeness=found.likeness,
            url=f"/v1/images/{gid}/file",
            thumb_url=f"/v1/images/{gid}/thumb",
        )

    @app.get("/v1/images/{gid}/file")
    async def image_file(gid: int) -> FileResponse:
        found = _found(gid)
        return _file(found.file, found.content_type)

    @app.get("/v1/images/{gid}/thumb")
    async def image_thumb(gid: int) -> FileResponse:
        found = _found(gid)
        if found.thumb:
            return _file(found.thumb, "image/jpeg")
        return _file(found.file, found.content_type)
