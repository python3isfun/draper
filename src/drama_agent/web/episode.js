// The Episode tab: one button that takes an episode from its storyboard to its clips and a
// rough assembly (shots/episode.py), with the plan, the progress and the holds in view.
// Uses h(), api(), go() from app.js and secs(), drawUrl(), framesOpenShot(), numberSelect(),
// providerName(), stickyDetails() from shots.js.

const ep = { key: null, episodes: null, episode: null, run: null, error: "", timer: null, loading: false,
  settings: { draws: 2, max_rounds: 3, provider: null, holds: [], render: true, assemble: true, parallel: 2, approve_failed: true, fast: false },
  skip: new Set(), busy: false, events: false, prompts: {} };

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
  if (ep.run && ep.run.status === "running" && epShown(key)) {
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

function holdButtons(run, row) {
  const base = `/v1/episode-runs/${encodeURIComponent(run.run_id)}`;
  return Object.entries(row.holds).map(([key, why]) => {
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

function renderRunShots(pkg, run) {
  const base = `/v1/episode-runs/${encodeURIComponent(run.run_id)}`;
  const skip = new Set(run.settings.skip_shots);
  return h("section", { class: "card" },
    h("h3", {}, "Shots"),
    h("table", { class: "ep-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Shot"), h("th", {}, "Stage"), h("th", {}, "Keyframes"), h("th", {}, "Prompt"), run.settings.render && h("th", {}, "Clip"), h("th", {}, ""))),
      h("tbody", {}, run.shots.map((row) => {
        const vp = row.prompt;
        const vpTone = vp.state === "current" && !vp.problems.length ? "ok" : vp.state === "writing" ? "accent" : vp.problems.length || vp.state === "failed" ? "warn" : "";
        const clip = row.clip || {};
        const clipTone = { current: "ok", rendering: "accent", failed: "bad", outdated: "warn" }[clip.state] || "";
        return h("tr", { class: row.stage === "skipped" ? "muted" : "" },
          h("td", {},
            h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(state.job.job_id, "keyframes"); setTimeout(() => framesOpenShot(row.shot_id), 50); } }, row.shot_id),
            h("div", { class: "small muted" }, `${row.place} · ${secs(row.clip_ms)}`),
            row.people.length > 0 && h("div", { class: "small muted" }, row.people.join(", "))),
          h("td", {}, stageChip(row), holdButtons(run, row)),
          h("td", {}, h("div", { class: "ep-kf" }, row.keyframes.map((k) =>
            k.take_id != null && k.n != null
              ? h("img", { src: drawUrl({ take_id: k.take_id }, k.n), alt: `cut ${k.index}`, title: `cut ${k.index}: approved`, class: "ep-thumb" })
              : h("span", { class: `ep-slot ${k.busy ? "busy" : ""}`, title: k.busy ? `cut ${k.index}: drawing` : `cut ${k.index}: no keyframe yet` }, k.index))),
            h("div", { class: "small muted" }, `${row.approved}/${row.cuts} approved`)),
          h("td", {}, h("span", { class: `chip ${vpTone}` }, vp.state), vp.problems.length > 0 && h("div", { class: "small warn-text" }, vp.problems.join("; ")), vp.error && h("div", { class: "small warn-text" }, vp.error)),
          run.settings.render && h("td", {},
            clip.state && clip.state !== "none" && h("span", { class: `chip ${clipTone}` }, clip.state),
            clip.has_file && h("div", {},
              h("video", { controls: true, preload: "none", src: clipUrl(clip.clip_id), poster: clip.has_poster ? clipUrl(clip.clip_id, "poster") : null, class: "ep-clip" }),
              h("div", { class: "small muted" }, `${clip.duration_s} s · ${clip.model || ""}`)),
            clip.error && h("div", { class: "small warn-text" }, clip.error)),
          h("td", { class: "ep-actions" },
            run.status !== "done" && (skip.has(row.shot_id)
              ? h("button", { type: "button", class: "btn small", onclick: (e) => epAction(`${base}/retry`, e.currentTarget, { shot_id: row.shot_id }) }, "Include")
              : h("button", { type: "button", class: "btn ghost small", title: "Leave this shot out of the run and the assembly", onclick: (e) => epAction(`${base}/settings`, e.currentTarget, { skip_shots: [...skip, row.shot_id] }, "PATCH") }, "Skip")),
            (row.stage === "waiting" || clip.state === "failed") && h("button", { type: "button", class: "btn small", title: "Forget this shot's failed tries and holds", onclick: (e) => epAction(`${base}/retry`, e.currentTarget, { shot_id: row.shot_id }) }, "Retry"),
            run.settings.render && vp.state === "current" && !vp.problems.length && clip.state !== "rendering" && h("button", { type: "button", class: "btn small", title: "Render this shot's clip again now", onclick: (e) => epAction(`/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/clips/${encodeURIComponent(row.shot_id)}`, e.currentTarget, { revision_id: pkg.revision_id, fast: run.settings.fast }) }, "Render again")));
      }))));
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
  if (ready && ep.run && ep.run.status === "running" && !ep.timer && !ep.loading) loadEpisode(pkg);
  if (!ready) return h("div", { class: "loading" }, ep.error || "Loading…");
  const run = ep.run;
  return h("div", {},
    renderEpisodeControls(pkg),
    run && renderRunHead(pkg, run),
    run && renderRunShots(pkg, run),
    run && renderRunEvents(run),
  );
}
