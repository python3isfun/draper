"use strict";

/*
 * Update all: brings a story's pictures, cut plans and keyframes up to date in one go.
 * The window first shows what would be done (a survey of the story) and waits for a
 * confirmation, then runs the steps in order, showing each one's progress, and ends with a
 * report of what was done and what waits on a person:
 *   1. Pictures: redraw the out-of-date design pictures (those drawn from them follow) and
 *      wait until the image queue is empty: keyframes are drawn from the picks.
 *   2. Cut plans: re-plan the out-of-date plans the model wrote (each keeps its own pace and
 *      cut count; the old plan goes to its history). A plan you wrote yourself is left to you.
 *   3. Keyframes: draw what can be drawn without an approval: first keyframes (planning
 *      shots without a plan first), the next cut after an approved one, and out-of-date
 *      keyframes. Nothing is approved: approving is the person's check.
 * The cuts waiting for an approval are approved in the window itself (each with the draws of
 * its newest run, their checks and a full-size view; "Details" opens the shot's page), from
 * the first look to the report; approving starts the next cut, which comes back once drawn.
 * It runs in the page: closing the window keeps it running (the header button reopens it);
 * reloading the page stops the steps not started yet (work already started finishes on the
 * server). Uses h(), api(), state, $, go(), numberSelect() and frames (shots.js).
 */

const upd = { el: null, run: null };
const UPD_POLL_MS = 3000;
const updBase = (pkg) => `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}`;
const updQ = (pkg) => new URLSearchParams({ revision_id: pkg.revision_id });
const updSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const updPlural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const updBusy = () => upd.run && upd.run.phase === "running";

const getImages = (pkg) => api(`${updBase(pkg)}/images?${updQ(pkg)}`);
const getBoard = (pkg) => api(`${updBase(pkg)}/keyframes?${updQ(pkg)}`);

// A picture's name for people: "Linh Tran · work (full body)", else its deliverable id.
function updPictureNames(images) {
  const slots = new Map((images.slots || []).map((s) => [s.deliverable_id, [s.entity, s.variant, s.view && `(${s.view})`].filter(Boolean).join(" · ").replace(" · (", " (")]));
  return new Map(images.images.map((i) => [i.image_id, slots.get(i.deliverable_id) || i.deliverable_id]));
}

const UPD_FINISHED = ["done", "failed", "paused"];

// A shot's cut waiting for a person: its first cut without an approval, with finished
// draws, nothing running and no design pick missing; with the takes of its newest run
// (every round of it). Null when it has none.
function updWaitingCut(s) {
  const sub = s.sub_shots.find((x) => !x.approved);
  if (!sub || s.busy || sub.missing.length) return null;
  const done = sub.takes.filter((t) => UPD_FINISHED.includes(t.status) && t.results.some((d) => d.status === "done"));
  if (!done.length) return null;
  const run = done[done.length - 1].run_id;
  return { shot: s, sub, takes: done.filter((t) => t.run_id === run), earlier: done.filter((t) => t.run_id !== run).length };
}

const updWaiting = (board) => board.shots.map(updWaitingCut).filter(Boolean);

// What the board says needs doing, shot by shot.
function updClassify(board) {
  const out = { replan: [], ownPlans: [], first: [], unplanned: 0, next: [], redraw: [],
    approve: [], picks: [], outdatedKf: [], busy: [], planFailed: [] };
  for (const s of board.shots) {
    if (s.busy) out.busy.push(s.shot_id);
    if (s.plan.status === "failed") out.planFailed.push({ shot_id: s.shot_id, reason: s.plan.error || "planning failed" });
    if (s.plan.outdated && s.plan.status !== "writing") {
      (s.plan.source === "user" ? out.ownPlans : out.replan).push({ shot_id: s.shot_id, reason: s.plan.outdated });
    }
    const outdated = s.sub_shots.filter((x) => (x.outdated || []).length);
    if (outdated.length) {
      out.outdatedKf.push({ shot_id: s.shot_id, cuts: outdated.map((x) => x.index), reason: outdated[0].outdated[0] });
      if (!s.busy) out.redraw.push(s.shot_id);
    }
    const sub = s.sub_shots.find((x) => !x.approved);
    if (!sub) continue;
    if (sub.missing.length) { out.picks.push({ shot_id: s.shot_id, cut: sub.index, reason: sub.missing.join("; ") }); continue; }
    if (s.busy) continue;
    if (updWaitingCut(s)) out.approve.push({ shot_id: s.shot_id, cut: sub.index, of: s.sub_shots.length });
    else if (sub.index === 1) {
      out.first.push(s.shot_id);
      if (s.plan.source === "single" && s.duration_ms > 0) out.unplanned += 1;
    } else out.next.push({ shot_id: s.shot_id, cut: sub.index });
  }
  return out;
}

// ---------------------------------------------------------------------------------------
// What a shot is doing now

const UPD_STAGE = { pending: "waiting for its turn", writing: "writing the prompt", drawing: "drawing", checking: "checking" };

