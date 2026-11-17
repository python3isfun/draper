"use strict";

/*
 * Keyframes tab: a checked start (and end) frame per shot, drawn from the design picks,
 * for video production. Uses h(), api(), state, renderMain() and stickyDetails() from app.js.
 * Model text is always inserted as text (h() uses text nodes).
 */

const frames = { key: null, view: null, error: "", notes: [], timer: null, loading: false,
  draws: 2, rounds: 3, directions: {}, prompts: {}, episode: "all" };

const framesKey = (pkg) => `${pkg.package_id}|${pkg.revision_id}`;
const framesBase = (pkg) => `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/keyframes`;
const drawUrl = (take, n, v = "thumb") =>
  `/v1/shot-takes/${encodeURIComponent(take.take_id)}/draws/${n}/${v}`;

async function loadFrames(pkg) {
  const key = framesKey(pkg);
  if (frames.key !== key) Object.assign(frames, { key, view: null, error: "", notes: [] });
  clearTimeout(frames.timer);
  frames.timer = null;
  frames.loading = true;
  try {
    const view = await api(`${framesBase(pkg)}?${new URLSearchParams({ revision_id: pkg.revision_id })}`);
    if (frames.key !== key) return;
    const changed = JSON.stringify(view) !== JSON.stringify(frames.view);
    frames.view = view;
    frames.error = "";
    if (changed && state.tab === "keyframes") renderWhenNotSelecting();
  } catch (e) {
    if (frames.key !== key) return;
    frames.error = e.message;
    if (state.tab === "keyframes") renderWhenNotSelecting();
  } finally {
    frames.loading = false;
  }
  if (frames.view && frames.view.busy > 0 && state.tab === "keyframes" && state.pkg && framesKey(state.pkg) === key) {
    frames.timer = setTimeout(() => loadFrames(state.pkg), 2000);
  }
}

async function frameAction(url, button, body, method = "POST") {
  if (button) button.disabled = true;
  frames.error = "";
  let out = null;
  try {
    out = await api(url, { method, ...(body ? { body: JSON.stringify(body) } : {}) });
  } catch (e) {
    frames.error = e.message;
  }
  await loadFrames(state.pkg);
  renderMain();
  return out;
}

const TAKE_STATUS = { pending: "waiting", writing: "writing the prompt…", drawing: "drawing…", checking: "checking…", failed: "failed" };

function checkBadge(d) {
  if (d.status === "failed") return h("span", { class: "chip bad" }, "draw failed");
  if (!d.check) return h("span", { class: "chip warn", title: d.check_error || "" }, d.check_error ? "not checked" : "checking…");
  return h("span", { class: `chip ${d.check.passed ? "ok" : "bad"}`, title: d.check.summary || "" },
    `${d.check.passed ? "pass" : "fail"} · ${d.check.score}`);
}

