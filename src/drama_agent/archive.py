"""Archive stories written before the app became English-only.

The app writes stories in American English set in the present-day US. Jobs from before
that (Chinese-language stories) are archived, not deleted: they are hidden from the job
list, can't be retried or resumed, and the UI doesn't render their content. Their data
stays in the database.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .models import Job
from .models.package import utcnow

log = logging.getLogger(__name__)

# Chinese, Japanese and Korean scripts and their punctuation.
_CJK = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]"
)

ARCHIVE_REASON = "Written in Chinese before the app became English-only."


def has_cjk(text: str | None) -> bool:
    return bool(text and _CJK.search(text))


def is_non_english(job: Job, repo: Any) -> bool:
    """Whether a job's story isn't in English.

    New jobs are always saved with language en-US (the API refuses anything else), so an
    explicit English language is trusted as is: an English idea that quotes a few Chinese
    or Japanese characters is not archived. Only jobs from before that rule, with no
    language set, are judged by their text and package.
    """
    language = (job.request.options.language or "").lower()
    if language:
        return not language.startswith("en")
    if any(has_cjk(src.text) for src in job.request.sources):
        return True
    if job.package_id:
        pkg = repo.get_package(job.package_id, job.revision_id)
        if pkg is not None:
            bible = pkg.story_bible
            if pkg.source_brief.language.lower().startswith("zh"):
                return True
            if has_cjk(bible.title) or has_cjk(bible.logline):
                return True
    return False


def archive_non_english(repo: Any) -> int:
    """Archive every not-yet-archived job whose story isn't English. Idempotent; returns
    how many were archived.

    Only candidates are loaded (not archived, language not en-US), so the cost stays
    bounded by the old jobs, not the whole history. A running job is left alone: a worker
    in another process may still save its own copy, which would undo the archive. It is
    archived on a later start, once it has finished. Queued or paused jobs are cancelled.
    """
    n = 0
    for job in repo.list_archive_candidates():
        if job.status == "running" or not is_non_english(job, repo):
            continue
        job.archived, job.archive_reason = True, ARCHIVE_REASON
        if job.status in ("queued", "paused"):
            job.status = "cancelled"
        job.updated_at = utcnow()
        repo.save_job(job)
        n += 1
    if n:
        log.info("archived %d non-English stories", n)
    return n