// A busy shot's activity: its cuts being planned, or the take being made (its stage, round,
// draws drawn and checked), with how far along the round is (0..1) and since when.
function updActivity(s) {
  if (s.plan.status === "writing") return { shot: s, text: "planning the cuts", frac: 0.05, since: null };
  for (const sub of s.sub_shots) {
    for (const t of sub.takes) {
      if (!UPD_STAGE[t.status]) continue;
      const drawn = t.results.length;
      const checked = t.results.filter((d) => d.check || d.check_error || d.status === "failed").length;
      const n = t.draws || 1;
      const frac = t.status === "pending" ? 0 : t.status === "writing" ? 0.1
        : t.status === "drawing" ? 0.15 + (0.55 * drawn) / n : 0.7 + (0.3 * checked) / n;
      const stage = t.status === "drawing" ? `drawing ${Math.min(drawn + 1, n)} of ${n}`
        : t.status === "checking" ? `checking ${Math.min(checked + 1, n)} of ${n}` : UPD_STAGE[t.status];
      const round = t.max_rounds > 1 ? ` · round ${t.round} of up to ${t.max_rounds}` : "";
      return { shot: s, text: `cut ${sub.index} of ${s.sub_shots.length}${round} · ${stage}`, frac, since: Date.parse(t.created_at) };
    }
  }
  return { shot: s, text: "starting", frac: 0, since: null };
}

// One line per busy shot (at most `max`, then how many more).
function updActivityList(shots, max = 6) {
  if (!shots.length) return null;
  const rows = shots.slice(0, max).map(updActivity);
  return h("ul", { class: "plain small update-now" },
    rows.map((a) => h("li", {},
      h("span", { class: "update-dot", "aria-hidden": "true" }),
      h("strong", {}, a.shot.shot_id), ` ${a.text}`,
      a.since && h("span", { class: "muted", "data-since": a.since, "data-until": "", "data-clock": "" }, updClock(Date.now() - a.since)))),
    shots.length > max && h("li", { class: "muted" }, `and ${shots.length - max} more`));
}

// ---------------------------------------------------------------------------------------
// The steps

const UPD_STEPS = [
  { key: "pictures", label: "Redraw out-of-date pictures" },
  { key: "plans", label: "Re-plan out-of-date cut plans" },
  { key: "keyframes", label: "Draw the keyframes that can be drawn" },
];

async function updPictures(r, step) {
  const pkg = r.pkg;
  const before = Object.keys(r.images.outdated || {}).length;
  step.total = before;
  if (before) {
    const v = await api(`${updBase(pkg)}/images/refresh?${updQ(pkg)}`, { method: "POST" });
    step.notes = v.notes || [];
  }
  // Pictures drawn from a redrawn one are queued when it finishes: the queue has to stay
  // empty for two polls in a row.
  let empty = 0;
  let v = null;
  while (empty < 2) {
    if (r.stopped) return;
    await updSleep(UPD_POLL_MS);
    v = await getImages(pkg);
    const left = Object.keys(v.outdated || {}).length;
    step.done = Math.max(0, before - left);
    step.detail = v.queued ? `${updPlural(v.queued, "picture")} drawing or queued; ${left} still out of date` : `${left} still out of date`;
    empty = v.queued === 0 ? empty + 1 : 0;
    updRender();
  }
  const names = updPictureNames(v);
  step.left = Object.entries(v.outdated || {}).map(([id, why]) => ({ what: names.get(id) || id, reason: why[0] }));
  step.result = before
    ? `${updPlural(step.done, "picture")} redrawn${step.left.length ? `; ${step.left.length} still out of date` : ""}.`
    : "No picture was out of date.";
}

async function updPlans(r, step) {
  const pkg = r.pkg;
  const todo = updClassify(await getBoard(pkg)).replan;
  step.total = todo.length;
  step.failed = [];
  const started = [];
  for (const p of todo) {
    if (r.stopped) return;
    try {
      // No pace or cuts: the plan keeps its own.
      await api(`${updBase(pkg)}/keyframes/${encodeURIComponent(p.shot_id)}/plan`, { method: "POST", body: JSON.stringify({ revision_id: pkg.revision_id }) });
      started.push(p.shot_id);
    } catch (e) {
      step.failed.push({ shot_id: p.shot_id, reason: e.message });
    }
    step.detail = `${started.length} of ${todo.length} sent to the planner`;
    updRender();
  }
  let board = null;
  while (started.length) {
    if (r.stopped) return;
    await updSleep(UPD_POLL_MS);
    board = await getBoard(pkg);
    const mine = board.shots.filter((s) => started.includes(s.shot_id));
    const writing = mine.filter((s) => s.plan.status === "writing").length;
    step.done = mine.length - writing;
    step.detail = writing ? `${step.done} of ${started.length} planned; ${writing} being written` : `${step.done} of ${started.length} planned`;
    updRender();
    if (!writing) break;
  }
  for (const s of (board ? board.shots : []).filter((x) => started.includes(x.shot_id))) {
    if (s.plan.status === "failed") step.failed.push({ shot_id: s.shot_id, reason: s.plan.error || "planning failed" });
    else if (s.plan.problems.length) (step.problems ||= []).push({ shot_id: s.shot_id, reason: s.plan.problems.join("; ") });
  }
  step.done = started.length - step.failed.filter((f) => started.includes(f.shot_id)).length;
  step.result = todo.length
    ? `${updPlural(step.done, "shot")} re-planned${step.failed.length ? `; ${step.failed.length} failed` : ""}.`
    : "No plan the model wrote was out of date.";
}