function renderFrameDraw(pkg, take) {
  return (d) => {
    const approved = take.approved === d.n;
    const failedItems = d.check ? d.check.items.filter((i) => !i.passed) : [];
    const finished = take.status === "done" || take.status === "failed";
    return h("figure", { class: `draw keyframe ${d.status}${approved ? " selected" : ""}` },
      d.status === "done" && !finished
        ? h("img", { class: "draw-waiting", src: drawUrl(take, d.n), alt: `Draw ${d.n + 1}`, loading: "lazy", title: "Approve once the round is finished" })
        : d.status === "done"
        ? h("button", { type: "button", class: "draw-pick", title: approved ? "Approved" : "Approve this frame for the shot",
            onclick: (e) => frameAction(`/v1/shot-takes/${encodeURIComponent(take.take_id)}/approve`, e.currentTarget, { n: d.n }) },
            h("img", { src: drawUrl(take, d.n), alt: `Draw ${d.n + 1}`, loading: "lazy" }))
        : h("div", { class: "draw-placeholder", title: d.error || "" }, "failed"),
      h("figcaption", {},
        h("div", { class: "keyframe-cap" }, `Draw ${d.n + 1}${approved ? " ✓ approved" : ""}`, checkBadge(d)),
        d.status === "done" && h("a", { href: drawUrl(take, d.n, "content"), target: "_blank", rel: "noopener", class: "muted small" }, "open"),
        approved && h("button", { type: "button", class: "btn ghost small",
          onclick: (e) => frameAction(`/v1/shot-takes/${encodeURIComponent(take.take_id)}/approve`, e.currentTarget, null, "DELETE") }, "Withdraw"),
        (failedItems.length > 0 || (d.check && d.check.summary)) && h("details", { class: "small" },
          h("summary", {}, failedItems.length ? `${failedItems.length} issue${failedItems.length > 1 ? "s" : ""}` : "Check"),
          d.check.summary && h("p", { class: "muted" }, d.check.summary),
          h("ul", { class: "plain check-items" }, failedItems.map((i) =>
            h("li", {}, h("strong", {}, `${i.item}`), h("span", { class: "muted" }, ` (${i.severity})`), `: ${i.finding}`))),
          d.check.fix && h("p", {}, h("strong", {}, "Fix: "), d.check.fix)),
      ),
    );
  };
}

function renderFrameTake(pkg, take) {
  const busy = !["done", "failed"].includes(take.status);
  const tid = encodeURIComponent(take.take_id);
  const uncheckable = take.status === "done" && take.results.some((d) => d.status === "done" && !d.check);
  return h("div", { class: "take" },
    h("div", { class: "take-head" },
      h("strong", {}, `Round ${take.round}`),
      busy && h("span", { class: "chip info" }, TAKE_STATUS[take.status]),
      take.status === "failed" && h("span", { class: "chip bad", title: take.error || "" }, take.refused ? "refused" : "failed"),
      take.status === "done" && (take.results.some((d) => d.check && d.check.passed)
        ? h("span", { class: "chip ok" }, "passed")
        : h("span", { class: "chip warn" }, take.auto && take.round < take.max_rounds ? "revising…" : "no draw passed: your call")),
      take.direction && h("span", { class: "chip" }, `“${take.direction}”`),
      take.prompt_source === "user" && h("span", { class: "chip" }, "your prompt"),
      take.prompt_source === "template" && h("span", { class: "chip warn" }, "template prompt"),
      h("span", { class: "take-actions" },
        uncheckable && h("button", { type: "button", class: "btn ghost small",
          onclick: (e) => frameAction(`/v1/shot-takes/${tid}/check`, e.currentTarget) }, "Check again"),
        take.status === "failed" && !take.refused && h("button", { type: "button", class: "btn ghost small",
          onclick: (e) => frameAction(`/v1/shot-takes/${tid}/retry`, e.currentTarget) }, "Retry"),
        !busy && h("button", { type: "button", class: "btn ghost small", title: "Delete this round and its draws",
          onclick: (e) => frameAction(`/v1/shot-takes/${tid}`, e.currentTarget, null, "DELETE") }, "Delete")),
    ),
    take.error && h("p", { class: `small ${take.refused ? "warn-text" : "muted"}` }, take.error),
    take.results.length > 0 && h("div", { class: "draws" }, take.results.map(renderFrameDraw(pkg, take))),
    take.prompt && stickyDetails(`kf:${take.take_id}`, `Prompt${take.prompt_model ? ` (by ${take.prompt_model})` : ""}`,
      take.fixes.length > 0 && h("div", { class: "small" }, h("strong", {}, "This round fixes:"),
        h("ul", { class: "plain" }, take.fixes.map((f) => h("li", {}, f)))),
      h("pre", { class: "prompt-view" }, take.prompt),
      take.motion && h("p", { class: "small" }, h("strong", {}, "Motion: "), take.motion),
      take.notes && h("p", { class: "small muted" }, take.notes)),
  );
}

