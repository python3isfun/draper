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
    const drawn = sub.takes.some((t) => t.results.some((d) => d.status === "done"));
    if (drawn) out.approve.push({ shot_id: s.shot_id, cut: sub.index, of: s.sub_shots.length });
    else if (sub.index === 1) {
      out.first.push(s.shot_id);
      if (s.plan.source === "single" && s.duration_ms > 0) out.unplanned += 1;
    } else out.next.push({ shot_id: s.shot_id, cut: sub.index });
  }
  return out;
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
  while (shots.size) {
    if (r.stopped) return;
    await updSleep(UPD_POLL_MS);
    const board = await getBoard(pkg);
    const running = board.shots.filter((s) => shots.has(s.shot_id) && s.busy).length;
    step.done = shots.size - running;
    step.detail = running ? `${step.done} of ${shots.size} shots finished; ${running} drawing and checking` : `${shots.size} of ${shots.size} shots finished`;
    updRender();
    if (!running) break;
  }
  step.result = shots.size
    ? `Keyframes drawn for ${updPlural(shots.size, "shot")}${step.skipped.length ? `; ${step.skipped.length} couldn't start` : ""}.`
    : step.skipped.length ? `Nothing could start (${step.skipped.length} shots, see below).` : "No keyframe needed drawing.";
}

const UPD_RUN = { pictures: updPictures, plans: updPlans, keyframes: updKeyframes };

// ---------------------------------------------------------------------------------------
// Flow: survey -> confirm -> run -> report

async function updSurvey(pkg) {
  // The job the story belongs to: the report's shot links open it.
  const jobId = upd.run && upd.run.pkg.package_id === pkg.package_id ? upd.run.jobId : state.job && state.job.job_id;
  const r = { pkg: { package_id: pkg.package_id, revision_id: pkg.revision_id }, jobId, phase: "survey",
    draws: frames.draws, rounds: frames.rounds, provider: frames.provider, error: "" };
  upd.run = r;
  updRender();
  try {
    const [images, board] = await Promise.all([getImages(pkg), getBoard(pkg)]);
    if (upd.run !== r) return;
    Object.assign(r, { images, board, plan: updClassify(board), phase: "plan" });
    const pics = Object.keys(images.outdated || {}).length;
    const kf = r.plan.first.length + r.plan.next.length + r.plan.redraw.length;
    r.steps = UPD_STEPS.map((s) => ({ ...s, status: "waiting",
      on: s.key === "pictures" ? pics > 0 || images.queued > 0 : s.key === "plans" ? r.plan.replan.length > 0 : kf > 0 || r.plan.replan.length > 0 || pics > 0 }));
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
// The window

function openUpdate() {
  const pkg = state.pkg;
  if (!pkg) return;
  if (!upd.el) {
    upd.el = h("div", { class: "update-backdrop", onclick: (e) => { if (e.target === upd.el) closeUpdate(); } });
    document.body.append(upd.el);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && upd.el && !upd.el.hidden) closeUpdate(); });
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

const updShotLink = (id, extra = "") => h("a", { href: "#", class: "update-shot", onclick: (e) => {
  e.preventDefault();
  closeUpdate();
  go(upd.run.jobId, "keyframes");
  setTimeout(() => document.getElementById(`kf-${id}`)?.scrollIntoView({ block: "start" }), 600);
} }, id, extra);

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
        h("input", { type: "checkbox", checked: s.on, onchange: (e) => { s.on = e.target.checked; updRender(); } }),
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
    h("p", { class: "small muted" }, "Nothing is approved for you. At the end, a report lists the keyframes waiting for your approval. You can close this window while it runs; reloading the page stops the steps that haven't started."),
  ];
}

const UPD_STATUS = { waiting: ["", "waiting"], running: ["info", "running"], done: ["ok", "done"], failed: ["bad", "failed"], skipped: ["", "skipped"], stopped: ["warn", "stopped"] };
const updMins = (ms) => (ms < 60000 ? `${Math.max(1, Math.round(ms / 1000))} s` : `${Math.round(ms / 60000)} min`);

function updStepsView(r) {
  return h("ol", { class: "plain update-steps" }, r.steps.map((s, i) => {
    const [cls, word] = UPD_STATUS[s.status];
    const pct = s.status === "running" && s.total ? Math.round((100 * (s.done || 0)) / s.total) : null;
    return h("li", { class: `update-step ${s.status}` },
      h("div", { class: "update-step-head" },
        h("span", { class: `chip ${cls}` }, word),
        h("strong", {}, `${i + 1}. ${s.label}`),
        s.started && h("span", { class: "small muted" }, updMins((s.ended || Date.now()) - s.started))),
      s.status === "running" && h("div", { class: "update-bar", role: "progressbar", "aria-valuenow": pct ?? undefined },
        h("div", { class: pct == null ? "indeterminate" : "", style: pct == null ? "" : `width:${pct}%` })),
      s.detail && s.status === "running" && h("p", { class: "small muted" }, s.detail),
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
      sec("waiting for your approval", "info", a.todo.approve, (it) => [updShotLink(it.shot_id, ` · cut ${it.cut} of ${it.of}`)],
        "Pick a draw on each. Approving a cut starts the next one; press Update all again afterwards to draw anything else that's left."),
      sec("still running", "info", a.todo.busy.map((id) => ({ shot_id: id })), shotWhy, "Started work that hadn't finished when the report was made."),
      sec("waiting for design picks", "warn", a.todo.picks, shotWhy, "Pick the missing looks, sets or props, then update again."),
      sec("plans you wrote that are out of date", "warn", a.todo.ownPlans, shotWhy),
      sec("plans that failed or have problems", "bad", [...(plans.failed || []), ...(plans.problems || [])], shotWhy),
      sec("pictures still out of date", "bad", pics.left, (it) => [h("strong", {}, it.what), h("span", { class: "muted" }, ` ${it.reason}`)]),
      sec("keyframes that couldn't start", "warn", kf.skipped, shotWhy),
      sec("shots with out-of-date keyframes left", "warn", a.todo.outdatedKf, (it) => [updShotLink(it.shot_id, ` · cut ${it.cuts.join(", ")}`), h("span", { class: "muted" }, ` ${it.reason}`)])),
    a && !a.todo.approve.length && !a.todo.picks.length && !a.todo.outdatedKf.length && !a.todo.ownPlans.length && !a.todo.busy.length
      && h("p", { class: "banner ok small" }, "Everything is up to date and nothing waits on you."));
}

function updRender() {
  const btn = document.getElementById("update-all-btn");
  if (btn) btn.textContent = updLabel(state.pkg);
  const el = upd.el;
  const r = upd.run;
  if (!el || el.hidden || !r) return;
  const body = r.phase === "survey" ? h("p", { class: "loading" }, "Looking at the story…")
    : r.phase === "error" ? h("p", { class: "form-error" }, `Couldn't read the story: ${r.error}`)
    : r.phase === "plan" ? updPlanView(r)
    : [updStepsView(r), r.phase === "done" && updReport(r), r.error && h("p", { class: "form-error" }, r.error)];
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
}

// While a step runs, the elapsed times tick.
setInterval(() => { if (updBusy() && upd.el && !upd.el.hidden) updRender(); }, 1000);