async function updKeyframes(r, step) {
  const pkg = r.pkg;
  const c = updClassify(await getBoard(pkg));
  const opts = { draws: r.draws, max_rounds: r.rounds, provider: r.provider || null };
  const shots = new Set();
  step.skipped = [];
  step.detail = `Starting: ${updPlural(c.first.length, "first keyframe")}, ${updPlural(c.next.length, "next cut")}, ${updPlural(c.redraw.length, "out-of-date shot")}`;
  updRender();
  // The next cut after an approved one.
  for (const n of c.next) {
    if (r.stopped) return;
    try {
      await api(`${updBase(pkg)}/keyframes/${encodeURIComponent(n.shot_id)}`, { method: "POST",
        body: JSON.stringify({ revision_id: pkg.revision_id, sub_shot: n.cut, auto: true, ...opts }) });
      shots.add(n.shot_id);
    } catch (e) {
      step.skipped.push({ shot_id: n.shot_id, reason: `cut ${n.cut}: ${e.message}` });
    }
  }
  // First keyframes (shots without a plan are planned first).
  if (c.first.length && !r.stopped) {
    const out = await api(`${updBase(pkg)}/keyframes`, { method: "POST",
      body: JSON.stringify({ revision_id: pkg.revision_id, shot_ids: c.first, plan: true, ...opts }) });
    const takes = await getBoard(pkg);
    for (const s of takes.shots) if (s.busy && c.first.includes(s.shot_id)) shots.add(s.shot_id);
    out.planning.forEach((id) => shots.add(id));
    step.skipped.push(...out.skipped);
  }
  // Out-of-date keyframes (a shot just started above is skipped: its cut is being drawn).
  if (c.redraw.length && !r.stopped) {
    const out = await api(`${updBase(pkg)}/keyframes`, { method: "POST",
      body: JSON.stringify({ revision_id: pkg.revision_id, outdated: true, ...opts }) });
    const board = await getBoard(pkg);
    for (const s of board.shots) if (s.busy && c.redraw.includes(s.shot_id)) shots.add(s.shot_id);
    step.skipped.push(...out.skipped.filter((s) => !shots.has(s.shot_id)));
  }
  step.total = shots.size;
  step.shots = [...shots];
  // A shot is finished the first time it isn't busy: a next cut started from the window by
  // an approval afterwards isn't this step's to wait for.
  const finished = new Set();
  while (shots.size) {
    if (r.stopped) return;
    await updSleep(UPD_POLL_MS);
    const { board, fresh } = await updBoard(r);
    for (const s of board.shots) if (shots.has(s.shot_id) && !s.busy) finished.add(s.shot_id);
    const running = shots.size - finished.size;
    step.done = finished.size;
    step.finished = finished;
    // The bar moves with each draw and check, not only when a whole shot is done.
    const partial = board.shots.filter((s) => shots.has(s.shot_id) && !finished.has(s.shot_id))
      .reduce((sum, s) => sum + updActivity(s).frac, 0);
    step.frac = (finished.size + partial) / shots.size;
    const detail = running ? `${step.done} of ${updPlural(shots.size, "shot")} finished; ${running} in progress:` : `${shots.size} of ${shots.size} shots finished`;
    if ((fresh && updLive(r, board)) || detail !== step.detail) {
      step.detail = detail;
      updRender();
    }
    if (!running) break;
  }
  step.result = shots.size
    ? `Keyframes drawn for ${updPlural(shots.size, "shot")}${step.skipped.length ? `; ${step.skipped.length} couldn't start` : ""}.`
    : step.skipped.length ? `Nothing could start (${step.skipped.length} shots, see below).` : "No keyframe needed drawing.";
}

const UPD_RUN = { pictures: updPictures, plans: updPlans, keyframes: updKeyframes };

// ---------------------------------------------------------------------------------------
// Flow: survey -> confirm -> run -> report

// Whether a step has something to do, by the first look (its default tick).
function updStepWanted(r, key) {
  const pics = Object.keys(r.images.outdated || {}).length;
  if (key === "pictures") return pics > 0 || r.images.queued > 0;
  if (key === "plans") return r.plan.replan.length > 0;
  const kf = r.plan.first.length + r.plan.next.length + r.plan.redraw.length;
  return kf > 0 || r.plan.replan.length > 0 || pics > 0;
}

async function updSurvey(pkg) {
  // The job the story belongs to: the report's shot links open it.
  const jobId = upd.run && upd.run.pkg.package_id === pkg.package_id ? upd.run.jobId : state.job && state.job.job_id;
  const r = { pkg: { package_id: pkg.package_id, revision_id: pkg.revision_id }, jobId, phase: "survey",
    draws: frames.draws, rounds: frames.rounds, provider: frames.provider, error: "",
    approved: [], next: true, shown: UPD_PAGE };
  upd.run = r;
  updRender();
  try {
    const [images, board] = await Promise.all([getImages(pkg), getBoard(pkg)]);
    if (upd.run !== r) return;
    Object.assign(r, { images, board, plan: updClassify(board), phase: "plan" });
    updLive(r, board);
    r.steps = UPD_STEPS.map((s) => ({ ...s, status: "waiting", on: updStepWanted(r, s.key) }));
  } catch (e) {
    r.phase = "error";
    r.error = e.message;
  }
  updRender();
}

