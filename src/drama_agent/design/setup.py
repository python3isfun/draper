"""Uploads' likeness, thumbnails, and the image clients from the environment."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from ..imagegen import ImageClient
from ..media import MediaStore, ffmpeg_available

log = logging.getLogger(__name__)

# Uploads used for these carry no one's likeness into the images (content rule 3).
NO_LIKENESS_USES = {"location_reference", "visual_style"}


def package_has_uploads(repo: Any, package_id: str) -> bool:
    """Whether any job behind the package had uploaded photos or videos that may carry a
    real person's likeness: character references, or uploads with no stated use (a story
    seed photo may show people). Location and style references don't. Unknown counts as
    yes."""
    jobs = repo.jobs_for_package(package_id)
    return not jobs or any(
        s.type in ("image", "video") and s.use_as not in NO_LIKENESS_USES
        for j in jobs
        for s in j.request.sources
    )


def make_thumb(
    media: MediaStore, rel: str, *, px: int = 512, suffix: str = "_thumb", quality: int = 4
) -> str | None:
    """A JPEG with a `px` long edge next to a stored image (default: the UI's 512 px
    thumbnail; full-size images are ~4 MP). Blocking (runs ffmpeg); None without ffmpeg or
    when the image can't be read."""
    if not ffmpeg_available():
        return None
    src = media.path(rel)
    out = src.with_name(f"{src.stem}{suffix}.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
             "-vf", f"scale='if(gt(iw,ih),{px},-2)':'if(gt(iw,ih),-2,{px})'",
             "-q:v", str(quality), str(out)],
            capture_output=True, timeout=60, check=True,
        )  # fmt: skip
    except (subprocess.SubprocessError, OSError):
        log.warning("no %s px copy of %s", px, rel)
        return None
    return str(out.relative_to(media.root))


def image_client_from_env() -> ImageClient:
    """Real images only when the model client is the real one (DRAMA_MODEL_CLIENT=mcphub)
    or DRAMA_IMAGE_CLIENT=minimax; otherwise local placeholders. Same opt-in rule as the
    text model: a key in the environment alone never triggers GPU calls."""
    import os

    from ..imagegen import FixtureImageClient, MiniMaxImageClient

    kind = _image_kind()
    if kind == "fixture":
        return FixtureImageClient(delay_s=float(os.environ.get("DRAMA_FIXTURE_DELAY_S", "0")))
    if kind == "minimax":
        return MiniMaxImageClient(
            api_key=os.environ["MCPHUB_KEY"],
            base_url=os.environ.get("MCPHUB_IMAGE_URL", "https://api.mcphub.com/api/v3/gpu-images"),
            model=os.environ.get("MCPHUB_IMAGE_MODEL", "minimax-h3-image"),
        )
    raise RuntimeError(f"unknown DRAMA_IMAGE_CLIENT={kind!r}")


def _image_kind() -> str:
    """DRAMA_IMAGE_CLIENT, else minimax with the real text model, else fixture."""
    import os

    return os.environ.get("DRAMA_IMAGE_CLIENT") or (
        "minimax"
        if os.environ.get("DRAMA_MODEL_CLIENT", "fixture").lower() == "mcphub"
        else "fixture"
    )


def keyframe_providers_from_env() -> dict[str, ImageClient]:
    """Keyframe providers besides H3, by name: "nano-banana" (Gemini's image models) when
    GEMINI_API_KEY is set (GEMINI_URL for a gateway, GEMINI_IMAGE_MODEL for the model,
    default gemini-nano-banana-2.1). With fixture images, a local stand-in, so the choice
    can be tried offline."""
    import os

    from ..imagegen import (
        GEMINI_MAX_REFERENCES,
        GEMINI_MODEL,
        GEMINI_URL,
        FixtureImageClient,
        GeminiImageClient,
    )

    model = os.environ.get("GEMINI_IMAGE_MODEL", GEMINI_MODEL)
    if key := os.environ.get("GEMINI_API_KEY"):
        return {"nano-banana": GeminiImageClient(
            api_key=key, base_url=os.environ.get("GEMINI_URL", GEMINI_URL), model=model)}  # fmt: skip
    if _image_kind() == "fixture":
        return {"nano-banana": FixtureImageClient(model_id=f"fixture-{model}",
                                                  max_references=GEMINI_MAX_REFERENCES)}  # fmt: skip
    return {}
