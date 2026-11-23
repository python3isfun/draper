"""HTTP endpoints of episode runs and shot clips (shots/episode.py, shots/clips.py)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..models import ProductionPackage
from ..shots.clips import ShotClip, clip_state, clip_view, latest_clip
from ..shots.common import shot_of
from ..shots.episode import EpisodeRun, EpisodeRunner, RunSettings, episode_shots
from ..shots.plans import plan_of
from ..shots.video_prompt import fingerprint, prompt_state


class RunRequest(RunSettings):
    revision_id: str | None = None


class SettingsPatch(BaseModel):
    draws: int | None = Field(None, ge=1, le=4)
    max_rounds: int | None = Field(None, ge=1, le=3)
    provider: str | None = None
    holds: list[str] | None = None
    hold_shots: list[str] | None = None
    skip_shots: list[str] | None = None
    approve_failed: bool | None = None
    render: bool | None = None
    assemble: bool | None = None
    parallel: int | None = Field(None, ge=1, le=6)


class ReleaseRequest(BaseModel):
    key: str = Field(min_length=1, max_length=200)


class ShotRequest(BaseModel):
    shot_id: str


class RevisionBody(BaseModel):
    revision_id: str | None = None


class EpisodeItem(BaseModel):
    episode_id: str
    number: int
    title: str
    shots: int
    run: dict[str, Any] | None = None  # the newest run's status, if there is one


def register(
    app: FastAPI,
    *,
    repo: Any,
    episodes: EpisodeRunner,
    package_or_404: Callable[[str, str | None], ProductionPackage],
    package_job: Callable[..., Any],
    file_response: Callable[[str, str], FileResponse],
) -> None:
    def _writable(package_id: str, revision_id: str | None) -> ProductionPackage:
        pkg = package_or_404(package_id, revision_id)
        package_job(pkg.package_id, None, pkg.revision_id)  # refuses archived stories
        return pkg

    def _run_or_404(run_id: str) -> EpisodeRun:
        run = repo.get_episode_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return run

    def _view(run: EpisodeRun) -> dict[str, Any]:
        pkg = repo.get_package(run.package_id, run.revision_id)
        if pkg is None:
            raise HTTPException(409, "the run's package revision is gone")
        return episodes.progress(run, pkg)

    def _summary(run: EpisodeRun) -> dict[str, Any]:
        return run.model_dump(mode="json", include={
            "run_id", "status", "created_at", "updated_at", "finished_at", "error", "holds"})  # fmt: skip

    @app.get("/v1/drama-packages/{package_id}/episodes", response_model=list[EpisodeItem])
    async def list_episodes(package_id: str, revision_id: str | None = None) -> list[EpisodeItem]:
        """The story's episodes, each with its shot count and its newest run."""
        pkg = package_or_404(package_id, revision_id)
        runs = repo.list_episode_runs(pkg.package_id, pkg.revision_id)
        out = []
        for ep in pkg.episodes:
            mine = [r for r in runs if r.episode_id == ep.episode_id]
            out.append(EpisodeItem(
                episode_id=ep.episode_id, number=ep.number, title=ep.title,
                shots=sum(1 for s in pkg.shot_list if s.episode_id == ep.episode_id),
                run=_summary(mine[-1]) if mine else None,
            ))  # fmt: skip
        return out

    @app.post(
        "/v1/drama-packages/{package_id}/episodes/{episode_id}/run",
        status_code=202,
        response_model=None,
    )
    async def start_run(package_id: str, episode_id: str, req: RunRequest) -> dict[str, Any]:
        """Generate the episode: plan every shot's cuts, draw and approve its keyframes,
        write its video prompts, render its clips and cut them together, holding where the
        settings say a person decides. One run per episode at a time."""
        pkg = _writable(package_id, req.revision_id)
        try:
            episode_shots(pkg, episode_id)
        except KeyError:
            raise HTTPException(404, f"no episode {episode_id}") from None
        settings = RunSettings.model_validate(req.model_dump(exclude={"revision_id"}))
        if settings.provider is not None:
            try:
                episodes.frames.provider(settings.provider)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        run = EpisodeRun(package_id=pkg.package_id, revision_id=pkg.revision_id,
                         episode_id=episode_id, settings=settings)  # fmt: skip
        try:
            episodes.start(run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return _view(run)

    @app.get("/v1/drama-packages/{package_id}/episodes/{episode_id}/run", response_model=None)
    async def latest_run(
        package_id: str, episode_id: str, revision_id: str | None = None
    ) -> dict[str, Any]:
        """The episode's newest run with each shot's stage, counts and a time estimate."""
        pkg = package_or_404(package_id, revision_id)
        runs = repo.list_episode_runs(pkg.package_id, pkg.revision_id, episode_id)
        if not runs:
            raise HTTPException(404, "the episode has no run")
        return _view(runs[-1])

    @app.get("/v1/episode-runs/{run_id}", response_model=None)
    async def get_run(run_id: str) -> dict[str, Any]:
        return _view(_run_or_404(run_id))

    @app.post("/v1/episode-runs/{run_id}/pause", status_code=202, response_model=None)
    async def pause_run(run_id: str, now: bool = False) -> dict[str, Any]:
        """Pause the run: what is already running finishes, nothing new starts. now=true
        also pauses the running takes and drops the clip being rendered."""
        _run_or_404(run_id)
        try:
            return _view(episodes.pause(run_id, now=now))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/v1/episode-runs/{run_id}/resume", status_code=202, response_model=None)
    async def resume_run(run_id: str) -> dict[str, Any]:
        """Go on from the board as it is now (a paused or failed run)."""
        run = _run_or_404(run_id)
        _writable(run.package_id, run.revision_id)
        try:
            return _view(episodes.resume(run_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/v1/episode-runs/{run_id}/release", response_model=None)
    async def release_hold(run_id: str, req: ReleaseRequest) -> dict[str, Any]:
        """Let the run past a hold: a cut's approval ("<shot>:approve", or "approve" for
        every shot), a check it would wait on ("<shot>:prompt", "<shot>:clip"), a failed
        cut ("<shot>:cut<i>", tried again), "render" or "assemble"."""
        _run_or_404(run_id)
        return _view(episodes.release(run_id, req.key))

    @app.post("/v1/episode-runs/{run_id}/retry", response_model=None)
    async def retry_shot(run_id: str, req: ShotRequest) -> dict[str, Any]:
        """Forget a shot's failed tries, holds and skip, so the run tries it again."""
        run = _run_or_404(run_id)
        pkg = package_or_404(run.package_id, run.revision_id)
        try:
            shot_of(pkg, req.shot_id)
        except KeyError:
            raise HTTPException(404, f"no shot {req.shot_id}") from None
        return _view(episodes.retry_shot(run_id, req.shot_id))

    @app.patch("/v1/episode-runs/{run_id}/settings", response_model=None)
    async def patch_settings(run_id: str, req: SettingsPatch) -> dict[str, Any]:
        """Change the run's settings as it goes (holds, skipped shots, draws...)."""
        run = _run_or_404(run_id)
        changes = req.model_dump(exclude_none=True)
        if not changes:
            return _view(run)
        try:
            RunSettings.model_validate({**run.settings.model_dump(), **changes})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return _view(episodes.update_settings(run_id, changes))

    @app.get("/v1/episode-runs/{run_id}/output", response_class=FileResponse)
    async def run_output(run_id: str) -> FileResponse:
        """The assembled episode (mp4)."""
        run = _run_or_404(run_id)
        if not run.output or not run.output.get("file"):
            raise HTTPException(404, "the episode isn't assembled yet")
        return file_response(run.output["file"], "video/mp4")

    @app.get("/v1/episode-runs/{run_id}/poster", response_class=FileResponse)
    async def run_poster(run_id: str) -> FileResponse:
        run = _run_or_404(run_id)
        if not run.output or not run.output.get("poster"):
            raise HTTPException(404, "no poster")
        return file_response(run.output["poster"], "image/jpeg")

    # -- clips ------------------------------------------------------------------

    @app.get("/v1/drama-packages/{package_id}/clips", response_model=None)
    async def list_clips(package_id: str, revision_id: str | None = None) -> dict[str, Any]:
        """Every shot's clips (newest last) and the newest one's state against the shot as
        it is now."""
        pkg = package_or_404(package_id, revision_id)
        clips = repo.list_shot_clips(pkg.package_id, pkg.revision_id)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        plans = {p.shot_id: p for p in repo.list_shot_plans(pkg.package_id, pkg.revision_id)}
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        prompts = {p.shot_id: p for p in repo.list_video_prompts(pkg.package_id, pkg.revision_id)}
        out = []
        for shot in pkg.shot_list:
            mine = [c for c in clips if c.shot_id == shot.shot_id]
            newest = latest_clip(mine, shot.shot_id)
            print_ = None
            vp = prompts.get(shot.shot_id)
            if vp is not None and vp.status == "done":
                plan = plan_of(pkg, shot, plans.get(shot.shot_id))
                context, ids = episodes.frames.video_inputs(pkg, shot)
                if prompt_state(vp, fingerprint(context, ids)) == "current":
                    inputs = episodes.clip_inputs(pkg, shot, plan, vp, takes, images)
                    print_ = inputs.print if inputs else None
            out.append({"shot_id": shot.shot_id, "episode_id": shot.episode_id,
                        "newest": clip_view(newest, clip_state(newest, print_)),
                        "clips": [c.model_dump(mode="json", exclude={"prompt"}) for c in mine]})  # fmt: skip
        return {"package_id": pkg.package_id, "revision_id": pkg.revision_id,
                "rendering": sum(1 for c in clips if c.busy), "shots": out}  # fmt: skip

    @app.post(
        "/v1/drama-packages/{package_id}/clips/{shot_id}", status_code=202, response_model=ShotClip
    )
    async def render_clip(package_id: str, shot_id: str, req: RevisionBody) -> ShotClip:
        """Render the shot's clip now from its current video prompt (one clip renders at a
        time; this one queues behind any other)."""
        pkg = _writable(package_id, req.revision_id)
        try:
            shot = shot_of(pkg, shot_id)
        except KeyError:
            raise HTTPException(404, f"no shot {shot_id}") from None
        vp = repo.get_video_prompt(pkg.package_id, pkg.revision_id, shot_id)
        if vp is None or vp.status != "done":
            raise HTTPException(409, f"{shot_id} has no finished video prompt")
        if any(c.busy for c in repo.list_shot_clips(pkg.package_id, pkg.revision_id)
               if c.shot_id == shot_id):  # fmt: skip
            raise HTTPException(409, f"{shot_id}'s clip is being rendered")
        return episodes.start_render(pkg, shot)

    @app.get("/v1/shot-clips/{clip_id}", response_model=ShotClip)
    async def get_clip(clip_id: str) -> ShotClip:
        clip = repo.get_shot_clip(clip_id)
        if clip is None:
            raise HTTPException(404, "clip not found")
        return clip

    @app.get("/v1/shot-clips/{clip_id}/{variant}", response_class=FileResponse)
    async def clip_file(clip_id: str, variant: str) -> FileResponse:
        """The clip (`content`, mp4) or its first frame (`poster`, JPEG)."""
        clip = repo.get_shot_clip(clip_id)
        if clip is None:
            raise HTTPException(404, "clip not found")
        if variant == "content" and clip.file:
            return file_response(clip.file, clip.content_type)
        if variant == "poster" and clip.poster:
            return file_response(clip.poster, "image/jpeg")
        raise HTTPException(404, "file not found")

    @app.delete("/v1/shot-clips/{clip_id}", status_code=204)
    async def delete_clip(clip_id: str) -> None:
        clip = repo.get_shot_clip(clip_id)
        if clip is None:
            raise HTTPException(404, "clip not found")
        if clip.busy:
            raise HTTPException(409, "the clip is being rendered")
        repo.delete_shot_clip(clip_id)