async function updStart() {
  const r = upd.run;
  r.phase = "running";
  r.started = Date.now();
  updRender();
  for (const step of r.steps) {
    if (!step.on) { step.status = "skipped"; step.result = "Not chosen."; continue; }
    if (r.stopped) { step.status = "skipped"; step.result = "Stopped before this step."; continue; }
    step.status = "running";
    step.started = Date.now();
    updRender();
    try {
      await UPD_RUN[step.key](r, step);
      step.status = r.stopped && !step.result ? "stopped" : "done";
      if (step.status === "stopped") step.result = "Stopped. What was already started goes on on the server.";
    } catch (e) {
      step.status = "failed";
      step.result = e.message;
    }
    step.ended = Date.now();
    updRender();
  }
  try {
    const [images, board] = await Promise.all([getImages(r.pkg), getBoard(r.pkg)]);
    r.after = { images, board, todo: updClassify(board) };
    updLive(r, board);
  } catch (e) {
    r.error = `Couldn't read the story for the report: ${e.message}`;
  }
  r.ended = Date.now();
  r.phase = "done";
  updRender();
  // The open views show the new state.
  if (state.pkg && state.pkg.package_id === r.pkg.package_id) {
    if (typeof loadFrames === "function" && frames.key === framesKey(state.pkg)) loadFrames(state.pkg);
    if (typeof loadImages === "function") loadImages(state.pkg);
  }
  if (typeof renderMain === "function") renderMain();
}

// ---------------------------------------------------------------------------------------
// Approving in the window

const UPD_PAGE = 8;  // waiting cuts shown at a time
// Takes a fresh board; says whether anything the window shows changed (so a poll that
// changes nothing doesn't redraw the pictures).
function updLive(r, board) {
  const sig = JSON.stringify(board.shots.map((s) => [s.busy, s.sub_shots.map((x) => [x.approved,
    x.takes.map((t) => [t.take_id, t.status, t.results.map((d) => [d.status, d.check && d.check.score])])])]));
  r.live = board;
  if (r.after) Object.assign(r.after, { board, todo: updClassify(board) });
  if (r.phase === "plan") {
    // The first look's counts follow approvals made in it; a step nobody ticked or
    // unticked is on when it has something to do.
    r.board = board;
    r.plan = updClassify(board);
    for (const s of r.steps || []) if (!s.touched) s.on = updStepWanted(r, s.key);
  }
  // An approval withdrawn (here, by withdrawing the cut before it, or elsewhere) leaves the list.
  r.approved = (r.approved || []).filter((a) => {
    const s = board.shots.find((x) => x.shot_id === a.shot_id);
    const mine = s && s.sub_shots[a.cut - 1];
    return mine && mine.approved && mine.approved[0] === a.take.take_id;
  });
  const changed = sig !== r.sig;
  r.sig = sig;
  return changed;
}

// The board, asked for in turn: an answer is used only when no later-asked board has been
// used already (a slow poll never puts back what an approval changed).
async function updBoard(r) {
  const seq = (r.asked = (r.asked || 0) + 1);
  const board = await getBoard(r.pkg);
  const fresh = seq > (r.applied || 0);
  if (fresh) r.applied = seq;
  return { board, fresh };
}

// Reads the board again (after an action, or while something draws); the report's lists
// follow it. A failed read shows until the next one succeeds.
async function updRefresh(r, always = true) {
  let changed = always;
  try {
    const { board, fresh } = await updBoard(r);
    changed = (fresh && updLive(r, board)) || always || Boolean(r.pollError);
    r.pollError = "";
  } catch (e) {
    r.pollError = `Couldn't read the board: ${e.message}`;
    changed = true;
  }
  if (!changed) return;
  updRender();
  // The Keyframes tab underneath shows it too (unless it is polling the board itself).
  if (state.pkg && state.pkg.package_id === r.pkg.package_id && frames.key === framesKey(state.pkg) && !frames.timer && !frames.loading) {
    loadFrames(state.pkg);
  }
}

async function updApprove(r, take, n) {
  r.acting = true;
  r.actionError = "";
  updRender();
  try {
    await api(`/v1/shot-takes/${encodeURIComponent(take.take_id)}/approve`, { method: "POST", body: JSON.stringify({ n, next: r.next }) });
    r.approved = r.approved.filter((a) => !(a.shot_id === take.shot_id && a.cut === take.sub_shot));
    r.approved.unshift({ shot_id: take.shot_id, cut: take.sub_shot, take, n });
  } catch (e) {
    r.actionError = e.message;
  }
  r.acting = false;
  await updRefresh(r);
}

async function updWithdraw(r, a) {
  r.acting = true;
  r.actionError = "";
  updRender();
  try {
    await api(`/v1/shot-takes/${encodeURIComponent(a.take.take_id)}/approve`, { method: "DELETE" });
  } catch (e) {
    r.actionError = e.message;
  }
  r.acting = false;
  await updRefresh(r);
}

// A draw's standing for "best check": only a draw that passed its check counts.
const updBest = (d) => (d.check && d.check.passed ? d.check.score : null);
const updDrawLabel = (take, d) => `Round ${take.round} · draw ${d.n + 1}`;

// A card is built once per cut and state of its takes, then reused on every redraw: its
// pictures don't reload and its open details stay open.
function updApproveCard(r, item) {
  const key = `${item.shot.shot_id}|${item.sub.index}`;
  const sig = JSON.stringify([item.sub.framing, item.earlier, item.takes.map((t) => [t.take_id, t.status, t.results.map((d) => [d.status, d.check && d.check.score])])]);
  const kept = (r.cards ||= new Map()).get(key);
  if (kept && kept.sig === sig) return kept.el;
  const el = updBuildCard(r, item);
  r.cards.set(key, { sig, el });
  return el;
}

