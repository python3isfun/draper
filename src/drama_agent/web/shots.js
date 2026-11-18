"use strict";

/*
 * Keyframes tab: each shot's sub-shot plan (the cuts inside its one video generation) and a
 * checked keyframe per sub-shot, drawn from the design picks, for video production. Uses
 * h(), api(), state, renderMain(), stickyDetails() and numberSelect() from app.js. Model
 * text is always inserted as text (h() uses text nodes).
 */

const frames = { key: null, view: null, error: "", notes: [], timer: null, loading: false,
  draws: 2, rounds: 3, provider: null, directions: {}, prompts: {}, plans: {}, episode: "all",
  sel: {}, notes2: {}, compare: {}, diffs: new Map() };

const framesKey = (pkg) => `${pkg.package_id}|${pkg.revision_id}`;
const framesBase = (pkg) => `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/keyframes`;
const drawUrl = (take, n, v = "thumb") =>
  `/v1/shot-takes/${encodeURIComponent(take.take_id)}/draws/${n}/${v}`;
const secs = (ms) => `${(ms / 1000).toFixed(1)} s`;

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

function renderFrameDraw(take) {
  return (d) => {
    const approved = take.approved === d.n;
    const finished = take.status === "done" || take.status === "failed";
    const failedItems = d.check ? d.check.items.filter((i) => !i.passed) : [];
    const label = `Draw ${d.n + 1}`;
    return h("figure", { class: `draw keyframe ${d.status}${approved ? " selected" : ""}` },
      d.status !== "done"
        ? h("div", { class: "draw-placeholder", title: d.error || "" }, "failed")
        : !finished
          ? h("img", { class: "draw-waiting", src: drawUrl(take, d.n), alt: label, loading: "lazy", title: "Approve once the round is finished" })
          : h("button", { type: "button", class: "draw-pick", title: approved ? "Approved" : "Approve this keyframe",
              onclick: (e) => frameAction(`/v1/shot-takes/${encodeURIComponent(take.take_id)}/approve`, e.currentTarget, { n: d.n }) },
              h("img", { src: drawUrl(take, d.n), alt: label, loading: "lazy" })),
      h("figcaption", {},
        h("div", { class: "keyframe-cap" }, `${label}${approved ? " ✓ approved" : ""}`, checkBadge(d)),
        d.status === "done" && h("a", { href: drawUrl(take, d.n, "content"), target: "_blank", rel: "noopener", class: "muted small" }, "open"),
        approved && h("button", { type: "button", class: "btn ghost small",
          onclick: (e) => frameAction(`/v1/shot-takes/${encodeURIComponent(take.take_id)}/approve`, e.currentTarget, null, "DELETE") }, "Withdraw"),
        (failedItems.length > 0 || (d.check && d.check.summary)) && h("details", { class: "small" },
          h("summary", {}, failedItems.length ? `${failedItems.length} issue${failedItems.length > 1 ? "s" : ""}` : "Check"),
          d.check.summary && h("p", { class: "muted" }, d.check.summary),
          h("ul", { class: "plain check-items" }, failedItems.map((i) =>
            h("li", {}, h("strong", {}, i.item), h("span", { class: "muted" }, ` (${i.severity})`), `: ${i.finding}`))),
          d.check.fix && h("p", {}, h("strong", {}, "Fix: "), d.check.fix)),
      ),
    );
  };
}

