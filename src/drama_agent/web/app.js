"use strict";

/*
 * Drama Agent web UI. Plain JS, no build step.
 * All model-generated text is inserted via textContent (see h()), never innerHTML.
 */

// ---------------------------------------------------------------------------
// Helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
  });
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = typeof body === "object" ? body.detail : body;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function fmtMs(ms) {
  const s = ms / 1000;
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

function timeAgo(iso) {
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return new Date(iso).toLocaleString();
}

const STATUS_TONE = {
  queued: "", running: "accent", completed: "ok", needs_revision: "warn",
  failed: "bad", cancelled: "", published: "ok", ready: "ok", draft: "warn", stale: "bad",
};
const statusChip = (s) => h("span", { class: `chip ${STATUS_TONE[s] || ""}` }, s.replace("_", " "));
const isActive = (s) => s === "queued" || s === "running";

// ---------------------------------------------------------------------------
// State + routing (hash: #job=<id>&tab=<tab>)

const state = { jobs: [], job: null, pkg: null, tab: "overview", pollTimer: null, assetFilter: "all", revise: null, offline: false };

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return { job: p.get("job"), tab: p.get("tab") || "overview" };
}

function go(jobId, tab = "overview") {
  const p = new URLSearchParams();
  if (jobId) p.set("job", jobId);
  if (tab && tab !== "overview") p.set("tab", tab);
  location.hash = p.toString();
}

window.addEventListener("hashchange", route);

async function route() {
  const { job, tab } = readHash();
  state.tab = tab;
  clearTimeout(state.pollTimer);
  if (!job) {
    state.job = state.pkg = null;
    renderJobList();
    renderMain();
    return;
  }
  if (state.job?.job_id !== job) {
    state.job = state.pkg = null;
    state.retryError = "";
    state.renderedSig = null;
    state.revise = null;
    renderMain();
  }
  await loadJob(job);
}

// ---------------------------------------------------------------------------
// Data loading

async function loadMeta() {
  try {
    const m = await api("/v1/meta");
    const live = m.client !== "FixtureModelClient";
    state.offline = !live;
    $("#offline-note").hidden = live;
    $("#meta").replaceChildren(
      h("span", { class: `chip ${live ? "ok" : "warn"}` }, live ? "live model" : "offline fixture"),
      " ", h("span", { class: "mono" }, m.model_id),
    );
  } catch {
    $("#meta").textContent = "API unreachable";
  }
}

async function loadJobs() {
  try {
    state.jobs = await api("/v1/drama-jobs");
  } catch {
    state.jobs = [];
  }
  renderJobList();
}

async function loadJob(jobId) {
  // A request for a previous job can still be in flight when the user switches jobs.
  // Its late response must not overwrite the job now on screen or restart its polling.
  const stillCurrent = () => readHash().job === jobId;
  try {
    const job = await api(`/v1/drama-jobs/${encodeURIComponent(jobId)}`);
    if (!stillCurrent()) return;
    const prev = state.job?.job_id === jobId ? state.job : null;
    const changed = !prev || prev.status !== job.status || prev.revision_id !== job.revision_id;
    let pkg = prev ? state.pkg : null;
    if (job.package_id && (changed || !pkg)) {
      const q = new URLSearchParams({ revision_id: job.revision_id || "" });
      pkg = await api(`/v1/drama-packages/${encodeURIComponent(job.package_id)}?${q}`);
      if (!stillCurrent()) return;
    }
    // Polling must not rebuild the page when nothing changed: rebuilding destroys any text
    // the user has selected, which made running jobs impossible to copy from.
    const sig = JSON.stringify(job) + "|" + (pkg?.revision_id || "");
    const unchanged = sig === state.renderedSig;
    state.job = job;
    state.pkg = pkg;
    if (!unchanged) {
      state.renderedSig = sig;
      renderWhenNotSelecting();
    }
    if (changed) loadJobs();
    clearTimeout(state.pollTimer);
    if (isActive(job.status)) state.pollTimer = setTimeout(() => loadJob(jobId), 1500);
  } catch (err) {
    if (!stillCurrent()) return;
    $("#main").replaceChildren(h("div", { class: "card placeholder" }, h("h2", {}, "Couldn't load job"), err.message));
  }
}