function updBuildCard(r, item) {
  const { shot, sub } = item;
  const draws = item.takes.flatMap((t) => t.results.filter((d) => d.status === "done").map((d) => ({ take: t, d })));
  // The passing draw with the best score, when one beats the others.
  const passed = draws.filter((x) => updBest(x.d) != null);
  const top = Math.max(...passed.map((x) => updBest(x.d)));
  const best = passed.filter((x) => updBest(x.d) === top).length === 1 ? passed.find((x) => updBest(x.d) === top) : null;
  const where = `${shot.shot_id} · cut ${sub.index} of ${shot.sub_shots.length}`;
  const big = draws.map(({ take, d }) => ({ src: drawUrl(take, d.n, "content"), caption: `${where} · ${updDrawLabel(take, d)}`,
    title: d.check ? `${d.check.passed ? "pass" : "fail"} ${d.check.score}: ${d.check.summary || ""}` : "", likeness: take.likeness }));
  return h("article", { class: "update-approve" },
    h("div", { class: "update-approve-head" },
      h("strong", {}, where),
      h("span", { class: "small muted" }, `${(sub.start_ms / 1000).toFixed(1)}–${secs(sub.end_ms)} · ${shot.place}`),
      updShotLink(shot.shot_id, "", "Details")),
    h("p", { class: "small" }, h("strong", {}, sub.framing), sub.camera ? ` (${sub.camera})` : "", ` ${sub.action}`),
    (sub.lines || []).length > 0 && h("p", { class: "small muted" }, sub.lines.map((l) => `${l.speaker || l.speaker_id}: "${l.text}"`).join("  ")),
    h("div", { class: "update-draws" }, draws.map(({ take, d }, i) => {
      const failed = d.check ? d.check.items.filter((x) => !x.passed) : [];
      const isBest = best && best.d === d && best.take === take;
      return h("figure", { class: `update-draw${isBest ? " best" : ""}` },
        h("button", { type: "button", class: "update-thumb", "data-focus": `big:${take.take_id}:${d.n}`, title: "See it full size (arrows step through the draws)", onclick: () => enlarge(big, i) },
          h("img", { src: drawUrl(take, d.n), alt: updDrawLabel(take, d), loading: "lazy" }), lookalikeTag(take.likeness)),
        h("figcaption", {},
          h("div", { class: "small" }, updDrawLabel(take, d)),
          h("div", { class: "update-draw-tags" }, checkBadge(d), isBest && h("span", { class: "chip info" }, "best check")),
          h("button", { type: "button", class: "btn small primary-outline", "data-act": "", "data-focus": `ok:${take.take_id}:${d.n}`,
            title: "Approve this keyframe (with the setting above, the next cut starts, drawn from it)", onclick: () => updApprove(r, take, d.n) }, "Approve"),
          (failed.length > 0 || (d.check && d.check.summary)) && stickyDetails(`upd-${take.take_id}-${d.n}`,
            failed.length ? `${failed.length} issue${failed.length > 1 ? "s" : ""}` : "Check",
            d.check.summary && h("p", { class: "muted small" }, d.check.summary),
            h("ul", { class: "plain check-items small" }, failed.map((x) =>
              h("li", {}, h("strong", {}, x.item), h("span", { class: "muted" }, ` (${x.severity})`), `: ${x.finding}`))),
            d.check.fix && h("p", { class: "small" }, h("strong", {}, "Fix: "), d.check.fix))));
    })),
    item.earlier > 0 && h("p", { class: "small muted" }, `${updPlural(item.earlier, "earlier take")} of this cut: see Details.`));
}

// What became of a cut approved here: its next cut drawing, waiting, approved, or none.
function updAfterApproval(r, a) {
  const s = r.live && r.live.shots.find((x) => x.shot_id === a.shot_id);
  if (!s) return "";
  const mine = s.sub_shots[a.cut - 1];
  if (!mine || !mine.approved || mine.approved[0] !== a.take.take_id) return "no longer approved";
  const next = s.sub_shots[a.cut];
  if (!next) return "last cut: the shot is complete";
  if (next.approved) return `cut ${next.index} approved too`;
  if (s.busy) return `cut ${next.index} drawing…`;
  if (next.takes.some((t) => t.results.some((d) => d.status === "done"))) return `cut ${next.index} waiting for you`;
  return `cut ${next.index} not started`;
}

