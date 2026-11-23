// The Episode tab: one button that takes an episode from its storyboard to its clips and a
// rough assembly (shots/episode.py), with the plan, the progress and the holds in view.
// Uses h(), api(), go() from app.js and secs(), drawUrl(), framesOpenShot(), numberSelect(),
// providerName(), stickyDetails() from shots.js.

const ep = { key: null, episodes: null, episode: null, run: null, error: "", timer: null, loading: false,
  settings: { draws: 2, max_rounds: 3, provider: null, holds: [], render: true, assemble: true, parallel: 2, approve_failed: true, fast: false },
  skip: new Set(), busy: false, events: false, prompts: {}, edit: {}, renderOpts: {} };

// Whether the page should keep polling: the run goes on, or a prompt or a take is in work.
const epLive = (run) => !!run && (run.status === "running"
  || run.shots.some((r) => r.prompt.state === "writing" || (r.clips || []).some((c) => c.state === "rendering")));

const epKey = (pkg) => `${pkg.package_id}|${pkg.revision_id}`;
const epBase = (pkg) => `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/episodes`;
const epShown = (key) => state.tab === "episode" && state.pkg && epKey(state.pkg) === key;
const clipUrl = (clipId, v = "content") => `/v1/shot-clips/${encodeURIComponent(clipId)}/${v}`;

function epDuration(s) {
  if (s == null) return "";
  s = Math.round(s);
  if (s < 90) return `${s} s`;
  const m = Math.floor(s / 60);
  return m < 90 ? `${m} min ${String(s % 60).padStart(2, "0")} s` : `${(m / 60).toFixed(1)} h`;
}

async function loadEpisode(pkg, { force = false } = {}) {
  const key = epKey(pkg);
  if (ep.key !== key) Object.assign(ep, { key, episodes: null, episode: null, run: null, error: "" });
  ep.pkg = { package_id: pkg.package_id, revision_id: pkg.revision_id };
  clearTimeout(ep.timer);
  ep.timer = null;
  ep.loading = true;
  try {
    const q = new URLSearchParams({ revision_id: pkg.revision_id });
    const episodes = await api(`${epBase(pkg)}?${q}`);
    if (ep.key !== key) return;
    const first = ep.episodes === null;
    ep.episodes = episodes;
    if (!ep.episode || !episodes.some((e) => e.episode_id === ep.episode)) {
      // The episode with a run going, else the first.
      const live = episodes.find((e) => e.run && e.run.status === "running");
      ep.episode = (live || episodes[0] || {}).episode_id || null;
    }
    const chosen = episodes.find((e) => e.episode_id === ep.episode);
    let run = null;
    if (chosen && chosen.run) {
      run = await api(`${epBase(pkg)}/${encodeURIComponent(ep.episode)}/run?${q}`);
    }
    if (ep.key !== key) return;
    const changed = force || first || JSON.stringify(run) !== JSON.stringify(ep.run);
    ep.run = run;
    ep.error = "";
    if (changed && epShown(key)) renderWhenNotSelecting();
  } catch (e) {
    if (ep.key !== key) return;
    ep.error = e.message;
    if (epShown(key)) renderWhenNotSelecting();
  } finally {
    ep.loading = false;
  }
  if (epLive(ep.run) && epShown(key)) {
    ep.timer = setTimeout(() => loadEpisode(ep.pkg), 2000);
  }
}

async function epAction(url, button, body, method = "POST") {
  if (button) button.disabled = true;
  ep.busy = true;
  try {
    const out = await api(url, { method, body: body === undefined ? undefined : JSON.stringify(body) });
    ep.error = "";
    await loadEpisode(ep.pkg, { force: true });
    return out;
  } catch (e) {
    ep.error = e.message;
    renderMain();
    return null;
  } finally {
    ep.busy = false;
    if (button) button.disabled = false;
  }
}