// ---------------------------------------------------------------------------
// Sidebar

function renderJobList() {
  const list = $("#job-list");
  const current = readHash().job;
  if (selectingIn(list)) {
    state.jobListPending = true;
    return;
  }
  state.jobListPending = false;
  if (!state.jobs.length) {
    list.replaceChildren(h("li", { class: "empty" }, "No jobs yet."));
    return;
  }
  // Not <button>: browsers don't allow selecting text inside buttons, so seeds couldn't
  // be copied. A click that ends a text selection selects instead of navigating.
  const open = (jobId) => (ev) => {
    if (ev.type === "keydown" && ev.key !== "Enter" && ev.key !== " ") return;
    if (ev.type === "click" && !getSelection().isCollapsed) return;
    ev.preventDefault();
    go(jobId);
  };
  list.replaceChildren(
    ...state.jobs.map((j) =>
      h("li", {},
        h("div", {
          class: `job-item${j.job_id === current ? " active" : ""}`, role: "link", tabindex: "0",
          "aria-current": j.job_id === current ? "page" : null,
          onclick: open(j.job_id), onkeydown: open(j.job_id),
        },
          h("div", { class: "seed" }, j.seed),
          h("div", { class: "meta" }, statusChip(j.status), h("span", { class: "muted small" }, timeAgo(j.created_at))),
        ),
      ),
    ),
  );
}

$("#refresh-jobs").addEventListener("click", loadJobs);