function renderNewFrameTake(pkg, shot, frame) {
  const key = `${shot.shot_id}|${frame}`;
  const input = h("input", { type: "text", placeholder: frame === "end" ? "Direction for the end frame (optional), e.g. she has reached the door" : "Direction (optional), e.g. tighter on the speaker, rain on the window",
    oninput: (e) => { frames.directions[key] = e.target.value; } });
  input.value = frames.directions[key] || "";
  const box = h("textarea", { class: "prompt-edit", rows: 6, spellcheck: "false", placeholder: "Your own prompt: <Picture N> are the reference pictures above, in order.",
    oninput: (e) => { frames.prompts[key] = e.target.value; } });
  box.value = frames.prompts[key] || "";
  const url = `${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}`;
  const body = (extra) => ({ revision_id: pkg.revision_id, frame, draws: frames.draws, max_rounds: frames.rounds, ...extra });
  return h("div", {},
    h("div", { class: "new-take" }, input,
      h("button", { type: "button", class: "btn small", onclick: async (e) => {
        const direction = (frames.directions[key] || "").trim();
        await frameAction(url, e.currentTarget, body({ direction: direction || null }));
        if (!frames.error) { frames.directions[key] = ""; renderMain(); }
      } }, frame === "end" ? "Make end frame" : "New take")),
    stickyDetails(`kfp:${key}`, "Write the prompt yourself",
      box,
      h("div", { class: "prompt-edit-actions" },
        h("button", { type: "button", class: "btn small", onclick: async (e) => {
          const prompt = (frames.prompts[key] || "").trim();
          if (!prompt) return;
          await frameAction(url, e.currentTarget, body({ prompt, auto: false }));
          if (!frames.error) { frames.prompts[key] = ""; renderMain(); }
        } }, "Draw my prompt"),
        h("span", { class: "muted small" }, "Checked like any other take, but never rewritten automatically."))),
  );
}

function renderRefStrip(shot) {
  return h("div", { class: "ref-strip" }, shot.references.map((r, i) => {
    let src;
    if (r.ref_id.startsWith("frame:")) {
      const [, takeId, n] = r.ref_id.split(":");
      src = `/v1/shot-takes/${encodeURIComponent(takeId)}/draws/${n}/thumb`;
    } else {
      src = `/v1/generated-images/${encodeURIComponent(r.ref_id)}/thumb`;
    }
    return h("figure", { class: "ref", title: r.role },
      h("img", { src, alt: r.role, loading: "lazy" }),
      h("figcaption", {}, `<Picture ${i + 1}>`, r.likeness && h("span", { class: "warn-text" }, " · likeness")));
  }));
}

function renderShotCard(pkg, shot) {
  const takes = (frame) => shot.takes.filter((t) => t.frame === frame);
  const start = takes("start"), end = takes("end");
  const ap = shot.approved;
  return h("section", { class: "card design-card shot-card", id: `kf-${shot.shot_id}` },
    h("div", { class: "design-card-head" },
      h("h3", {}, shot.shot_id, h("span", { class: "muted small" }, ` · ${shot.place} · ${(shot.duration_ms / 1000).toFixed(1)} s`)),
      h("span", { class: "chips" },
        shot.intimacy && h("span", { class: "chip warn", title: "Scene staged with intimacy: clothed staging only, and never from a likeness" }, `intimacy: ${shot.intimacy}`),
        h("span", { class: `chip ${ap.start ? "ok" : "warn"}` }, ap.start ? "start ✓" : "start: approve one"),
        ap.end ? h("span", { class: "chip ok" }, "end ✓")
          : shot.end_suggested ? h("span", { class: "chip info", title: "The prompt writer thinks this shot moves enough to pin its last frame too" }, "end frame suggested") : null)),
    h("p", { class: "small" }, h("strong", {}, shot.framing), " ", shot.camera),
    h("p", { class: "small" }, shot.action),
    h("p", { class: "small muted" }, shot.people.length ? `On screen: ${shot.people.join(", ")}` : "No one on screen"),
    shot.missing.length > 0
      ? h("p", { class: "small warn-text" }, `Pick design images first (Assets tab): ${shot.missing.join("; ")}.`)
      : renderRefStrip(shot),
    h("div", { class: "frame-block" },
      h("h4", {}, "Start frame"),
      start.length ? start.map((t) => renderFrameTake(pkg, t)) : h("p", { class: "muted small" }, "No takes yet."),
      shot.missing.length === 0 && renderNewFrameTake(pkg, shot, "start")),
    ap.start && h("div", { class: "frame-block" },
      h("h4", {}, "End frame", h("span", { class: "muted small" }, " · optional, drawn from the approved start frame")),
      end.map((t) => renderFrameTake(pkg, t)),
      renderNewFrameTake(pkg, shot, "end")),
  );
}

