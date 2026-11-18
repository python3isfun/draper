"""Package routes: a package and its sheets, the export zip, JSON schemas, revisions."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask

from ..export import build_export
from ..models import (
    CastSheet,
    ProductionPackage,
    PropSheet,
    Revision,
    RevisionRequest,
    SetSheet,
    SourceAsset,
)
from ..models.design_sheets import SHEET_SCHEMAS
from ..pipeline.impact import analyze_impact
from ..pipeline.orchestrator import new_id
from ..render import render_markdown
from .schemas import (
    _new_export_file,
)


def register(app: FastAPI, ctx: Any) -> None:
    """Add these routes; `ctx` holds what create_app shares (repo, media, runners, helpers)."""
    _package_job = ctx._package_job
    _package_or_404 = ctx._package_or_404
    media = ctx.media
    repo = ctx.repo

    @app.get("/v1/drama-packages/{package_id}", response_model=None)
    async def get_package(
        package_id: str,
        revision_id: str | None = None,
        format: Literal["json", "markdown"] = Query("json"),
    ) -> ProductionPackage | PlainTextResponse:
        pkg = repo.get_package(package_id, revision_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        if format == "markdown":
            # text/plain, not text/markdown: browsers display it (and its text can be
            # copied) instead of downloading it.
            return PlainTextResponse(render_markdown(pkg), media_type="text/plain")
        return pkg

    @app.get("/v1/drama-packages/{package_id}/export", response_class=FileResponse)
    async def export_package(
        package_id: str, revision_id: str | None = None, job_id: str | None = None
    ) -> FileResponse:
        """Everything a production team needs, as one zip: story bible, scripts, storyboard
        and shot list (CSV), cast/set/prop sheets, asset requirements with the generated
        reference images, QA report, sources and the full package JSON."""
        pkg = _package_or_404(package_id, revision_id)
        job = _package_job(pkg.package_id, job_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        ids = [s.asset_id for s in job.request.sources if s.asset_id] if job else []
        assets = {k: SourceAsset.model_validate(v) for k, v in repo.get_source_assets(ids).items()}
        tmp = _new_export_file()
        try:
            name = await asyncio.to_thread(
                build_export,
                pkg,
                tmp,
                job=job,
                images=images,
                assets=assets,
                media=media,
                options=repo.list_options(pkg.package_id, pkg.revision_id),
                keyframes=repo.list_shot_takes(pkg.package_id, pkg.revision_id),
                plans=repo.list_shot_plans(pkg.package_id, pkg.revision_id),
                readings=repo.list_scene_readings(pkg.package_id, pkg.revision_id),
            )
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return FileResponse(
            tmp,
            media_type="application/zip",
            filename=name,
            background=BackgroundTask(tmp.unlink, missing_ok=True),
        )

    @app.get("/v1/drama-packages/{package_id}/{sheet}", response_model=None)
    async def get_sheet(
        package_id: str,
        sheet: Literal["cast-sheet", "set-sheet", "prop-sheet"],
        revision_id: str | None = None,
    ) -> CastSheet | SetSheet | PropSheet:
        """One design sheet as its own document (schema: /v1/schemas/{sheet}.json)."""
        pkg = repo.get_package(package_id, revision_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        doc = getattr(pkg, sheet.replace("-", "_"))
        if doc is None:
            raise HTTPException(404, f"this package was made before {sheet}s existed")
        return doc

    @app.get("/v1/schemas/{name}.json")
    async def get_schema(name: str) -> dict:
        """JSON Schema of a standalone document: cast-sheet, set-sheet or prop-sheet."""
        model = SHEET_SCHEMAS.get(name)
        if model is None:
            raise HTTPException(404, f"unknown schema {name}; known: {', '.join(SHEET_SCHEMAS)}")
        schema = model.model_json_schema()
        schema["$id"] = f"/v1/schemas/{name}.json"
        return schema

    @app.post("/v1/drama-packages/{package_id}/revisions", status_code=202, response_model=Revision)
    async def create_revision(package_id: str, req: RevisionRequest) -> Revision:
        pkg = repo.get_package(package_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        if req.base_revision_id != pkg.revision_id:
            # Prevents overwriting a newer edit.
            raise HTTPException(409, f"base_revision_id is stale; latest is {pkg.revision_id}")
        impact = analyze_impact(pkg, req.target_ids, req.locked_ids, req.visual_change)
        if impact.unknown_targets:
            raise HTTPException(422, f"unknown target_ids: {impact.unknown_targets}")

        rev = Revision(
            revision_id=new_id("rev"),
            package_id=package_id,
            base_revision_id=pkg.revision_id,
            request=req,
            impacted_ids=impact.impacted_ids,
            stale_requirement_ids=impact.stale_requirement_ids,
            conflicts=[f"target {c} is locked" for c in impact.conflicts],
            locked_in_scope=impact.locked_in_scope,
            status="needs_revision" if impact.conflicts else "queued",
        )
        repo.save_revision(rev)
        # TODO: run the scoped regeneration worker (re-run only impacted steps with
        # locked_ids enforced via check_locks), then publish a new package revision.
        return rev