const STAGE = {
  pictures: ["", "design pictures"], planning: ["info", "planning cuts"], keyframes: ["info", "keyframes"],
  prompt: ["info", "video prompt"], render: ["info", "clip"], rendering: ["accent", "rendering"],
  ready: ["ok", "ready"], done: ["ok", "done"], waiting: ["warn", "waiting"], skipped: ["", "skipped"],
};
function stageChip(row) {
  const [kind, label] = STAGE[row.stage] || ["", row.stage];
  return h("span", { class: `chip ${kind}` }, label);
}

function renderEpisodeControls(pkg) {
  const s = ep.settings;
  const run = ep.run;
  const running = run && run.status === "running";
  const chosen = (ep.episodes || []).find((e) => e.episode_id === ep.episode);
  const providers = (frames.view && frames.view.providers) || {};
  const toggle = (name) => (e) => {
    s.holds = s.holds.filter((x) => x !== name);
    if (e.target.checked) s.holds.push(name);
  };
  return h("section", { class: "card" },
    h("h3", {}, "Generate episode"),
    h("p", { class: "small muted" },
      "One button for the whole episode: the design pictures if any are missing, each shot's cut plan, a keyframe per cut (the best draw approved), each shot's video prompt, its clip, and the clips cut together in order. Everything a run does shows on the Keyframes tab too, where you can change it while the run waits or goes on: the run reads the board as it is at every step. It holds where you ask it to, and where a person is needed (a cut that failed twice, a prompt that fails a check)."),
    h("div", { class: "design-row" },
      h("label", {}, "Episode ",
        h("select", { "aria-label": "Episode", disabled: running, onchange: (e) => { ep.episode = e.target.value; ep.run = null; loadEpisode(pkg, { force: true }); } },
          (ep.episodes || []).map((e) => h("option", { value: e.episode_id, selected: e.episode_id === ep.episode },
            `Episode ${e.number}: ${e.title} (${e.shots} shots)`)))),
      h("label", {}, " Draws ", numberSelect(s.draws, 4, (n) => { s.draws = n; }, "Draws per round")),
      h("label", {}, " Rounds ", numberSelect(s.max_rounds, 3, (n) => { s.max_rounds = n; }, "Automatic rounds")),
      h("label", {}, " At once ", numberSelect(s.parallel, 6, (n) => { s.parallel = n; }, "Shots whose keyframes are made at the same time")),
      Object.keys(providers).length > 1 && h("label", {}, " Drawn with ",
        h("select", { "aria-label": "Image provider", onchange: (e) => { s.provider = e.target.value || null; } },
          Object.entries(providers).map(([name, model]) =>
            h("option", { value: name, title: model, selected: (s.provider || frames.view.default_provider) === name }, providerName(name)))))),
    h("div", { class: "design-row small" },
      h("label", { title: "The run draws every cut's keyframe, then waits for you to approve one draw (Keyframes tab) before it goes on with that shot. Off: the best-scoring draw is approved by the run." },
        h("input", { type: "checkbox", checked: s.holds.includes("approve"), onchange: toggle("approve") }), " Approve keyframes myself"),
      h("label", { title: "Approve a cut's best draw even when no draw passed the check. Off: the run waits for you there." },
        h("input", { type: "checkbox", checked: s.approve_failed, onchange: (e) => { s.approve_failed = e.target.checked; } }), " Approve a failed check's best draw"),
      h("label", { title: "Wait before the first clip is rendered (video renders are the slow, paid part)" },
        h("input", { type: "checkbox", checked: s.holds.includes("render"), onchange: toggle("render") }), " Hold before rendering"),
      h("label", { title: "Wait before the clips are cut together" },
        h("input", { type: "checkbox", checked: s.holds.includes("assemble"), onchange: toggle("assemble") }), " Hold before assembling"),
      h("label", { title: "Off: stop at the video prompts (the shot specs); render nothing" },
        h("input", { type: "checkbox", checked: s.render, onchange: (e) => { s.render = e.target.checked; } }), " Render clips"),
      h("label", { title: "Off: leave the clips as they are; don't cut the episode together" },
        h("input", { type: "checkbox", checked: s.assemble, onchange: (e) => { s.assemble = e.target.checked; } }), " Assemble the episode"),
      h("label", { title: "Render faster and rougher: a lower resolution (512) and only the cuts' keyframes as reference pictures. Trades likeness and detail stability for speed." },
        h("input", { type: "checkbox", checked: s.fast, onchange: (e) => { s.fast = e.target.checked; } }), " Fast (low res, keyframes only)")),
    h("div", { class: "design-row" },
      h("button", { type: "button", class: "btn primary", disabled: running || !chosen || ep.busy, onclick: async (e) => {
        const body = { revision_id: pkg.revision_id, ...s, skip_shots: [...ep.skip] };
        await epAction(`${epBase(pkg)}/${encodeURIComponent(ep.episode)}/run`, e.currentTarget, body);
      } }, running ? "Running…" : run ? "Run again" : "Generate episode"),
      ep.skip.size > 0 && h("span", { class: "small muted" }, `${ep.skip.size} shot${ep.skip.size > 1 ? "s" : ""} skipped`),
      h("span", { class: "small muted" }, "A run again starts from what is there: approved keyframes, current prompts and clips are kept.")),
    ep.error && h("p", { class: "small warn-text", role: "alert" }, ep.error),
  );
}

