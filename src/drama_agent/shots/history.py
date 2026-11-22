"""The history of a shot's cut plans: every plan that was replaced is kept, with the
keyframes approved for it then, and can be restored with them.

Keyframe takes are never deleted when a plan changes: a take drawn for a sub-shot the plan
no longer has is only hidden (plans.current_sub) and its approval withdrawn. So restoring an
old plan brings its takes back, and its approvals are put back where the draw still exists.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ..models import ProductionPackage
from ..models.package import utcnow
from ..models.storyboard import Shot
from .approvals import stale_approvals
from .inserts import restore_moves
from .models import ShotPlan, ShotPlanVersion
from .plans import current_sub, make_sub_shots

log = logging.getLogger(__name__)


def replace_plan(
    repo: Any,
    pkg: ProductionPackage,
    shot: Shot,
    plan: ShotPlan,
    by: Literal["model", "user", "restore"],
    *,
    previous: ShotPlan | None = None,
) -> ShotPlanVersion | None:
    """Save `plan` as the shot's plan: first the plan it replaces (`previous`, else the
    stored one), if it had cuts and they change, goes to the history with the approvals it
    has now; then the approvals the new plan no longer matches are withdrawn. Returns the
    version kept. A history that can't be written is logged, never costs the new plan."""
    old = previous or repo.get_shot_plan(plan.package_id, plan.revision_id, plan.shot_id)
    kept = None
    if old is not None and old.sub_shots and old.sub_shots != plan.sub_shots:
        takes = repo.list_shot_takes(plan.package_id, plan.revision_id)
        approved = {t.take_id: t.approved for t in takes
                    if t.shot_id == plan.shot_id and t.approved is not None}  # fmt: skip
        try:
            kept = repo.add_plan_version(ShotPlanVersion(
                package_id=plan.package_id, revision_id=plan.revision_id,
                shot_id=plan.shot_id, version=0,  # numbered by the store
                plan=old.model_copy(update={"status": "done", "error": None}),
                approved=approved, replaced_by=by,
            ))  # fmt: skip
        except Exception:
            log.warning("the replaced plan of %s couldn't be kept", plan.shot_id, exc_info=True)
    repo.save_shot_plan(plan)
    takes = repo.list_shot_takes(plan.package_id, plan.revision_id)
    for t in stale_approvals(pkg, shot, takes, plan):
        repo.approve_shot_draw(t.take_id, None)
    return kept


def restore_plan(
    repo: Any, pkg: ProductionPackage, shot: Shot, version: int
) -> tuple[ShotPlan, int]:
    """Make an earlier plan the shot's plan again: the plan in force goes to the history and
    the restored one leaves it. Its approvals are put back in cut order where the take and
    its draw still exist. Returns (the plan, keyframes approved again). Raises KeyError for
    a version the shot doesn't have, ValueError when its cuts no longer fit the shot (a
    revision changed who is in it or how long it runs)."""
    found = [v for v in repo.list_plan_versions(pkg.package_id, pkg.revision_id, shot.shot_id)
             if v.version == version]  # fmt: skip
    if not found:
        raise KeyError(version)
    v = found[0]
    # Checked as a person's plan is, except for the problems a plan may be kept with
    # (flagged on the board): people not in the shot or cuts outside the clip are refused.
    subs = make_sub_shots(pkg, shot, [s.model_dump() for s in v.plan.sub_shots],
                          v.plan.clip_ms, strict=False)  # fmt: skip
    plan = v.plan.model_copy(update={"revision_id": pkg.revision_id, "sub_shots": subs,
                                     "status": "done", "error": None,
                                     "updated_at": utcnow()})  # fmt: skip
    # Undoing an inserted cut, or restoring it again: the keyframes move with their cuts.
    current = repo.get_shot_plan(pkg.package_id, pkg.revision_id, shot.shot_id)
    if current is not None:
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        for t in restore_moves(pkg, shot, current, plan, takes):
            repo.save_shot_take(t)
    replace_plan(repo, pkg, shot, plan, "restore")
    repo.delete_plan_version(pkg.package_id, pkg.revision_id, shot.shot_id, version)
    by_id = {t.take_id: t for t in repo.list_shot_takes(pkg.package_id, pkg.revision_id)}
    back = 0
    for take_id, n in sorted(v.approved.items(),
                             key=lambda kv: getattr(by_id.get(kv[0]), "sub_shot", 0)):  # fmt: skip
        t = by_id.get(take_id)
        if t is None or current_sub(pkg, shot, plan, t) is None:
            continue
        if not any(d.n == n and d.status == "done" for d in t.results):
            continue
        repo.approve_shot_draw(take_id, n)
        back += 1
    return plan, back