function updApprovals(r, open) {
  if (!r.live) return null;
  const waiting = updWaiting(r.live);
  // Busy shots the keyframe step doesn't list already (next cuts started by approving).
  const kf = r.steps && r.steps.find((s) => s.key === "keyframes");
  const listed = kf && kf.status === "running" && kf.shots ? kf.shots.filter((id) => !kf.finished || !kf.finished.has(id)) : [];
  const busy = r.live.shots.filter((s) => s.busy && !listed.includes(s.shot_id));
  const running = busy.length;
  if (!waiting.length && !r.approved.length && !(open && running)) return null;
  const shown = waiting.slice(0, r.shown);
  const body = [
    h("div", { class: "design-row small" },
      h("label", { class: "update-check" }, h("input", { type: "checkbox", checked: r.next, onchange: (e) => { r.next = e.target.checked; } }),
        " Approving a cut starts the next one (drawn from it)"),
      running > 0 && h("span", { class: "small muted" }, "New cuts appear here when drawn.")),
    running > 0 && h("div", { class: "update-now-box" }, h("strong", { class: "small" }, `In progress (${running})`), updActivityList(busy)),
    r.actionError && h("p", { class: "form-error" }, r.actionError),
    r.pollError && h("p", { class: "form-error" }, r.pollError),
    shown.map((item) => updApproveCard(r, item)),
    waiting.length > shown.length && h("button", { type: "button", class: "btn small", onclick: () => { r.shown += UPD_PAGE; updRender(); } },
      `Show ${Math.min(UPD_PAGE, waiting.length - shown.length)} more (${waiting.length - shown.length} left)`),
    !waiting.length && h("p", { class: "small muted" }, running ? "Nothing to approve yet." : "Nothing waits for your approval."),
    r.approved.length > 0 && h("section", { class: "update-report-part" },
      h("h4", {}, h("span", { class: "chip ok" }, r.approved.length), " approved here"),
      h("ul", { class: "plain small update-approved" }, r.approved.map((a) => h("li", {},
        h("button", { type: "button", class: "update-thumb small", title: "See it full size",
          onclick: () => enlarge([{ src: drawUrl(a.take, a.n, "content"), caption: `${a.shot_id} · cut ${a.cut} · ${updDrawLabel(a.take, { n: a.n })}`, likeness: a.take.likeness }]) },
          h("img", { src: drawUrl(a.take, a.n), alt: "", loading: "lazy" })),
        h("span", {}, h("strong", {}, `${a.shot_id} · cut ${a.cut}`), h("span", { class: "muted" }, ` ${updAfterApproval(r, a)}`)),
        updShotLink(a.shot_id, "", "Details"),
        h("button", { type: "button", class: "btn ghost small", disabled: r.acting, "data-focus": `wd:${a.take.take_id}`, onclick: () => updWithdraw(r, a) }, "Withdraw"))))),
  ];
  const title = waiting.length ? `${updPlural(waiting.length, "cut")} waiting for your approval` : "Approvals";
  return open
    ? h("section", { class: "update-approvals" }, h("h3", {}, title), body)
    : stickyDetails("upd-approvals", title, body);
}

// While the window is open and something is drawing, the approvals follow the board (one
// read at a time).
setInterval(async () => {
  const r = upd.run;
  if (!r || !r.live || !upd.el || upd.el.hidden || r.phase === "running" || r.acting || r.polling) return;
  if (!r.live.shots.some((s) => s.busy) && !r.pollError) return;
  r.polling = true;
  try {
    await updRefresh(r, false);
  } finally {
    r.polling = false;
  }
}, UPD_POLL_MS);

// ---------------------------------------------------------------------------------------
// The window

function openUpdate() {
  const pkg = state.pkg;
  if (!pkg) return;
  if (!upd.el) {
    upd.el = h("div", { class: "update-backdrop", onclick: (e) => { if (e.target === upd.el) closeUpdate(); } });
    document.body.append(upd.el);
    // Escape closes a full-size picture first (it sits above the window), then the window.
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && upd.el && !upd.el.hidden && !$(".lightbox")) closeUpdate(); });
  }
  upd.el.hidden = false;
  const same = upd.run && upd.run.pkg.package_id === pkg.package_id && upd.run.pkg.revision_id === pkg.revision_id;
  if (updBusy() || (same && upd.run.phase === "done")) updRender();
  else updSurvey(pkg);
}

function closeUpdate() {
  if (upd.el) upd.el.hidden = true;
  if (upd.run && upd.run.phase !== "running" && upd.run.phase !== "done") upd.run = null;
  if (typeof renderMain === "function") renderMain();  // the header button's label
}

// The header button: "Update all", or the running update's progress.
function updLabel(pkg) {
  const r = upd.run;
  const mine = r && pkg && r.pkg.package_id === pkg.package_id;
  if (mine && r.phase === "running") {
    const at = r.steps.findIndex((s) => s.status === "running") + 1 || r.steps.length;
    return `Updating: step ${at} of ${r.steps.length}…`;
  }
  return mine && r.phase === "done" ? "Update all: report" : "Update all";
}

function updateButton(pkg) {
  return h("button", { type: "button", id: "update-all-btn", class: "btn small primary-outline", onclick: openUpdate,
    title: "Bring the story's pictures, cut plans and keyframes up to date: shows the plan first, runs it in order, then reports what waits on you" }, updLabel(pkg));
}

// Opens the shot on the Keyframes tab (the window closes; the header button reopens it).
const updShotLink = (id, extra = "", label = null) => h("a", { href: "#", class: "update-shot",
  title: "Open this shot on the Keyframes tab: its plan, every take, prompts and checks", onclick: (e) => {
    e.preventDefault();
    const jobId = upd.run.jobId;  // closing the first look drops the run
    closeUpdate();
    go(jobId, "keyframes");
    // Once the tab has drawn the shot (it may redraw as the board loads).
    framesOpenShot(id);
  } }, label || [id, extra]);

// A short list of shots (or pictures) with why, the rest folded.
function updList(items, render) {
  if (!items || !items.length) return null;
  const head = items.slice(0, 6);
  const rest = items.slice(6);
  return h("ul", { class: "plain small update-list" },
    head.map((it) => h("li", {}, render(it))),
    rest.length > 0 && h("li", {}, h("details", {}, h("summary", {}, `${rest.length} more`),
      h("ul", { class: "plain" }, rest.map((it) => h("li", {}, render(it)))))));
}
const shotWhy = (it) => [updShotLink(it.shot_id, it.cut ? ` · cut ${it.cut}` : ""), it.reason ? h("span", { class: "muted" }, ` ${it.reason}`) : null];