function renderFrameTake(take, takes = []) {
  const busy = !["done", "failed"].includes(take.status);
  // The automatic round after this one, if it has started (same run, next round).
  const next = takes.find((t) => t.run_id === take.run_id && t.round === take.round + 1);
  const tid = encodeURIComponent(take.take_id);
  const uncheckable = take.status === "done" && take.results.some((d) => d.status === "done" && !d.check);
  return h("div", { class: "take" },
    h("div", { class: "take-head" },
      h("strong", {}, `Round ${take.round}`),
      busy && h("span", { class: "chip info" }, TAKE_STATUS[take.status]),
      take.status === "failed" && h("span", { class: "chip bad", title: take.error || "" }, take.refused ? "refused" : "failed"),
      take.status === "done" && (take.results.some((d) => d.check && d.check.passed)
        ? h("span", { class: "chip ok" }, "passed")
        : next ? h("span", { class: "chip" }, `revised in round ${next.round}`)
        // The next automatic round is created as this one finishes, so it shows on the next poll.
        : h("span", { class: "chip warn" }, "no draw passed: your call")),
      take.direction && h("span", { class: "chip" }, `“${take.direction}”`),
      take.prompt_version && h("span", { class: "chip", title: "The prompt version this take draws" }, `prompt v${take.prompt_version}`),
      take.provider && take.provider !== "h3" && h("span", { class: "chip", title: "The image provider that drew this take" }, providerName(take.provider)),
      take.prompt_source === "user" && h("span", { class: "chip" }, "your prompt"),
      take.warnings && take.warnings.length > 0 && h("span", { class: "chip warn", title: take.warnings.join("\n") }, `${take.warnings.length} rule finding${take.warnings.length > 1 ? "s" : ""}`),
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
    take.results.length > 0 && h("div", { class: "draws" }, take.results.map(renderFrameDraw(take))),
    take.prompt && stickyDetails(`kf:${take.take_id}`, `Prompt${take.prompt_model ? ` (by ${take.prompt_model})` : ""}`,
      take.fixes.length > 0 && h("div", { class: "small" }, h("strong", {}, "This round fixes:"),
        h("ul", { class: "plain" }, take.fixes.map((f) => h("li", {}, f)))),
      h("pre", { class: "prompt-view" }, take.prompt),
      take.motion && h("p", { class: "small" }, h("strong", {}, "Motion: "), take.motion),
      take.notes && h("p", { class: "small muted" }, take.notes)),
  );
}

// Word-level diff of two prompts (longest common subsequence) as [kind, text] parts, kind
/** A provider's name as shown. */
const providerName = (name) => ({ h3: "MiniMax H3", "nano-banana": "Nano Banana" })[name] || name;

// "=", "-" or "+". Cached per pair of texts: the board redraws every 2 s while busy.
function wordDiff(a, b) {
  const key = `${a}\u0000${b}`;
  if (frames.diffs.has(key)) return frames.diffs.get(key);
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  const n = x.length, m = y.length;
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push(["=", x[i]]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push(["-", x[i++]]);
    else out.push(["+", y[j++]]);
  }
  while (i < n) out.push(["-", x[i++]]);
  while (j < m) out.push(["+", y[j++]]);
  if (frames.diffs.size > 50) frames.diffs.clear();
  frames.diffs.set(key, out);
  return out;
}

const diffNodes = (parts) => parts.map(([k, t]) => (k === "=" ? t : h(k === "-" ? "del" : "ins", {}, t)));

const SOURCE_LABEL = { model: "written by the model", template: "built-in template", user: "your edit" };

// The sub-shot's prompt: its versions, an editor (save as the next version), compare with an
// earlier version, and draw any version with the last take's seeds.
function renderPromptVersions(pkg, shot, sub) {
  const key = `${shot.shot_id}|${sub.index}`;
  const versions = sub.versions || [];
  const latest = versions[versions.length - 1];
  const nextV = sub.next_version || (latest ? latest.v + 1 : 1);
  const selV = frames.sel[key] ?? (latest ? latest.v : null);
  const sel = versions.find((v) => v.v === selV) || latest;
  const url = `${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}`;
  const draft = frames.prompts[key];
  const text = draft ?? (sel ? sel.prompt : "");
  const box = h("textarea", { class: "prompt-edit", rows: 9, spellcheck: "false",
    placeholder: "Write the prompt: <Picture N> are the reference pictures above, in order.",
    oninput: (e) => { frames.prompts[key] = e.target.value; sync(); } });
  box.value = text;
  const note = h("input", { type: "text", placeholder: "What you changed (optional)",
    oninput: (e) => { frames.notes2[key] = e.target.value; } });
  note.value = frames.notes2[key] || "";
  const edited = () => sel ? box.value.trim() !== sel.prompt.trim() : box.value.trim() !== "";
  const saveBtn = h("button", { type: "button", class: "btn small", onclick: async (e) => {
    const out = await frameAction(`${url}/prompts`, e.currentTarget, { revision_id: pkg.revision_id, sub_shot: sub.index, prompt: box.value, based_on: sel ? sel.v : null, note: frames.notes2[key] || "" });
    if (out && !frames.error) { delete frames.prompts[key]; delete frames.notes2[key]; frames.sel[key] = out.v; renderMain(); }
  } }, `Save as v${nextV}`);
  const drawBtn = h("button", { type: "button", class: "btn small", onclick: async (e) => {
    const body = { revision_id: pkg.revision_id, sub_shot: sub.index, draws: frames.draws, provider: frames.provider };
    if (edited()) Object.assign(body, { prompt: box.value, based_on: sel ? sel.v : null, note: frames.notes2[key] || "" });
    else body.prompt_version = sel.v;
    await frameAction(url, e.currentTarget, body);
    if (!frames.error) { delete frames.prompts[key]; delete frames.notes2[key]; delete frames.sel[key]; renderMain(); }
  } }, "");
  const counter = h("span", { class: "muted small" });
  function sync() {
    const changed = edited();
    saveBtn.disabled = !changed || !box.value.trim();
    drawBtn.disabled = !box.value.trim();
    drawBtn.textContent = changed ? "Save and draw" : sel ? `Draw v${sel.v}` : "Draw";
    counter.textContent = `${box.value.length.toLocaleString()} characters${changed ? " · edited" : ""}`;
  }
  sync();
  const against = frames.compare[key];
  const base = against != null && against !== sel?.v && versions.find((v) => v.v === against);
  return stickyDetails(`kfv:${key}`, versions.length ? `Prompt · v${sel.v} · ${versions.length} version${versions.length > 1 ? "s" : ""}` : "Prompt",
    versions.length > 0 && h("div", { class: "version-row" },
      h("label", { class: "small" }, "Version ",
        h("select", { "aria-label": "Prompt version", onchange: (e) => { frames.sel[key] = Number(e.target.value); delete frames.prompts[key]; delete frames.compare[key]; renderMain(); } },
          [...versions].reverse().map((v) => h("option", { value: v.v, selected: v.v === sel.v },
            `v${v.v} · ${SOURCE_LABEL[v.source] || v.source}${v.based_on ? ` from v${v.based_on}` : ""} · ${timeAgo(v.created_at)}`)))),
      versions.length > 1 && h("label", { class: "small" }, " compare with ",
        h("select", { "aria-label": "Compare with", onchange: (e) => { frames.compare[key] = e.target.value === "" ? null : Number(e.target.value); renderMain(); } },
          h("option", { value: "" }, "—"),
          versions.filter((v) => v.v !== sel.v).reverse().map((v) => h("option", { value: v.v, selected: v.v === against }, `v${v.v}`))))),
    sel && sel.note && h("p", { class: "small muted" }, sel.note),
    sel && sel.findings.length > 0 && h("div", { class: "small warn-text" }, h("strong", {}, `v${sel.v} breaks the H3 image rules:`),
      h("ul", { class: "plain" }, sel.findings.map((f) => h("li", {}, f)))),
    base && h("div", { class: "prompt-diff small" }, h("div", { class: "muted" }, `Changes from v${base.v} to v${sel.v}:`), diffNodes(wordDiff(base.prompt, sel.prompt))),
    box,
    h("div", { class: "prompt-edit-actions" }, note),
    h("div", { class: "prompt-edit-actions" }, saveBtn, drawBtn,
      draft != null && h("button", { type: "button", class: "btn ghost small", onclick: () => { delete frames.prompts[key]; renderMain(); } }, "Discard edits"),
      counter),
    h("p", { class: "muted small" }, "Saving keeps every version. Drawing a version uses the last take's seeds, so its draws compare one for one; a version is drawn as written, never rewritten. To restore an old version, pick it and draw it, or save it again as the newest."));
}

function renderNewFrameTake(pkg, shot, sub) {
  const key = `${shot.shot_id}|${sub.index}`;
  const input = h("input", { type: "text", placeholder: "Direction for the writer (optional), e.g. tighter on the speaker, rain on the window",
    oninput: (e) => { frames.directions[key] = e.target.value; } });
  input.value = frames.directions[key] || "";
  const url = `${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}`;
  return h("div", {},
    renderPromptVersions(pkg, shot, sub),
    h("div", { class: "new-take" }, input,
      h("button", { type: "button", class: "btn small", title: "The model writes a new prompt version and draws it", onclick: async (e) => {
        const direction = (frames.directions[key] || "").trim();
        await frameAction(url, e.currentTarget, { revision_id: pkg.revision_id, sub_shot: sub.index, draws: frames.draws, max_rounds: frames.rounds, direction: direction || null, provider: frames.provider });
        if (!frames.error) { frames.directions[key] = ""; renderMain(); }
      } }, "New take")),
  );
}

function refSrc(r, variant = "thumb") {
  if (r.ref_id.startsWith("frame:")) {
    const [, takeId, n] = r.ref_id.split(":");
    return `/v1/shot-takes/${encodeURIComponent(takeId)}/draws/${n}/${variant}`;
  }
  return `/v1/generated-images/${encodeURIComponent(r.ref_id)}/${variant}`;
}

// Each reference picture opens full size; the arrows step through the sub-shot's pictures.
function renderRefStrip(sub) {
  const items = sub.references.map((r, i) => ({ src: refSrc(r, "content"), caption: `<Picture ${i + 1}>`, title: r.role }));
  return h("div", { class: "ref-strip" }, sub.references.map((r, i) =>
    h("figure", { class: "ref", title: `${r.role} (click to enlarge)` },
      h("button", { type: "button", class: "ref-open", "aria-label": `Enlarge <Picture ${i + 1}>: ${r.role}`, onclick: () => enlarge(items, i) },
        h("img", { src: refSrc(r), alt: r.role, loading: "lazy" })),
      h("figcaption", {}, `<Picture ${i + 1}>`, r.likeness && h("span", { class: "warn-text" }, " · likeness")))));
}

// Plan editor: one row per sub-shot (start, framing, camera, action, people).
// Where a person is in a sub-shot: "" (not in it), "on:left|center|right", "off:left|right".
const POSITIONS = [["", "—"], ["on:left", "on screen, left"], ["on:center", "on screen, center"], ["on:right", "on screen, right"], ["off:left", "off frame left"], ["off:right", "off frame right"]];

function positionsOf(sub) {
  const pos = {};
  for (const id of sub.people) pos[id] = `on:${sub.placement?.[id] || "center"}`;
  for (const [id, side] of Object.entries(sub.off_screen || {})) pos[id] = `off:${side}`;
  return pos;
}

function renderPlanEditor(pkg, shot) {
  const key = shot.shot_id;
  const draft = frames.plans[key];
  const rows = draft.rows;
  const field = (row, name, placeholder, tag = "input") => {
    const el = h(tag, { placeholder, ...(tag === "input" ? { type: "text" } : { rows: 2 }),
      oninput: (e) => { row[name] = e.target.value; } });
    el.value = row[name] || "";
    return el;
  };
  const start = (row, i) => {
    const el = h("input", { type: "number", min: 0, step: 0.1, class: "plan-start", disabled: i === 0,
      oninput: (e) => { row.start_s = e.target.value; } });
    el.value = i === 0 ? 0 : row.start_s;
    return el;
  };
  const clip = h("input", { type: "number", min: 4, max: 15, step: 1, class: "plan-start",
    oninput: (e) => { draft.clip_s = e.target.value; } });
  clip.value = draft.clip_s;
  return h("div", { class: "plan-editor" },
    h("label", { class: "small" }, "Clip length ", clip, ` s (whole seconds, 4–15; the storyboard says ${secs(shot.duration_ms)})`),
    rows.map((row, i) => h("div", { class: "plan-row" },
      h("div", { class: "plan-row-head" },
        h("strong", {}, `Sub-shot ${i + 1}`),
        h("label", { class: "small" }, " starts at ", start(row, i), " s"),
        i > 0 && h("button", { type: "button", class: "btn ghost small", onclick: () => { rows.splice(i, 1); renderMain(); } }, "Remove")),
      field(row, "framing", "Framing: shot size, angle, who is where"),
      field(row, "camera", "Camera: still, pan, push-in…"),
      field(row, "action", "Action from this cut to the next, with the lines spoken in it", "textarea"),
      h("div", { class: "plan-people small" }, shot.people.map((p) =>
        h("label", {}, `${p.name} `,
          h("select", { "aria-label": `Where ${p.name} is`, onchange: (e) => { row.pos[p.id] = e.target.value; } },
            POSITIONS.map(([v, label]) => h("option", { value: v, selected: (row.pos[p.id] || "") === v }, label)))))))),
    h("div", { class: "prompt-edit-actions" },
      rows.length < 3 && h("button", { type: "button", class: "btn ghost small", onclick: () => {
        rows.push({ start_s: "", framing: "", camera: "", action: "", pos: { ...rows[rows.length - 1].pos } });
        renderMain();
      } }, "+ Sub-shot"),
      h("button", { type: "button", class: "btn small", onclick: async (e) => {
        const sub_shots = rows.map((r, i) => {
          const people = [], placement = {}, off_screen = {};
          for (const [id, v] of Object.entries(r.pos)) {
            const [where, side] = v.split(":");
            if (where === "on") { people.push(id); placement[id] = side; }
            else if (where === "off") off_screen[id] = side;
          }
          return { start_ms: i === 0 ? 0 : Math.round(Number(r.start_s) * 1000), framing: r.framing, camera: r.camera, action: r.action, people, placement, off_screen };
        });
        await frameAction(`${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}/plan`, e.currentTarget, { revision_id: pkg.revision_id, sub_shots, clip_s: Number(draft.clip_s) || null });
        if (!frames.error) { delete frames.plans[key]; renderMain(); }
      } }, "Save plan"),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { delete frames.plans[key]; renderMain(); } }, "Cancel"),
      h("span", { class: "muted small" }, "Each sub-shot ends where the next starts. A clean spec: every line inside one sub-shot, each speaker on screen or placed off frame, every cut a new view, screen sides held across cuts. Changed sub-shots lose their approved keyframes.")));
}