function renderRunHead(pkg, run) {
  const c = run.counts;
  const est = run.estimate || {};
  const tone = { running: "accent", paused: "warn", done: "ok", failed: "bad" }[run.status] || "";
  const holds = Object.keys(run.holds || {});
  const base = `/v1/episode-runs/${encodeURIComponent(run.run_id)}`;
  return h("section", { class: "card" },
    h("div", { class: "section-head" },
      h("h3", {}, "Run ", h("span", { class: "muted small mono" }, run.run_id)),
      h("span", { class: "chips" },
        h("span", { class: `chip ${tone}` }, run.status === "running" && holds.length && !c.holds ? "running" : run.status === "running" && holds.length ? "waiting for you" : run.status),
        h("span", { class: `chip ${c.cuts_approved === c.cuts ? "ok" : ""}` }, `${c.cuts_approved}/${c.cuts} keyframes`),
        h("span", { class: `chip ${c.prompts_current === c.shots ? "ok" : ""}` }, `${c.prompts_current}/${c.shots} prompts`),
        run.settings.render && h("span", { class: `chip ${c.clips_done === c.shots ? "ok" : ""}` }, `${c.clips_done}/${c.shots} clips`),
        holds.length > 0 && h("span", { class: "chip warn" }, `${holds.length} hold${holds.length > 1 ? "s" : ""}`))),
    h("p", { class: "small muted" },
      `Elapsed ${epDuration(est.elapsed_s)}`,
      est.per_cut_s != null && ` · about ${epDuration(est.per_cut_s)} a keyframe`,
      est.per_clip_s != null && ` · about ${epDuration(est.per_clip_s)} a clip`,
      run.status === "running" && est.remaining_s != null && ` · about ${epDuration(est.remaining_s)} left (${est.cuts_left} keyframes, ${est.clips_left} clips)`,
      run.status === "running" && est.remaining_s == null && " · estimate once the first keyframe and clip are done"),
    run.error && h("p", { class: "small warn-text" }, run.error),
    h("div", { class: "design-row" },
      run.status === "running" && h("button", { type: "button", class: "btn", title: "What is already running finishes; nothing new starts", onclick: (e) => epAction(`${base}/pause`, e.currentTarget, {}) }, "Pause"),
      run.status === "running" && h("button", { type: "button", class: "btn", title: "Pause the running takes after their current step and drop the clip being rendered", onclick: (e) => epAction(`${base}/pause?now=true`, e.currentTarget, {}) }, "Stop now"),
      (run.status === "paused" || run.status === "failed") && h("button", { type: "button", class: "btn primary", onclick: (e) => epAction(`${base}/resume`, e.currentTarget, {}) }, "Resume"),
      holds.includes("render") && h("button", { type: "button", class: "btn primary", onclick: (e) => epAction(`${base}/release`, e.currentTarget, { key: "render" }) }, "Start rendering"),
      holds.includes("assemble") && h("button", { type: "button", class: "btn primary", onclick: (e) => epAction(`${base}/release`, e.currentTarget, { key: "assemble" }) }, "Assemble the episode"),
      run.settings.holds.includes("approve") && h("button", { type: "button", class: "btn", title: "Let the run approve the best draw of every cut from here on", onclick: (e) => epAction(`${base}/release`, e.currentTarget, { key: "approve" }) }, "Approve the rest for me"),
      run.status !== "running" && !run.settings.render && h("span", { class: "small muted" }, "Clips were not rendered (setting). ")),
    run.output && run.output.file && h("div", { class: "ep-output" },
      h("video", { controls: true, preload: "metadata", src: `${base}/output`, poster: `${base}/poster`, class: "ep-video" }),
      h("p", { class: "small" },
        h("a", { href: `${base}/output`, download: `${pkg.package_id}-${run.episode_id}.mp4` }, "Download the episode"),
        ` · ${epDuration(run.output.duration_s)} · ${run.output.width}x${run.output.height} · ${run.output.clips} clips`)),
  );
}