function updPlanView(r) {
  const p = r.plan;
  const pics = Object.keys(r.images.outdated || {}).length;
  const kfs = p.first.length + p.next.length + p.redraw.length;
  const names = updPictureNames(r.images);
  const step = (key, body) => {
    const s = r.steps.find((x) => x.key === key);
    const n = r.steps.indexOf(s) + 1;
    return h("li", { class: `update-step ${s.on ? "" : "off"}` },
      h("label", { class: "update-step-head" },
        h("input", { type: "checkbox", checked: s.on, onchange: (e) => { s.on = e.target.checked; s.touched = true; updRender(); } }),
        h("strong", {}, `${n}. ${s.label}`)),
      h("div", { class: "update-step-body" }, body));
  };
  const providers = r.board.providers || {};
  return [
    h("p", { class: "small muted" }, "Here is what would be done, in this order. Nothing has started yet. Untick a step to leave it out."),
    h("ol", { class: "plain update-steps" },
      step("pictures", [
        h("p", { class: "small" }, pics
          ? `${updPlural(pics, "picked picture")} drawn before a design change: each is redrawn and replaces its pick; pictures drawn from it follow. Then it waits for the image queue to empty, since keyframes are drawn from the picks.`
          : r.images.queued ? `No picture is out of date. ${updPlural(r.images.queued, "picture")} in the queue are waited for.` : "No picture is out of date."),
        updList(Object.entries(r.images.outdated || {}).map(([id, why]) => ({ id, why })),
          (it) => [h("strong", {}, names.get(it.id) || it.id), h("span", { class: "muted" }, ` ${it.why[0]}`)]),
      ]),
      step("plans", [
        h("p", { class: "small" }, p.replan.length
          ? `${updPlural(p.replan.length, "cut plan")} written by the model before a change to the shot. Each is planned again with its own clip length, pace and cut count; the old plan goes to its History. Keyframes the new cuts no longer match are withdrawn and drawn again in step 3.`
          : "No plan the model wrote is out of date."),
        updList(p.replan, shotWhy),
        p.ownPlans.length > 0 && h("p", { class: "small warn-text" }, `${updPlural(p.ownPlans.length, "plan")} you wrote yourself ${p.ownPlans.length === 1 ? "is" : "are"} out of date too. They are left for you to edit.`),
      ]),
      step("keyframes", [
        h("p", { class: "small" }, `Draws what can be drawn without an approval, each checked and revised automatically. The list is worked out again after steps 1 and 2. Right now that is:`),
        h("ul", { class: "small update-counts" },
          h("li", {}, `${updPlural(p.first.length, "shot")} without a first keyframe${p.unplanned ? ` (${p.unplanned} planned first)` : ""}`),
          h("li", {}, `${updPlural(p.next.length, "shot")} whose next cut can start from its approved keyframe`),
          h("li", {}, `${updPlural(p.redraw.length, "shot")} with out-of-date keyframes`)),
        h("div", { class: "design-row small" },
          h("label", {}, "Draws ", numberSelect(r.draws, 4, (n) => { r.draws = n; updRender(); }, "Draws per round")),
          h("label", {}, " Rounds ", numberSelect(r.rounds, 3, (n) => { r.rounds = n; updRender(); }, "Automatic rounds")),
          Object.keys(providers).length > 1 && h("label", {}, " Drawn with ",
            h("select", { "aria-label": "Image provider", onchange: (e) => { r.provider = e.target.value; } },
              Object.entries(providers).map(([name, model]) =>
                h("option", { value: name, title: model, selected: (r.provider || r.board.default_provider) === name }, providerName(name)))))),
        kfs > 0 && h("p", { class: "small muted" }, `Up to about ${kfs * r.draws * r.rounds} keyframe images (${kfs} shots × ${r.draws} draws × up to ${r.rounds} rounds).`),
      ])),
    (p.busy.length > 0) && h("p", { class: "small muted" }, `${updPlural(p.busy.length, "shot")} already being planned or drawn: left as they are.`),
    h("p", { class: "small muted" }, "Nothing is approved for you: you approve the cuts here, as they are drawn and in the report. You can close this window while it runs; reloading the page stops the steps that haven't started."),
    updApprovals(r, false),
  ];
}

const UPD_STATUS = { waiting: ["", "waiting"], running: ["info", "running"], done: ["ok", "done"], failed: ["bad", "failed"], skipped: ["", "skipped"], stopped: ["warn", "stopped"] };
const updClock = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const updMins = (ms) => (ms < 60000 ? `${Math.max(1, Math.round(ms / 1000))} s` : `${Math.round(ms / 60000)} min`);

function updStepsView(r) {
  return h("ol", { class: "plain update-steps" }, r.steps.map((s, i) => {
    const [cls, word] = UPD_STATUS[s.status];
    const pct = s.status !== "running" || !s.total ? null
      : Math.round(100 * (s.frac != null ? s.frac : (s.done || 0) / s.total));
    // The keyframe step lists what each of its unfinished shots is doing.
    const now = s.key === "keyframes" && s.status === "running" && r.live && s.shots
      ? r.live.shots.filter((x) => s.shots.includes(x.shot_id) && !(s.finished && s.finished.has(x.shot_id)))
      : [];
    return h("li", { class: `update-step ${s.status}` },
      h("div", { class: "update-step-head" },
        h("span", { class: `chip ${cls}` }, word),
        h("strong", {}, `${i + 1}. ${s.label}`),
        s.started && h("span", { class: "small muted", "data-since": s.started, "data-until": s.ended || "" }, updMins((s.ended || Date.now()) - s.started))),
      s.status === "running" && h("div", { class: "update-bar", role: "progressbar", "aria-valuenow": pct ?? undefined },
        h("div", { class: pct == null ? "indeterminate" : "", style: pct == null ? "" : `width:${pct}%` })),
      s.detail && s.status === "running" && h("p", { class: "small muted" }, s.detail),
      now.length > 0 && updActivityList(now),
      s.result && h("p", { class: "small" }, s.result));
  }));
}