function renderPlan(pkg, shot) {
  const p = shot.plan;
  const editing = frames.plans[shot.shot_id];
  const source = { single: "the whole shot as one sub-shot", model: "planned by the text model", user: "your plan" }[p.source];
  return h("div", { class: "plan" },
    h("div", { class: "take-head" },
      h("h4", { style: "margin:0" }, `Sub-shots (${shot.sub_shots.length})`),
      p.status === "writing" ? h("span", { class: "chip info" }, "planning…") : h("span", { class: "muted small" }, source),
      p.status === "failed" && h("span", { class: "chip bad", title: p.error || "" }, "plan failed"),
      !editing && h("span", { class: "take-actions" },
        h("button", { type: "button", class: "btn ghost small", disabled: shot.busy, title: "Ask the text model where the cuts go",
          onclick: (e) => frameAction(`${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}/plan`, e.currentTarget, { revision_id: pkg.revision_id }) }, "Plan cuts"),
        h("button", { type: "button", class: "btn ghost small", disabled: shot.busy, onclick: () => {
          frames.plans[shot.shot_id] = { clip_s: Math.min(15, Math.max(4, Math.ceil(shot.clip_ms / 1000))), rows: shot.sub_shots.map((s) => ({ start_s: s.start_ms / 1000, framing: s.framing, camera: s.camera, action: s.action, pos: positionsOf(s) })) };
          renderMain();
        } }, "Edit"))),
    p.error && h("p", { class: "small warn-text" }, p.error),
    p.outdated && h("p", { class: "small warn-text" }, `This plan was written ${p.outdated}.`),
    p.problems && p.problems.length > 0 && h("div", { class: "small warn-text" },
      h("strong", {}, "Not a clean spec for video yet:"),
      h("ul", { class: "plain" }, p.problems.map((x) => h("li", {}, x)))),
    p.notes && h("p", { class: "small muted" }, p.notes),
    editing && renderPlanEditor(pkg, shot),
  );
}