// The release buttons for a shot's holds whose kind passes `test`, so each hold shows on
// the step it belongs to (a cut hold on Keyframes, a prompt hold on Prompt, and so on).
function stepHolds(run, row, test) {
  const base = `/v1/episode-runs/${encodeURIComponent(run.run_id)}`;
  return Object.entries(row.holds || {}).filter(([key]) => test(key.split(":")[1] || "")).map(([key, why]) => {
    const kind = key.split(":")[1] || "";
    let action = null;
    if (/^cut\d+$/.test(kind) && /approval/.test(why)) {
      action = h("button", { type: "button", class: "btn small", title: "Approve a draw on the Keyframes tab, or let the run approve this shot's best draws", onclick: (e) => epAction(`${base}/release`, e.currentTarget, { key: `${row.shot_id}:approve` }) }, "Approve for me");
    } else if (/^cut\d+$/.test(kind)) {
      action = h("button", { type: "button", class: "btn small", onclick: (e) => epAction(`${base}/release`, e.currentTarget, { key }) }, "Try again");
    } else if (kind === "prompt" || kind === "clip") {
      action = h("button", { type: "button", class: "btn small", title: kind === "prompt" ? "Go on with this prompt as it is" : "Render it again", onclick: (e) => epAction(`${base}/release`, e.currentTarget, { key }) }, kind === "prompt" ? "Use it anyway" : "Try again");
    }
    return h("div", { class: "ep-hold small" }, h("span", { class: "warn-text" }, why), " ", action);
  });
}

// Deep-link to the Keyframes tab, scrolled to this shot, where every step can be edited by
// hand (cut plan, each cut's draws, the video prompt).
function openOnBoard(shotId) {
  go(state.job.job_id, "keyframes");
  setTimeout(() => framesOpenShot(shotId), 50);
}

// One step of a shot's pipeline: a status dot, a name, a one-line state, its own controls,
// and an optional body (thumbnails, a clip player). `state` is one of done|active|wait|hold|fail|idle.
function epStep(name, state, label, controls, body) {
  return h("div", { class: `ep-step ep-step-${state}` },
    h("div", { class: "ep-step-dot", "aria-hidden": "true" }),
    h("div", { class: "ep-step-main" },
      h("div", { class: "ep-step-head" },
        h("span", { class: "ep-step-name" }, name),
        h("span", { class: "ep-step-state small muted" }, label),
        controls && controls.length ? h("span", { class: "ep-step-controls" }, controls) : null),
      body || null));
}