$("#new-job").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const f = new FormData(form);
  const button = form.querySelector("button[type=submit]");
  const err = $("#new-job-error");
  err.textContent = "";
  const type = f.get("type");
  const body = {
    sources: [{ type, text: f.get("text").trim(), use_as: type === "script" ? "plot_reference" : "story_seed" }],
    options: {
      language: f.get("language") || null,
      story_scope: f.get("story_scope"),
      reference_mode: f.get("reference_mode"),
      genre: f.get("genre").trim() || null,
      audience: f.get("audience").trim() || null,
    },
  };
  button.disabled = true;
  try {
    const created = await api("/v1/drama-jobs", { method: "POST", body: JSON.stringify(body) });
    await loadJobs();
    go(created.job_id);
  } catch (e) {
    err.textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Main view

// True while the user has text selected inside `el` (e.g. mid copy).
function selectingIn(el) {
  const sel = getSelection();
  return sel && !sel.isCollapsed && sel.rangeCount > 0 && el.contains(sel.getRangeAt(0).commonAncestorContainer);
}

// Background updates wait until the selection is released; user actions render at once.
function renderWhenNotSelecting() {
  if (selectingIn($("#main"))) {
    state.renderPending = true;
    return;
  }
  state.renderPending = false;
  renderMain();
}

document.addEventListener("selectionchange", () => {
  if (state.renderPending && !selectingIn($("#main"))) renderWhenNotSelecting();
  if (state.jobListPending && !selectingIn($("#job-list"))) renderJobList();
});

function renderMain() {
  const main = $("#main");
  const job = state.job;
  if (!readHash().job) {
    main.replaceChildren(
      h("div", { class: "card placeholder" },
        h("h2", {}, "Turn a seed into a 3-episode short drama"),
        h("p", {}, "Enter a one-line idea or paste a script on the left. Each job produces scripts, a timed shot list and art asset requirements."),
      ),
    );
    return;
  }
  if (!job) {
    main.replaceChildren(h("div", { class: "loading" }, "Loading…"));
    return;
  }
  main.replaceChildren(renderJobHeader(job), state.pkg ? renderPackage(state.pkg) : renderWaiting(job));
}

function renderJobHeader(job) {
  const pkg = state.pkg;
  const title = pkg?.story_bible?.title || job.request.sources[0].text?.slice(0, 60) || job.job_id;
  const actions = [];
  if (pkg) {
    const q = new URLSearchParams({ revision_id: pkg.revision_id });
    const base = `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}`;
    actions.push(
      h("a", { class: "btn small", href: `${base}?${q}&format=markdown`, target: "_blank", rel: "noopener" }, "Markdown"),
      h("a", { class: "btn small", href: `${base}?${q}`, download: `${pkg.package_id}.json` }, "JSON"),
    );
  }
  if (job.status === "needs_revision" || job.status === "failed") {
    const from = Object.entries(job.stages).find(([, s]) => s.status !== "passed")?.[0];
    actions.unshift(
      h("button", { class: "btn small primary-outline", type: "button", onclick: (e) => retryJob(job.job_id, e.currentTarget),
        title: "Run again. Steps that already passed are kept and not re-run." },
        from ? `Retry from ${from}` : "Retry"),
    );
  }

  const banners = [];
  if (state.retryError) {
    banners.push(h("div", { class: "banner bad" }, h("strong", {}, "Couldn't retry. "), state.retryError));
  }
  if (job.status === "queued" && job.failure_reason) {
    banners.push(h("div", { class: "banner warn" }, h("strong", {}, "Queued. "), job.failure_reason));
  }
  if (job.status === "failed") {
    banners.push(h("div", { class: "banner bad" }, h("strong", {}, "Job failed. "), job.failure_reason || ""));
  }
  if (job.status === "needs_revision") {
    const failed = Object.entries(job.stages).find(([, s]) => s.status === "failed");
    banners.push(
      h("div", { class: "banner warn" },
        h("strong", {}, `Needs revision at the ${failed?.[0] || "?"} step. `),
        "Hard checks still failed after the retry budget. The draft below is kept for inspection.",
        failed && h("ul", {}, failed[1].issues.slice(0, 8).map((i) => h("li", {}, h("code", {}, i.object_id), ` ${i.check}: ${i.evidence}`))),
      ),
    );
  }

  return h("section", { class: "card" },
    h("div", { class: "job-head" },
      h("div", {},
        h("h1", {}, title),
        h("div", { class: "chips" }, statusChip(job.status), h("span", { class: "chip mono" }, job.job_id),
          job.attempts > 1 && h("span", { class: "chip warn" }, `attempt ${job.attempts}`),
          pkg && h("span", { class: "chip mono" }, pkg.revision_id),
          pkg && isSampleOutput(pkg) && h("span", { class: "chip warn", title: "Produced by the offline fixture, not a model. The seed was ignored." }, "sample output")),
      ),
      h("div", { class: "job-actions" }, actions),
    ),
    h("div", { class: "stages" },
      Object.entries(job.stages).map(([name, s]) =>
        h("div", { class: `stage ${s.status}` },
          h("div", { class: "name" }, name),
          h("div", { class: "state" }, s.status, s.attempts > 1 ? ` · ${s.attempts} attempts` : ""),
        ),
      ),
    ),
    banners,
  );
}

async function retryJob(jobId, button) {
  button.disabled = true;
  state.retryError = "";
  try {
    await api(`/v1/drama-jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
    state.pkg = null;  // the retry publishes a new revision; drop the old draft from view
    await loadJob(jobId);
  } catch (e) {
    state.retryError = e.message;
    button.disabled = false;
    renderMain();
  }
}

const isSampleOutput = (pkg) => Object.values(pkg.provenance?.model_versions || {}).includes("fixture-v1");

function renderWaiting(job) {
  if (isActive(job.status)) {
    return h("div", { class: "loading" }, "Generating… with a live model each step can take a minute or more.");
  }
  return h("div", { class: "loading" }, "No package was produced.");
}

// ---------------------------------------------------------------------------
// Package tabs

const TABS = [
  ["overview", "Overview"],
  ["script", "Script"],
  ["storyboard", "Storyboard"],
  ["assets", "Assets"],
  ["qa", "QA"],
  ["revise", "Revise"],
];

function lookups(pkg) {
  const b = pkg.story_bible;
  const name = {};
  for (const c of b.characters) name[c.character_id] = c.name;
  for (const l of b.locations) name[l.location_id] = l.name;
  for (const p of b.props) name[p.prop_id] = p.name;
  const lines = {};
  for (const ep of pkg.episodes) for (const sc of ep.scenes) for (const bt of sc.beats) if (bt.dialogue) lines[bt.dialogue.dialogue_id] = bt.dialogue;
  return { name, lines };
}

function renderPackage(pkg) {
  const counts = {
    script: pkg.episodes.length,
    storyboard: pkg.shot_list.length,
    assets: pkg.asset_requirements.items.length,
    qa: pkg.qa_report.hard_issues.length + pkg.qa_report.creative_issues.length,
  };
  const tabs = h("div", { class: "tabs", role: "tablist" },
    TABS.map(([id, label]) =>
      h("button", { class: "tab", role: "tab", type: "button", "aria-selected": String(state.tab === id), onclick: () => go(state.job.job_id, id) },
        label, counts[id] != null && h("span", { class: "count" }, counts[id]),
      ),
    ),
  );
  const L = lookups(pkg);
  const body = {
    overview: renderOverview, script: renderScript, storyboard: renderStoryboard,
    assets: renderAssets, qa: renderQA, revise: renderRevise,
  }[state.tab] || renderOverview;
  return h("div", {}, tabs, h("div", { role: "tabpanel" }, body(pkg, L)));
}

// Overview ------------------------------------------------------------------

function renderOverview(pkg, L) {
  const b = pkg.story_bible;
  const sb = pkg.source_brief;
  const looksBy = groupBy(b.looks, "character_id");
  const statesBy = groupBy(b.location_states, "location_id");
  const propStatesBy = groupBy(b.prop_states, "prop_id");
  return h("div", {},
    h("section", { class: "card hero" },
      h("h1", {}, b.title),
      h("p", { class: "logline" }, b.logline),
      h("dl", { class: "kv" },
        kv("Genre", b.genre), kv("Audience", b.audience_assumption), kv("Emotional goal", b.emotional_goal),
        kv("Core conflict", b.core_conflict), kv("Synopsis", b.series_synopsis),
        b.future_direction && kv("Future direction", b.future_direction),
      ),
    ),
    h("h2", { class: "section-title" }, `Characters (${b.characters.length})`),
    h("div", { class: "grid" },
      b.characters.map((c) =>
        h("div", { class: "card entity" },
          h("h3", {}, c.name, h("code", { class: "muted" }, c.character_id)),
          h("p", {}, c.appearance), h("p", { class: "muted" }, c.costume),
          h("p", {}, h("strong", {}, "Goal: "), c.goal),
          h("p", {}, h("strong", {}, "Motivation: "), c.motivation),
          b.character_arcs?.[c.character_id] && h("p", {}, h("strong", {}, "Arc: "), b.character_arcs[c.character_id]),
          (looksBy[c.character_id] || []).length > 0 && h("div", { class: "variants chips" },
            looksBy[c.character_id].map((lk) => h("span", { class: "chip", title: lk.description }, lk.look_id))),
        ),
      ),
    ),
    h("h2", { class: "section-title" }, `Locations (${b.locations.length})`),
    h("div", { class: "grid" },
      b.locations.map((l) =>
        h("div", { class: "card entity" },
          h("h3", {}, l.name, h("code", { class: "muted" }, l.location_id)),
          h("p", {}, l.layout), h("p", { class: "muted" }, l.furnishings), h("p", { class: "muted" }, l.lighting),
          h("div", { class: "variants chips" }, (statesBy[l.location_id] || []).map((s) => h("span", { class: "chip", title: s.description }, s.location_state_id))),
        ),
      ),
    ),
    b.props.length > 0 && [
      h("h2", { class: "section-title" }, `Key props (${b.props.length})`),
      h("div", { class: "grid" },
        b.props.map((p) =>
          h("div", { class: "card entity" },
            h("h3", {}, p.name, h("code", { class: "muted" }, p.prop_id)),
            h("p", {}, p.description),
            h("div", { class: "variants chips" }, (propStatesBy[p.prop_id] || []).map((s) => h("span", { class: "chip", title: s.description }, s.prop_state_id))),
          ),
        ),
      ),
    ],
    h("h2", { class: "section-title" }, "Source brief"),
    h("div", { class: "grid" },
      listCard("Observed facts", sb.observed_facts),
      listCard("User constraints", sb.user_constraints),
      listCard("Creative assumptions", sb.creative_assumptions),
    ),
  );
}

const kv = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
const listCard = (title, items) =>
  h("div", { class: "card" }, h("h3", {}, title),
    items.length ? h("ul", { class: "plain small" }, items.map((i) => h("li", {}, i))) : h("p", { class: "muted small" }, "None"));

function groupBy(arr, key) {
  const out = {};
  for (const x of arr) (out[x[key]] ||= []).push(x);
  return out;
}

// Script --------------------------------------------------------------------

function renderScript(pkg, L) {
  return pkg.episodes.map((ep) =>
    h("section", { class: "card episode" },
      h("h2", {}, `Episode ${ep.number} · ${ep.title}`),
      h("p", { class: "muted small" }, `Opening: ${ep.opening_event} · Conflict: ${ep.main_conflict} · Ending: ${ep.ending_event}`),
      ep.scenes.map((sc) =>
        h("div", { class: "scene" },
          h("div", { class: "scene-head" },
            h("h3", {}, sc.scene_id),
            h("span", { class: "chip" }, `${sc.int_ext}. ${L.name[sc.location_id] || sc.location_id} · ${sc.time_of_day}`),
            h("span", { class: "chip" }, `${sc.target_duration_ms / 1000}s`),
            h("span", { class: "chips" }, sc.character_ids.map((c) => h("span", { class: "chip info" }, L.name[c] || c))),
          ),
          sc.beats.map((bt) =>
            h("div", { class: "beat" },
              h("div", {},
                bt.action && h("p", { class: "action" }, bt.action),
                bt.dialogue && h("p", { class: "line" },
                  h("span", { class: "who" }, L.name[bt.dialogue.character_id] || bt.dialogue.character_id, bt.dialogue.voice_only ? " (V.O.)" : "", ": "),
                  bt.dialogue.text),
                bt.sound && h("p", { class: "muted small" }, `♪ ${bt.sound}`),
              ),
              h("div", { class: "ids" }, bt.beat_id, bt.dialogue && h("div", {}, bt.dialogue.dialogue_id)),
            ),
          ),
        ),
      ),
    ),
  );
}

// Storyboard ----------------------------------------------------------------

function renderStoryboard(pkg, L) {
  const timing = Object.fromEntries(pkg.timing.map((t) => [t.episode_id, t]));
  return pkg.episodes.map((ep) => {
    const shots = pkg.shot_list.filter((s) => s.episode_id === ep.episode_id).sort((a, b) => a.start_ms - b.start_ms);
    const total = shots.reduce((n, s) => Math.max(n, s.start_ms + s.duration_ms), 0) || 1;
    const sceneIdx = Object.fromEntries(ep.scenes.map((s, i) => [s.scene_id, i % 3]));
    const t = timing[ep.episode_id];
    return h("section", { class: "card episode" },
      h("div", { class: "section-head" },
        h("h2", {}, `Episode ${ep.number} · ${ep.title}`),
        h("div", { class: "chips" },
          h("span", { class: `chip ${t?.planned_timing_valid ? "ok" : "bad"}` }, `planned timing ${t?.planned_timing_valid ? "valid" : "invalid"}`),
          h("span", { class: "chip warn", title: t?.speech_rate_assumption || "" }, `spoken timing ${t?.spoken_timing_verified ? "verified" : "not measured"}`),
        ),
      ),
      h("div", { class: "timeline", role: "img", "aria-label": `Shot timeline, ${fmtMs(total)} total` },
        shots.map((s) =>
          h("div", { class: `seg s${sceneIdx[s.scene_id] ?? 0}`, style: `flex:${s.duration_ms}`, title: `${s.shot_id} ${fmtMs(s.start_ms)}–${fmtMs(s.start_ms + s.duration_ms)}` },
            s.shot_id.split("_").slice(-1)[0]),
        ),
      ),
      h("div", { class: "ruler" }, h("span", {}, "0:00.0"), h("span", {}, fmtMs(total))),
      h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, ["Shot", "Time", "Framing / camera", "Visual action", "Dialogue", "Sound", "Assets"].map((c) => h("th", {}, c)))),
          h("tbody", {},
            shots.map((s) =>
              h("tr", {},
                h("td", { class: "mono" }, s.shot_id, h("div", { class: "muted" }, s.scene_id)),
                h("td", { class: "mono" }, `${fmtMs(s.start_ms)}`, h("div", { class: "muted" }, `${(s.duration_ms / 1000).toFixed(1)}s`)),
                h("td", {}, s.framing, h("div", { class: "muted small" }, s.camera)),
                h("td", {}, s.visual_action,
                  s.character_ids.length > 0 && h("div", { class: "chips", style: "margin-top:4px" }, s.character_ids.map((c) => h("span", { class: "chip info" }, L.name[c] || c)))),
                h("td", {}, s.dialogue_cues.map((c) => {
                  const line = L.lines[c.dialogue_id];
                  return h("div", { class: "small" },
                    h("strong", {}, line ? (L.name[line.character_id] || line.character_id) : c.dialogue_id, ": "),
                    line?.text || "", h("span", { class: "muted mono" }, ` ${fmtMs(c.start_ms)}–${fmtMs(c.end_ms)}`));
                })),
                h("td", { class: "small" }, s.sound),
                h("td", { class: "mono" }, s.asset_requirement_ids.length),
              ),
            ),
          ),
        ),
      ),
    );
  });
}

// Assets --------------------------------------------------------------------

const ASSET_TYPES = ["character_identity", "character_look", "location", "prop", "shot_reference"];

function renderAssets(pkg) {
  const ar = pkg.asset_requirements;
  const items = state.assetFilter === "all" ? ar.items : ar.items.filter((r) => r.asset_type === state.assetFilter);
  const count = (t) => ar.items.filter((r) => r.asset_type === t).length;
  const sb = ar.style_brief;
  return h("div", {},
    h("section", { class: "card" },
      h("h3", {}, "Style brief"),
      h("dl", { class: "kv", style: "margin-top:8px" },
        kv("Medium", sb.medium), kv("Era / region", sb.era_region), kv("Color & light", sb.color_and_light), kv("Aspect ratio", sb.aspect_ratio)),
    ),
    h("div", { class: "filters", style: "margin-top:16px" },
      [["all", `All ${ar.items.length}`], ...ASSET_TYPES.map((t) => [t, `${t.replace("_", " ")} ${count(t)}`])].map(([id, label]) =>
        h("button", { class: "filter", type: "button", "aria-pressed": String(state.assetFilter === id), onclick: () => { state.assetFilter = id; renderMain(); } }, label)),
    ),
    h("div", { class: "grid" },
      items.map((r) =>
        h("div", { class: "card req" },
          h("h3", {}, r.requirement_id),
          h("div", { class: "chips", style: "margin-top:6px" },
            h("span", { class: "chip info" }, r.asset_type), statusChip(r.requirement_status),
            h("span", { class: "chip" }, r.priority),
            h("span", { class: "chip", title: r.used_by.map((u) => u.shot_id).join(", ") }, `${r.used_by.length} shots`)),
          h("p", { class: "brief small" }, r.design_brief),
          h("div", { class: "lists" },
            h("div", {}, h("h4", {}, "Must keep"), bullet(r.must_keep)),
            h("div", {}, h("h4", {}, "Design freedom"), bullet(r.design_freedom)),
            h("div", {}, h("h4", {}, "Deliverables"), bullet(r.deliverables.map((d) => `${d.view} — ${d.purpose}${d.required ? "" : " (optional)"}`))),
            h("div", {}, h("h4", {}, "Depends on"), bullet(r.depends_on, true)),
          ),
        ),
      ),
    ),
  );
}

const bullet = (items, mono = false) =>
  items.length ? h("ul", { class: "plain" }, items.map((i) => h("li", { class: mono ? "mono" : "" }, i))) : h("span", { class: "muted" }, "—");

// QA ------------------------------------------------------------------------

function renderQA(pkg) {
  const qa = pkg.qa_report;
  const prov = pkg.provenance;
  const usage = Object.entries(prov.token_usage || {});
  const totals = usage.reduce((t, [, u]) => ({ p: t.p + (u.prompt_tokens || 0), c: t.c + (u.completion_tokens || 0) }), { p: 0, c: 0 });
  return h("div", { class: "stack" },
    issuesCard("Hard check issues", qa.hard_issues, "All hard checks passed."),
    issuesCard("Creative review notes", qa.creative_issues, "No creative notes."),
    h("section", { class: "card" },
      h("h3", {}, "Provenance"),
      h("dl", { class: "kv", style: "margin-top:8px" },
        kv("Models", Object.entries(prov.model_versions).map(([s, m]) => `${s}: ${m}`).join(" · ") || "—"),
        kv("Prompts", Object.entries(prov.prompt_versions).map(([s, v]) => `${s}: ${v}`).join(" · ") || "—"),
        kv("Created", new Date(prov.created_at).toLocaleString())),
      usage.length > 0 && h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Stage"), h("th", {}, "Prompt tokens"), h("th", {}, "Completion tokens"))),
          h("tbody", {},
            usage.map(([s, u]) => h("tr", {}, h("td", {}, s), h("td", { class: "mono" }, u.prompt_tokens ?? 0), h("td", { class: "mono" }, u.completion_tokens ?? 0))),
            h("tr", {}, h("td", {}, h("strong", {}, "Total")), h("td", { class: "mono" }, totals.p), h("td", { class: "mono" }, totals.c)),
          ),
        ),
      ),
    ),
  );
}

function issuesCard(title, issues, emptyText) {
  return h("section", { class: "card" },
    h("h3", {}, `${title} (${issues.length})`),
    issues.length === 0
      ? h("p", { class: "muted small" }, emptyText)
      : h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, ["Check", "Object", "Evidence", "Severity", "Fix scope"].map((c) => h("th", {}, c)))),
          h("tbody", {}, issues.map((i) => h("tr", {},
            h("td", {}, i.check), h("td", { class: "mono" }, i.object_id), h("td", {}, i.evidence),
            h("td", {}, statusChipSeverity(i.severity)), h("td", { class: "small" }, i.fix_scope || "—")))),
        )));
}

const statusChipSeverity = (s) => h("span", { class: `chip ${s === "blocker" ? "bad" : s === "major" ? "warn" : ""}` }, s);

// Revise --------------------------------------------------------------------

function allIds(pkg) {
  const b = pkg.story_bible;
  const ids = [];
  const add = (id, label) => ids.push([id, label]);
  b.characters.forEach((c) => add(c.character_id, `character · ${c.name}`));
  b.looks.forEach((x) => add(x.look_id, `look · ${x.description}`));
  b.locations.forEach((x) => add(x.location_id, `location · ${x.name}`));
  b.location_states.forEach((x) => add(x.location_state_id, `location state · ${x.description}`));
  b.props.forEach((x) => add(x.prop_id, `prop · ${x.name}`));
  b.prop_states.forEach((x) => add(x.prop_state_id, `prop state · ${x.description}`));
  for (const ep of pkg.episodes) {
    add(ep.episode_id, `episode · ${ep.title}`);
    for (const sc of ep.scenes) {
      add(sc.scene_id, `scene · ${sc.location_id}`);
      for (const bt of sc.beats) if (bt.dialogue) add(bt.dialogue.dialogue_id, `dialogue · ${bt.dialogue.text.slice(0, 30)}`);
    }
  }
  pkg.shot_list.forEach((s) => add(s.shot_id, `shot · ${s.visual_action.slice(0, 30)}`));
  pkg.asset_requirements.items.forEach((r) => add(r.requirement_id, `asset · ${r.asset_type}`));
  return ids;
}

function renderRevise(pkg) {
  const r = (state.revise ||= { targets: [], locked: [...pkg.locked_ids], instruction: "", visual: false, result: null, error: "" });
  const ids = allIds(pkg);
  const known = new Set(ids.map(([id]) => id));

  const picker = (key, label) => {
    const input = h("input", { list: "all-ids", placeholder: "Type or pick an ID", "aria-label": label });
    const add = () => {
      const v = input.value.trim();
      if (v && known.has(v) && !r[key].includes(v)) { r[key].push(v); renderMain(); }
      else input.setCustomValidity(known.has(v) ? "" : "Unknown ID"), input.reportValidity();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
    return h("div", { class: "field" },
      h("span", {}, label),
      h("div", { class: "idpicker" }, input, h("button", { class: "btn", type: "button", onclick: add }, "Add")),
      h("div", { class: "selected" },
        r[key].length === 0 ? h("span", { class: "muted small" }, "None") :
          r[key].map((id) => h("span", { class: "chip info mono" }, id,
            h("button", { type: "button", "aria-label": `Remove ${id}`, onclick: () => { r[key] = r[key].filter((x) => x !== id); renderMain(); } }, "×")))),
    );
  };

  const submit = async (ev) => {
    ev.preventDefault();
    r.error = "";
    r.result = null;
    if (!r.targets.length) { r.error = "Pick at least one target."; renderMain(); return; }
    try {
      r.result = await api(`/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/revisions`, {
        method: "POST",
        body: JSON.stringify({
          base_revision_id: pkg.revision_id, target_ids: r.targets, instruction: r.instruction,
          locked_ids: r.locked, visual_change: r.visual,
        }),
      });
    } catch (e) {
      r.error = e.message;
    }
    renderMain();
  };

  const instruction = h("textarea", { rows: 3, required: true, placeholder: "第二集父亲的动机更可信，但不要提前揭示他的身份", oninput: (e) => { r.instruction = e.target.value; } });
  instruction.value = r.instruction;
  const visual = h("input", { type: "checkbox", onchange: (e) => { r.visual = e.target.checked; } });
  visual.checked = r.visual;

  return h("div", { class: "stack" },
    h("form", { class: "card", onsubmit: submit },
      h("h3", { style: "margin-bottom:10px" }, "Request a targeted revision"),
      h("p", { class: "hint" }, `Based on revision ${pkg.revision_id}. The server runs the impact analysis now. Regenerating the content isn't implemented yet.`),
      h("datalist", { id: "all-ids" }, ids.map(([id, label]) => h("option", { value: id }, label))),
      picker("targets", "Targets: what to change"),
      picker("locked", "Locked: must not change"),
      h("label", { class: "field" }, h("span", {}, "Instruction"), instruction),
      h("label", { class: "check" }, visual, "This changes how a character, location or prop looks"),
      h("button", { class: "btn primary", type: "submit" }, "Analyze impact"),
      r.error && h("p", { class: "form-error" }, r.error),
    ),
    r.result && renderRevisionResult(r.result),
  );
}

function renderRevisionResult(rev) {
  const group = (title, items, tone) =>
    h("div", {}, h("h4", { class: "small muted", style: "margin:10px 0 4px" }, `${title} (${items.length})`),
      items.length ? h("div", { class: "chips" }, items.map((i) => h("span", { class: `chip mono ${tone}` }, i))) : h("span", { class: "muted small" }, "None"));
  return h("section", { class: "card result" },
    h("div", { class: "section-head" }, h("h3", {}, `Revision ${rev.revision_id}`), statusChip(rev.status)),
    group("Conflicts", rev.conflicts, "bad"),
    group("Locked objects in scope (must stay unchanged)", rev.locked_in_scope, "warn"),
    group("Impacted objects", rev.impacted_ids, "info"),
    group("Stale asset requirements", rev.stale_requirement_ids, "bad"),
  );
}

// ---------------------------------------------------------------------------
// Boot

loadMeta();
loadJobs().then(route);