const stateLine = (st) => [st.wearing && `wears ${st.wearing}`, st.hair && `hair ${st.hair}`, st.carrying ? `holding ${st.carrying}` : "empty hands", st.condition].filter(Boolean).join(" · ");

// Each person in the sub-shot: how they look at this moment, the design picture chosen for it
// (by reading the scene), why, and a menu to draw them from another of their pictures.
function renderChoices(pkg, shot, sub) {
  if (!sub.choices || !sub.choices.length) return null;
  const url = `${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}/choice`;
  return h("div", { class: "choices" }, sub.choices.map((c) => {
    const select = h("select", { "aria-label": `Picture for ${c.name}`, onchange: (e) =>
      frameAction(url, null, { revision_id: pkg.revision_id, sub_shot: sub.index, character_id: c.character_id, image_id: e.target.value }) },
      c.candidates.map((x) => h("option", { value: x.image_id, selected: x.image_id === c.image_id, title: x.shows }, x.label.slice(0, 70))));
    // Opens on the chosen picture; the arrows step through the person's other pictures to compare.
    const pool = c.candidates.some((x) => x.image_id === c.image_id) ? c.candidates : [{ image_id: c.image_id, label: c.name, shows: c.shows }, ...c.candidates];
    const items = pool.map((x) => ({ src: `/v1/generated-images/${encodeURIComponent(x.image_id)}/content`,
      caption: `${c.name}${x.image_id === c.image_id ? " (drawn from)" : ""}`, title: x.label }));
    return h("div", { class: "choice" },
      h("button", { type: "button", class: "ref-open", "aria-label": `Enlarge ${c.name}'s picture`, title: `${c.shows || c.name} (click to enlarge)`,
                    onclick: () => enlarge(items, pool.findIndex((x) => x.image_id === c.image_id)) },
        h("img", { src: `/v1/generated-images/${encodeURIComponent(c.image_id)}/thumb`, alt: c.name, loading: "lazy" })),
      h("div", { class: "small" },
        h("div", {}, h("strong", {}, c.name), " ", h("span", { class: "muted" }, stateLine(c.state))),
        c.keep.length > 0 && h("div", { class: "muted" }, `The picture already has: ${c.keep.join(", ")}`),
        c.reason && h("div", { class: "muted" }, c.source === "user" ? "Chosen by you." : c.reason),
        h("label", { class: "muted" }, "Draw from ", select)));
  }));
}