function renderShotSteps(pkg, run, row) {
  const held = (kind) => Object.keys(row.holds || {}).some((k) => (k.split(":")[1] || "") === kind);
  const steps = [];

  // 1. Cut plan.
  const planning = row.stage === "planning" || row.stage === "pictures";
  steps.push(epStep("Cuts",
    row.cuts > 0 ? "done" : planning ? "active" : "idle",
    row.cuts > 0 ? `${row.cuts} cut${row.cuts > 1 ? "s" : ""} planned` : planning ? "planning…" : "not planned yet",
    [h("button", { type: "button", class: "btn ghost small", title: "Change the cut plan on the Keyframes tab", onclick: () => openOnBoard(row.shot_id) }, "Edit cuts")]));

  // 2. Keyframes, one per cut.
  const cutHeld = row.keyframes.some((k) => held(`cut${k.index}`));
  const kfState = row.cuts > 0 && row.approved === row.cuts ? "done" : cutHeld ? "hold" : row.keyframes.some((k) => k.busy) || row.approved > 0 ? "active" : "idle";
  steps.push(epStep("Keyframes", kfState, `${row.approved}/${row.cuts} approved`,
    [h("button", { type: "button", class: "btn ghost small", title: "Draw, approve or replace a cut's keyframe on the Keyframes tab", onclick: () => openOnBoard(row.shot_id) }, "Edit keyframes")],
    h("div", {},
      h("div", { class: "ep-kf" }, row.keyframes.map((k) =>
        k.take_id != null && k.n != null
          ? h("img", { src: drawUrl({ take_id: k.take_id }, k.n), alt: `cut ${k.index}`, title: `cut ${k.index}: approved`, class: "ep-thumb" })
          : h("span", { class: `ep-slot ${k.busy ? "busy" : ""}`, title: k.busy ? `cut ${k.index}: drawing` : `cut ${k.index}: no keyframe yet` }, k.index))),
      stepHolds(run, row, (kind) => /^cut\d+$/.test(kind)))));

  // 3. Video prompt.
  const vp = row.prompt;
  const vpUrl = `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/keyframes/${encodeURIComponent(row.shot_id)}/video-prompt`;
  const vpState = vp.state === "writing" ? "active" : vp.state === "failed" ? "fail" : held("prompt") ? "hold" : vp.state === "current" ? "done" : vp.state === "outdated" ? "active" : "idle";
  const editing = ep.edit[row.shot_id];
  const canEdit = (vp.blocks || []).length > 0 && vp.state !== "writing";
  steps.push(epStep("Prompt", vpState,
    `${vp.problems.length ? `${vp.state} · ${vp.problems.join("; ")}` : vp.state}${vp.source === "user" ? " · edited by hand" : ""}`,
    [
      vp.state !== "writing" && h("button", { type: "button", class: "btn small", title: "Write this shot's video prompt again from its keyframes", onclick: (e) => epAction(vpUrl, e.currentTarget, { revision_id: pkg.revision_id }) }, "Rewrite"),
      canEdit && !editing && h("button", { type: "button", class: "btn small", title: "Edit the prompt's text here", onclick: () => { ep.edit[row.shot_id] = { blocks: vp.blocks.map((b) => ({ ...b })), sound: vp.sound || "", error: "" }; renderMain(); } }, "Edit"),
      h("button", { type: "button", class: "btn ghost small", title: "See the prompt with its pictures on the Keyframes tab", onclick: () => openOnBoard(row.shot_id) }, "On the board"),
    ],
    h("div", {},
      vp.error && h("div", { class: "small warn-text" }, vp.error),
      editing ? renderPromptEditor(pkg, run, row, vpUrl) : (vp.blocks || []).length > 0 && h("details", { class: "small ep-prompt" },
        h("summary", { class: "muted" }, "Prompt text"),
        vp.blocks.map((b) => h("p", { class: "vp-text" }, h("strong", {}, vp.blocks.length > 1 ? `[Shot ${b.sub_shot}] ` : ""), vpText(b.text, vp.pictures || {}), h("span", { class: "muted" }, ` · camera: ${b.camera}`))),
        vp.sound && h("p", { class: "vp-text" }, h("strong", {}, "Sound: "), vp.sound)),
      stepHolds(run, row, (kind) => kind === "prompt"))));

  // 4. Clip render: every take beside the others, the newest finished one in the episode.
  if (run.settings.render) {
    const clip = row.clip || {};
    const takes = row.clips || [];
    const busy = takes.filter((c) => c.state === "rendering").length;
    const clipState = { current: "done", rendering: "active", failed: "fail", outdated: "active" }[clip.state] || (held("clip") ? "hold" : "idle");
    const canRender = vp.state === "current" && !vp.problems.length && busy < MAX_TAKES_IN_FLIGHT;
    const label = busy ? `rendering ${busy} take${busy > 1 ? "s" : ""}${takes.length > busy ? ` · ${takes.length - busy} done` : ""}`
      : clip.state && clip.state !== "none" ? `${clip.state}${clip.duration_s ? ` · ${clip.duration_s} s` : ""}${takes.length > 1 ? ` · ${takes.length} takes` : ""}` : "no clip yet";
    steps.push(epStep("Clip", clipState, label,
      canRender ? renderOptions(pkg, run, row, takes.length > 0) : [],
      h("div", {},
        takes.length > 0 && h("div", { class: "ep-takes" }, takes.map((c) => renderTake(pkg, run, row, c, (takes.find((t) => t.has_file) || {}).clip_id))),
        stepHolds(run, row, (kind) => kind === "clip"))));
  }

  return steps;
}