function renderKeyframes(pkg) {
  if (frames.key !== framesKey(pkg)) loadFrames(pkg);
  const v = frames.key === framesKey(pkg) ? frames.view : null;
  if (v && v.busy && !frames.timer && !frames.loading) loadFrames(pkg);  // resume polling
  if (!v) return h("div", { class: "loading" }, frames.error || "Loading…");
  const shots = v.shots;
  const approved = shots.filter((s) => s.approved.start).length;
  const passed = shots.filter((s) => s.takes.some((t) => t.frame === "start" && t.results.some((d) => d.check && d.check.passed))).length;
  const blocked = shots.filter((s) => s.missing.length).length;
  const episodes = [...new Set(shots.map((s) => s.episode_id))];
  const shown = frames.episode === "all" ? shots : shots.filter((s) => s.episode_id === frames.episode);
  const controls = h("section", { class: "card" },
    h("h3", {}, "Shot keyframes"),
    h("p", { class: "small muted" },
      "Each shot gets a start frame drawn from the picked looks, set and props, checked against them (faces, wardrobe, who is where, framing, action, defects) and revised automatically until a draw passes or the rounds run out. Approve one draw per shot; only approved frames go into the export for video production. Shots that move can also get an end frame."),
    h("div", { class: "chips", style: "margin:8px 0" },
      h("span", { class: `chip ${approved === shots.length ? "ok" : "info"}` }, `${approved}/${shots.length} approved`),
      h("span", { class: "chip" }, `${passed} passed the check`),
      blocked > 0 && h("span", { class: "chip warn" }, `${blocked} waiting for design picks`),
      v.busy > 0 && h("span", { class: "chip info" }, `${v.busy} running`)),
    h("div", { class: "design-row" },
      h("label", {}, "Draws ", numberSelect(frames.draws, 4, (n) => { frames.draws = n; }, "Draws per round")),
      h("label", {}, " Rounds ", numberSelect(frames.rounds, 3, (n) => { frames.rounds = n; }, "Automatic rounds")),
      h("button", { type: "button", class: "btn", onclick: async (e) => {
        const out = await frameAction(framesBase(pkg), e.currentTarget, { revision_id: pkg.revision_id, draws: frames.draws, max_rounds: frames.rounds });
        if (out) frames.notes = out.skipped.map((s) => `${s.shot_id}: ${s.reason}`);
        renderMain();
      } }, "Make start frames for every shot without one"),
      episodes.length > 1 && h("select", { "aria-label": "Episode", onchange: (e) => { frames.episode = e.target.value; renderMain(); } },
        h("option", { value: "all", selected: frames.episode === "all" }, "All episodes"),
        episodes.map((ep) => h("option", { value: ep, selected: frames.episode === ep }, ep)))),
    frames.error && h("p", { class: "small warn-text" }, frames.error),
    frames.notes.length > 0 && stickyDetails("kf-notes", `${frames.notes.length} shot${frames.notes.length > 1 ? "s" : ""} not started`,
      h("ul", { class: "plain small" }, frames.notes.map((n) => h("li", {}, n)))),
  );
  return h("div", {}, controls, shown.map((s) => renderShotCard(pkg, s)));
}