function renderReading(pkg, shot) {
  const r = shot.reading;
  const label = r.status === "writing" ? "reading the scene…"
    : r.fresh ? `scene read${r.model ? ` by ${r.model}` : ""}${r.error ? " (the last re-read failed)" : ""}`
    : r.status === "failed" ? "scene reading failed"
    : "scene not read yet: read before the next written take";
  return h("div", { class: "take-head small" },
    h("span", { class: `chip ${r.fresh ? "ok" : r.status === "failed" ? "bad" : "info"}`, title: r.error || "" }, label),
    h("button", { type: "button", class: "btn ghost small", disabled: r.status === "writing",
      title: "Work out how each person looks in every sub-shot of this scene, and choose the closest design picture for each",
      onclick: (e) => frameAction(`${framesBase(pkg)}/scenes/${encodeURIComponent(r.scene_id)}/reading`, e.currentTarget, { revision_id: pkg.revision_id }) },
      r.fresh ? "Read the scene again" : "Read the scene"));
}

function renderSubShot(pkg, shot, sub, total) {
  const blocked = sub.missing.length > 0;
  return h("div", { class: "frame-block" },
    h("div", { class: "take-head" },
      h("h4", { style: "margin:0" }, total > 1 ? `[Shot ${sub.index}] at ${sub.timecode}` : "Keyframe"),
      h("span", { class: "muted small" }, `${secs(sub.start_ms)}–${secs(sub.end_ms)}`),
      h("span", { class: `chip ${sub.approved ? "ok" : "warn"}` }, sub.approved ? "approved ✓" : "approve one"),
      (sub.outdated || []).length > 0 && h("span", { class: "chip warn", title: sub.outdated.join("; ") }, "out of date")),
    (sub.outdated || []).length > 0 && h("p", { class: "small warn-text" }, `The approved keyframe is out of date: ${sub.outdated.join("; ")}. Draw a new take and approve it.`),
    total > 1 && h("p", { class: "small" }, h("strong", {}, sub.framing), sub.camera ? ` ${sub.camera}` : "", " — ", sub.action),
    total > 1 && h("p", { class: "small muted" }, sub.people_names.length
      ? `On screen: ${sub.people.map((id, i) => `${sub.people_names[i]}${sub.placement[id] ? ` (${sub.placement[id]})` : ""}`).join(", ")}`
      : "No one on screen",
      Object.keys(sub.off_screen || {}).length > 0 && ` · off frame: ${Object.entries(sub.off_screen).map(([id, side]) => `${(shot.people.find((p) => p.id === id) || { name: id }).name} (${side})`).join(", ")}`),
    sub.lines.length > 0 && h("ul", { class: "plain small sub-lines" }, sub.lines.map((x) =>
      h("li", {}, h("span", { class: "muted" }, `${(x.start_ms / 1000).toFixed(1)}–${(x.end_ms / 1000).toFixed(1)} s `), h("strong", {}, x.speaker), x.where !== "on screen" ? h("span", { class: "muted" }, ` (${x.where})`) : "", `: “${x.text}”`))),
    renderChoices(pkg, shot, sub),
    blocked
      ? h("p", { class: "small warn-text" }, `First: ${sub.missing.join("; ")}.`)
      : renderRefStrip(sub),
    sub.takes.map((t) => renderFrameTake(t, sub.takes)),
    !blocked && renderNewFrameTake(pkg, shot, sub));
}