const MAX_TAKES_IN_FLIGHT = 4;

// The render options, inline: how many takes, fast or full quality, then Render.
function renderOptions(pkg, run, row, again) {
  const o = ep.renderOpts[row.shot_id] || (ep.renderOpts[row.shot_id] = { takes: 1, fast: run.settings.fast });
  const url = `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/clips/${encodeURIComponent(row.shot_id)}`;
  return [
    h("label", { class: "ep-opt small", title: "How many takes to render, each with its own seed" },
      "Takes ", h("select", { onchange: (e) => { o.takes = Number(e.target.value); } }, [1, 2, 3, 4].map((n) => h("option", { value: n, selected: o.takes === n }, n)))),
    h("label", { class: "ep-opt small", title: "Fast: a lower resolution with the keyframes only as references. Full: the run's normal size and every reference picture." },
      h("select", { onchange: (e) => { o.fast = e.target.value === "fast"; } },
        h("option", { value: "fast", selected: o.fast }, "Fast"), h("option", { value: "full", selected: !o.fast }, "Full quality"))),
    h("button", { type: "button", class: "btn small", title: "Render new takes of this shot now; each appears beside the clip while it renders", onclick: (e) => epAction(url, e.currentTarget, { revision_id: pkg.revision_id, fast: o.fast, takes: o.takes }) }, again ? "Render again" : "Render now"),
  ];
}