function updReport(r) {
  const a = r.after;
  const byKey = Object.fromEntries(r.steps.map((s) => [s.key, s]));
  const pics = byKey.pictures;
  const plans = byKey.plans;
  const kf = byKey.keyframes;
  const sec = (title, cls, items, render, note) => items && items.length > 0 && h("section", { class: "update-report-part" },
    h("h4", {}, h("span", { class: `chip ${cls}` }, items.length), " ", title),
    note && h("p", { class: "small muted" }, note),
    updList(items, render));
  return h("div", { class: "update-report" },
    h("h3", {}, "Report"),
    h("p", { class: "small muted" }, `Took ${updMins(r.ended - r.started)}.`),
    h("ul", { class: "small" }, r.steps.map((s) => h("li", {}, h("strong", {}, `${s.label}: `), s.result || s.status))),
    a && h("div", {},
      sec("still running", "info", a.todo.busy.map((id) => ({ shot_id: id })), shotWhy, "Started work that hadn't finished when the report was made."),
      sec("waiting for design picks", "warn", a.todo.picks, shotWhy, "Pick the missing looks, sets or props, then update again."),
      sec("plans you wrote that are out of date", "warn", a.todo.ownPlans, shotWhy),
      sec("plans that failed or have problems", "bad", [...(plans.failed || []), ...(plans.problems || [])], shotWhy),
      sec("pictures still out of date", "bad", pics.left, (it) => [h("strong", {}, it.what), h("span", { class: "muted" }, ` ${it.reason}`)]),
      sec("keyframes that couldn't start", "warn", kf.skipped, shotWhy),
      sec("shots with out-of-date keyframes left", "warn", a.todo.outdatedKf, (it) => [updShotLink(it.shot_id, ` · cut ${it.cuts.join(", ")}`), h("span", { class: "muted" }, ` ${it.reason}`)])),
    a && !a.todo.approve.length && !a.todo.picks.length && !a.todo.outdatedKf.length && !a.todo.ownPlans.length && !a.todo.busy.length
      && h("p", { class: "banner ok small" }, "Everything is up to date and nothing waits on you."),
    updApprovals(r, true));
}

function updRender() {
  const btn = document.getElementById("update-all-btn");
  if (btn) btn.textContent = updLabel(state.pkg);
  const el = upd.el;
  const r = upd.run;
  if (!el || el.hidden || !r) return;
  // Kept across redraws: the scroll position and the focused control (read before the new
  // view is built: building it moves the reused cards out of the old one).
  const scrolled = el.querySelector(".update-body")?.scrollTop || 0;
  const focused = el.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const body = r.phase === "survey" ? h("p", { class: "loading" }, "Looking at the story…")
    : r.phase === "error" ? h("p", { class: "form-error" }, `Couldn't read the story: ${r.error}`)
    : r.phase === "plan" ? updPlanView(r)
    : [updStepsView(r),
       // Cuts drawn so far can be approved while the rest are drawn.
       r.phase === "running" && r.steps.find((s) => s.key === "keyframes").status === "running" && updApprovals(r, true),
       r.phase === "done" && updReport(r), r.error && h("p", { class: "form-error" }, r.error)];
  const anyOn = r.steps && r.steps.some((s) => s.on);
  const buttons = r.phase === "plan"
    ? [h("button", { type: "button", class: "btn", onclick: closeUpdate }, "Cancel"),
       h("button", { type: "button", class: "btn primary update-go", disabled: !anyOn, onclick: updStart }, "Start update")]
    : r.phase === "running"
      ? [h("button", { type: "button", class: "btn", disabled: r.stopped, title: "Start nothing new. What is already drawing finishes on the server.",
           onclick: () => { r.stopped = true; updRender(); } }, r.stopped ? "Stopping…" : "Stop"),
         h("button", { type: "button", class: "btn", onclick: closeUpdate, title: "It keeps running; the header button reopens it" }, "Close (keeps running)")]
      : [r.phase === "done" && h("button", { type: "button", class: "btn", onclick: () => updSurvey(state.pkg && state.pkg.package_id === r.pkg.package_id ? state.pkg : r.pkg) }, "Check again"),
         h("button", { type: "button", class: "btn", onclick: closeUpdate }, "Close")];
  el.replaceChildren(h("div", { class: "update-window", role: "dialog", "aria-modal": "true", "aria-labelledby": "update-title" },
    h("div", { class: "update-title-row" },
      h("h2", { id: "update-title" }, "Update all"),
      h("button", { type: "button", class: "btn ghost small", "aria-label": "Close", onclick: closeUpdate }, "×")),
    h("div", { class: "update-body" }, body),
    h("div", { class: "update-actions" }, buttons)));
  // Reused cards keep their buttons: only whether an action is under way changes.
  for (const b of el.querySelectorAll("[data-act]")) b.disabled = Boolean(r.acting);
  if (focused) el.querySelector(`[data-focus="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
  el.querySelector(".update-body").scrollTop = scrolled;
}

// While the window is open, the elapsed times tick (only those labels change).
setInterval(() => {
  if (!upd.el || upd.el.hidden) return;
  for (const span of upd.el.querySelectorAll("[data-since]")) {
    const ms = (Number(span.dataset.until) || Date.now()) - Number(span.dataset.since);
    span.textContent = "clock" in span.dataset ? updClock(ms) : updMins(ms);
  }
}, 1000);