function renderShotCard(pkg, shot) {
  const subs = shot.sub_shots;
  const done = subs.filter((s) => s.approved).length;
  return h("section", { class: "card design-card shot-card", id: `kf-${shot.shot_id}` },
    h("div", { class: "design-card-head" },
      h("h3", {}, shot.shot_id, h("span", { class: "muted small" }, ` · ${shot.place} · ${secs(shot.clip_ms)} clip`)),
      h("span", { class: "chips" },
        shot.intimacy && h("span", { class: "chip warn", title: "Scene staged with intimacy: clothed staging only, and never from a likeness" }, `intimacy: ${shot.intimacy}`),
        h("span", { class: `chip ${done === subs.length ? "ok" : "warn"}` }, `${done}/${subs.length} keyframes approved`))),
    h("p", { class: "small" }, h("strong", {}, shot.framing), " ", shot.camera),
    h("p", { class: "small" }, shot.action),
    h("p", { class: "small muted" }, shot.people.length ? `On screen: ${shot.people.map((p) => p.name).join(", ")}` : "No one on screen"),
    renderPlan(pkg, shot),
    renderReading(pkg, shot),
    subs.map((s) => renderSubShot(pkg, shot, s, subs.length)),
  );
}

function renderKeyframes(pkg) {
  if (frames.key !== framesKey(pkg)) loadFrames(pkg);
  const v = frames.key === framesKey(pkg) ? frames.view : null;
  if (v && v.busy && !frames.timer && !frames.loading) loadFrames(pkg);  // resume polling
  if (!v) return h("div", { class: "loading" }, frames.error || "Loading…");
  const shots = v.shots;
  const complete = shots.filter((s) => s.sub_shots.every((x) => x.approved)).length;
  const subTotal = shots.reduce((n, s) => n + s.sub_shots.length, 0);
  const subApproved = shots.reduce((n, s) => n + s.sub_shots.filter((x) => x.approved).length, 0);
  const blocked = shots.filter((s) => s.sub_shots[0].missing.length).length;
  const outdated = shots.filter((s) => s.sub_shots.some((x) => (x.outdated || []).length)).length;
  const episodes = [...new Set(shots.map((s) => s.episode_id))];
  const shown = frames.episode === "all" ? shots : shots.filter((s) => s.episode_id === frames.episode);
  const controls = h("section", { class: "card" },
    h("h3", {}, "Shot keyframes"),
    h("p", { class: "small muted" },
      "A shot is one video generation; a dynamic one holds a few cuts (sub-shots). Each shot's cuts are planned (by the text model, or by you), then each sub-shot gets a keyframe, the frame the video shows as that cut begins: drawn from the picked looks, set and props (and from the approved keyframe before it), checked against them, and revised automatically until a draw passes or the rounds run out. Approve one draw per sub-shot; the next sub-shot then starts. Only approved keyframes go into the export, with the plan's timestamps."),
    h("div", { class: "chips", style: "margin:8px 0" },
      h("span", { class: `chip ${complete === shots.length ? "ok" : "info"}` }, `${complete}/${shots.length} shots complete`),
      h("span", { class: "chip" }, `${subApproved}/${subTotal} sub-shot keyframes approved`),
      blocked > 0 && h("span", { class: "chip warn" }, `${blocked} waiting for design picks`),
      outdated > 0 && h("span", { class: "chip warn", title: "Approved keyframes drawn before a design change" }, `${outdated} with out-of-date keyframes`),
      v.busy > 0 && h("span", { class: "chip info" }, `${v.busy} running`)),
    h("div", { class: "design-row" },
      h("label", {}, "Draws ", numberSelect(frames.draws, 4, (n) => { frames.draws = n; }, "Draws per round")),
      h("label", {}, " Rounds ", numberSelect(frames.rounds, 3, (n) => { frames.rounds = n; }, "Automatic rounds")),
      // Another image provider for keyframes (e.g. Nano Banana), when the server has one.
      v && Object.keys(v.providers || {}).length > 1 && h("label", {}, " Drawn with ",
        h("select", { "aria-label": "Image provider", title: "The image model that draws new keyframe takes", onchange: (e) => { frames.provider = e.target.value; } },
          Object.entries(v.providers).map(([name, model]) =>
            h("option", { value: name, title: model, selected: (frames.provider || v.default_provider) === name }, providerName(name))))),
      h("button", { type: "button", class: "btn", onclick: async (e) => {
        const out = await frameAction(framesBase(pkg), e.currentTarget, { revision_id: pkg.revision_id, draws: frames.draws, max_rounds: frames.rounds, plan: true, provider: frames.provider });
        if (out) frames.notes = out.skipped.map((s) => `${s.shot_id}: ${s.reason}`);
        renderMain();
      } }, "Plan cuts and draw first keyframes for every shot"),
      outdated > 0 && h("button", { type: "button", class: "btn", title: "A new take for each shot's first out-of-date cut; approving it starts the next out-of-date cut", onclick: async (e) => {
        const out = await frameAction(framesBase(pkg), e.currentTarget, { revision_id: pkg.revision_id, draws: frames.draws, max_rounds: frames.rounds, outdated: true, provider: frames.provider });
        if (out) frames.notes = out.skipped.map((s) => `${s.shot_id}: ${s.reason}`);
        renderMain();
      } }, `Redraw out-of-date keyframes (${outdated} shots)`),
      episodes.length > 1 && h("select", { "aria-label": "Episode", onchange: (e) => { frames.episode = e.target.value; renderMain(); } },
        h("option", { value: "all", selected: frames.episode === "all" }, "All episodes"),
        episodes.map((ep) => h("option", { value: ep, selected: frames.episode === ep }, ep)))),
    frames.error && h("p", { class: "small warn-text" }, frames.error),
    frames.notes.length > 0 && stickyDetails("kf-notes", `${frames.notes.length} shot${frames.notes.length > 1 ? "s" : ""} not started`,
      h("ul", { class: "plain small" }, frames.notes.map((n) => h("li", {}, n)))),
  );
  return h("div", {}, controls, shown.map((s) => renderShotCard(pkg, s)));
}