// One take of a shot's clip: a gray box while it renders, the video once it is done.
function renderTake(pkg, run, row, c, usedId) {
  const url = `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/clips/${encodeURIComponent(row.shot_id)}`;
  const who = `seed ${c.seed ?? "?"} · ${c.fast ? "fast" : "full"}`;
  const del = h("button", { type: "button", class: "btn ghost small", title: "Delete this take", onclick: (e) => epAction(clipUrl(c.clip_id).replace(/\/content$/, ""), e.currentTarget, undefined, "DELETE") }, "Delete");
  if (c.state === "rendering") {
    const queued = c.status === "pending" && c.ahead > 0;
    return h("div", { class: "ep-take busy", title: who },
      h("div", { class: "ep-take-box" },
        h("div", { class: "ep-spinner", "aria-hidden": "true" }),
        h("div", { class: "small" }, queued ? `queued · ${c.ahead} ahead` : "rendering…"),
        h("div", { class: "small mono ep-elapsed", "data-since": c.created_at }, elapsed(c.created_at))),
      h("div", { class: "small muted" }, who));
  }
  if (c.state === "failed") {
    return h("div", { class: "ep-take failed" },
      h("div", { class: "ep-take-box", title: c.error || "" }, h("div", { class: "small warn-text" }, "failed"), h("div", { class: "small warn-text ep-err" }, c.error || "")),
      h("div", { class: "small muted" }, who),
      h("div", { class: "ep-take-actions" },
        h("button", { type: "button", class: "btn small", title: "Render this take again with the same seed", onclick: (e) => epAction(url, e.currentTarget, { revision_id: pkg.revision_id, fast: c.fast, takes: 1, seed: c.seed }) }, "Retry"), del));
  }
  const used = c.clip_id === usedId;
  return h("div", { class: `ep-take ${used ? "used" : ""} ${c.state === "outdated" ? "outdated" : ""}` },
    c.has_file && h("video", { controls: true, preload: "none", src: clipUrl(c.clip_id), poster: c.has_poster ? clipUrl(c.clip_id, "poster") : null, class: "ep-clip" }),
    h("div", { class: "small muted" }, who, c.state === "outdated" && h("span", { class: "chip warn", title: "The prompt or the pictures changed since this take was rendered" }, "out of date")),
    used ? h("div", { class: "small ok-text" }, "In the episode") : null,
    h("div", { class: "ep-take-actions" },
      !used && c.has_file && h("button", { type: "button", class: "btn small", title: "Cut the episode from this take", onclick: (e) => epAction(clipUrl(c.clip_id, "use"), e.currentTarget, {}) }, "Use"),
      c.has_file && h("a", { class: "btn ghost small", href: clipUrl(c.clip_id), download: `${row.shot_id}-s${c.seed ?? 0}.mp4` }, "Download"),
      del));
}

function elapsed(since) {
  const s = Math.max(0, Math.round((Date.now() - new Date(since).getTime()) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
}
// The rendering boxes' clocks tick without redrawing the page.
setInterval(() => document.querySelectorAll(".ep-elapsed[data-since]").forEach((el) => { el.textContent = elapsed(el.dataset.since); }), 1000);

// The inline prompt editor: one field per cut, the camera sentence, the sound sentence, and
// the pictures the prompt may name. Saving makes it the shot's prompt; its clips go out of date.
function renderPromptEditor(pkg, run, row, vpUrl) {
  const d = ep.edit[row.shot_id];
  const pics = row.prompt.pictures || {};
  const close = () => { delete ep.edit[row.shot_id]; renderMain(); };
  const save = async (button, thenRender) => {
    button.disabled = true;
    try {
      await api(vpUrl, { method: "PUT", body: JSON.stringify({ revision_id: pkg.revision_id, blocks: d.blocks, sound: d.sound }) });
      delete ep.edit[row.shot_id];
      if (thenRender) {
        const o = ep.renderOpts[row.shot_id] || { takes: 1, fast: run.settings.fast };
        await api(`/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/clips/${encodeURIComponent(row.shot_id)}`, { method: "POST", body: JSON.stringify({ revision_id: pkg.revision_id, fast: o.fast, takes: o.takes }) });
      }
      await loadEpisode(ep.pkg, { force: true });
    } catch (e) {
      d.error = e.message;
      button.disabled = false;
      renderMain();
    }
  };
  return h("div", { class: "ep-editor" },
    d.blocks.map((b, i) => h("div", { class: "ep-editor-block" },
      h("label", { class: "small muted" }, d.blocks.length > 1 ? `Cut ${b.sub_shot}` : "Text"),
      h("textarea", { class: "ep-field", rows: 4, oninput: (e) => { d.blocks[i].text = e.target.value; } }, b.text),
      h("label", { class: "small muted" }, "Camera"),
      h("input", { class: "ep-field", value: b.camera, oninput: (e) => { d.blocks[i].camera = e.target.value; } }))),
    h("label", { class: "small muted" }, "Sound"),
    h("textarea", { class: "ep-field", rows: 2, oninput: (e) => { d.sound = e.target.value; } }, d.sound),
    Object.keys(pics).length > 0 && h("div", { class: "small muted ep-pics" }, "Pictures you can name: ",
      Object.entries(pics).map(([ref, label]) => h("code", { class: "ep-pic", title: label, onclick: () => navigator.clipboard && navigator.clipboard.writeText(`{pic:${ref}}`) }, `{pic:${ref}}`, " ", h("span", { class: "muted" }, label)))),
    d.error && h("div", { class: "small warn-text" }, d.error),
    h("div", { class: "ep-take-actions" },
      h("button", { type: "button", class: "btn small", onclick: (e) => save(e.currentTarget, false) }, "Save"),
      h("button", { type: "button", class: "btn small", title: "Save the prompt and render a new take from it", onclick: (e) => save(e.currentTarget, true) }, "Save & render"),
      h("button", { type: "button", class: "btn ghost small", onclick: close }, "Cancel")));
}

function renderShotCard(pkg, run, row) {
  const base = `/v1/episode-runs/${encodeURIComponent(run.run_id)}`;
  const skip = new Set(run.settings.skip_shots);
  const skipped = skip.has(row.shot_id);
  return h("div", { class: `ep-shot ${skipped ? "muted" : ""}` },
    h("div", { class: "ep-shot-head" },
      h("div", {},
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); openOnBoard(row.shot_id); } }, row.shot_id),
        " ", stageChip(row),
        h("div", { class: "small muted" }, `${row.place} · ${secs(row.clip_ms)}`, row.people.length > 0 && ` · ${row.people.join(", ")}`)),
      h("div", { class: "ep-actions" },
        run.status !== "done" && (skipped
          ? h("button", { type: "button", class: "btn small", onclick: (e) => epAction(`${base}/retry`, e.currentTarget, { shot_id: row.shot_id }) }, "Include")
          : h("button", { type: "button", class: "btn ghost small", title: "Leave this shot out of the run and the assembly", onclick: (e) => epAction(`${base}/settings`, e.currentTarget, { skip_shots: [...skip, row.shot_id] }, "PATCH") }, "Skip")),
        (row.stage === "waiting" || (row.clip || {}).state === "failed" || Object.keys(row.holds || {}).length > 0) && run.status !== "done" &&
          h("button", { type: "button", class: "btn small", title: "Forget this shot's failed tries and holds and run it again", onclick: (e) => epAction(`${base}/retry`, e.currentTarget, { shot_id: row.shot_id }) }, "Restart shot"))),
    skipped ? h("div", { class: "small muted" }, "Skipped: left out of the run and the assembly.") : h("div", { class: "ep-steps" }, renderShotSteps(pkg, run, row)));
}

function renderRunShots(pkg, run) {
  return h("section", { class: "card" },
    h("div", { class: "section-head" }, h("h3", {}, "Shots & steps"), h("span", { class: "small muted" }, `${run.shots.length} shots, each from cuts to clip`)),
    h("div", { class: "ep-shotlist" }, run.shots.map((row) => renderShotCard(pkg, run, row))));
}

const EP_TONE = { ok: "ok", warn: "warn", bad: "bad", info: "" };
function renderRunEvents(run) {
  const events = [...run.events].reverse();
  return stickyDetails("ep-events", `What the run did (${events.length})`,
    h("ol", { class: "timeline-list" }, events.map((e) =>
      h("li", { class: `event ${EP_TONE[e.kind] || ""}` },
        h("span", { class: "event-time mono" }, clock(e.at)),
        h("div", {}, e.shot_id && h("span", { class: "chip" }, e.shot_id), " ", h("span", {}, e.text))))));
}

function renderEpisode(pkg) {
  if (ep.key !== epKey(pkg)) loadEpisode(pkg);
  if (frames.key !== framesKey(pkg)) loadFrames(pkg);  // the providers, and the board behind the links
  const ready = ep.key === epKey(pkg) && ep.episodes !== null;
  if (ready && epLive(ep.run) && !ep.timer && !ep.loading) loadEpisode(pkg);
  if (!ready) return h("div", { class: "loading" }, ep.error || "Loading…");
  const run = ep.run;
  return h("div", {},
    renderEpisodeControls(pkg),
    run && renderRunHead(pkg, run),
    run && renderRunShots(pkg, run),
    run && renderRunEvents(run),
  );
}
