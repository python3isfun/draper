"use strict";

/*
 * Keyframes tab: each shot's sub-shot plan (the cuts inside its one video generation) and a
 * checked keyframe per sub-shot, drawn from the design picks, for video production. Uses
 * h(), api(), state, framesRender(), stickyDetails() and numberSelect() from app.js. Model
 * text is always inserted as text (h() uses text nodes).
 */

const frames = { key: null, view: null, error: "", notes: [], timer: null, loading: false,
  draws: 2, rounds: 3, provider: null, directions: {}, prompts: {}, plans: {}, planOpts: {}, history: {}, episode: "all",
  sel: {}, notes2: {}, compare: {}, diffs: new Map(), vdraws: {}, verr: {},
  send: { key: null, targets: null, error: "" } };  // "Send to a look": the draw it is open on

const framesKey = (pkg) => `${pkg.package_id}|${pkg.revision_id}`;
const framesBase = (pkg) => `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/keyframes`;
const drawUrl = (take, n, v = "thumb") =>
  `/v1/shot-takes/${encodeURIComponent(take.take_id)}/draws/${n}/${v}`;
const secs = (ms) => `${(ms / 1000).toFixed(1)} s`;

// The keyframe views also show on a look's or set state's page (library.js): there they
// redraw the library page, and the story is the one the page shows.
const framesInLib = () => ["chars", "sets"].includes(document.body.dataset.mode);
const framesShown = (key) => (framesInLib() ? frames.key === key : state.tab === "keyframes" && state.pkg && framesKey(state.pkg) === key);
function framesRender() {
  if (framesInLib()) { if (!typingIn($("#main"))) renderLibrary(); } else renderMain();
}

async function loadFrames(pkg) {
  const key = framesKey(pkg);
  if (frames.key !== key) Object.assign(frames, { key, view: null, error: "", notes: [] });
  frames.pkg = { package_id: pkg.package_id, revision_id: pkg.revision_id };
  clearTimeout(frames.timer);
  frames.timer = null;
  frames.loading = true;
  try {
    const [view, chats] = await Promise.all([
      api(`${framesBase(pkg)}?${new URLSearchParams({ revision_id: pkg.revision_id })}`),
      // The keyframe chats of this story (each sub-shot lists its own).
      api(`/v1/experiments?${new URLSearchParams({ package_id: pkg.package_id })}`).catch(() => []),
    ]);
    if (frames.key !== key) return;
    frames.chats = chats.filter((c) => c.target && c.target.shot_id);
    const changed = JSON.stringify(view) !== JSON.stringify(frames.view);
    frames.view = view;
    frames.error = "";
    if (changed && framesShown(key)) framesInLib() ? framesRender() : renderWhenNotSelecting();
  } catch (e) {
    if (frames.key !== key) return;
    frames.error = e.message;
    if (framesShown(key)) framesInLib() ? framesRender() : renderWhenNotSelecting();
  } finally {
    frames.loading = false;
  }
  if (frames.view && frames.view.busy > 0 && framesShown(key)) {
    frames.timer = setTimeout(() => loadFrames(frames.pkg), 2000);
  }
}

async function frameAction(url, button, body, method = "POST") {
  if (button) button.disabled = true;
  frames.error = "";
  let out = null;
  let failed = "";
  try {
    out = await api(url, { method, ...(body ? { body: JSON.stringify(body) } : {}) });
  } catch (e) {
    failed = e.message;
  }
  await loadFrames(frames.pkg || state.pkg);
  // The action's own error wins: reloading the board succeeds and would clear it.
  if (failed) frames.error = failed;
  framesRender();
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
    const finished = ["done", "failed", "paused"].includes(take.status);
    const failedItems = d.check ? d.check.items.filter((i) => !i.passed) : [];
    const label = `Draw ${d.n + 1}`;
    return h("figure", { class: `draw keyframe ${d.status}${approved ? " selected" : ""}` },
      d.status !== "done"
        ? h("div", { class: "draw-placeholder", title: d.error || "" }, "failed")
        : !finished
          ? h("div", { class: "draw-waiting-box" },
              h("img", { class: "draw-waiting", src: drawUrl(take, d.n), alt: label, loading: "lazy", title: "Approve once the round is finished" }),
              lookalikeTag(take.likeness), idTag(`take:${take.take_id}:${d.n}`))
          : h("button", { type: "button", class: "draw-pick", title: approved ? "Approved" : "Approve this keyframe",
              onclick: (e) => frameAction(`/v1/shot-takes/${encodeURIComponent(take.take_id)}/approve`, e.currentTarget, { n: d.n }) },
              h("img", { src: drawUrl(take, d.n), alt: label, loading: "lazy" }), lookalikeTag(take.likeness), idTag(`take:${take.take_id}:${d.n}`)),
      h("figcaption", {},
        h("div", { class: "keyframe-cap" }, `${label}${approved ? " ✓ approved" : ""}`, checkBadge(d)),
        d.status === "done" && h("a", { href: drawUrl(take, d.n, "content"), target: "_blank", rel: "noopener", class: "muted small" }, "open"),
        approved && h("button", { type: "button", class: "btn ghost small",
          onclick: (e) => frameAction(`/v1/shot-takes/${encodeURIComponent(take.take_id)}/approve`, e.currentTarget, null, "DELETE") }, "Withdraw"),
        finished && d.status === "done" && h("button", { type: "button", class: "btn ghost small",
          title: "Change this draw in a chat with the image model; a result's Use as keyframe brings it back",
          onclick: (e) => frameChat(take, `${take.take_id}:${d.n}`, e.currentTarget) }, "Develop in a chat"),
        finished && d.status === "done" && renderSendToDesign(take, d),
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

// A keyframe draw as a take of a look its shot wears (or of its set state): it goes beside
// that picture's drawn takes on its Characters or Sets page, as a pick only when asked.
function renderSendToDesign(take, d) {
  const key = `${take.take_id}:${d.n}`;
  const base = `/v1/shot-takes/${encodeURIComponent(take.take_id)}/draws/${d.n}`;
  const open = frames.send.key === key;
  const load = async () => {
    frames.send = { key, targets: null, error: "" };
    framesRender();
    try {
      frames.send.targets = await api(`${base}/design-targets`);
    } catch (e) {
      frames.send.error = e.message;
    }
    if (frames.send.key === key) framesRender();
  };
  const send = async (t, pick, button) => {
    button.disabled = true;
    try {
      await api(`${base}/to-design`, { method: "POST", body: JSON.stringify({ requirement_id: t.requirement_id, deliverable_id: t.deliverable_id, pick }) });
      await load();
    } catch (e) {
      frames.send.error = e.message;
      framesRender();
    }
  };
  const toggle = h("button", { type: "button", class: "btn ghost small", "aria-expanded": String(open),
    title: "Add this draw as a take of a look this shot wears, or of its set",
    onclick: () => { if (open) { frames.send = { key: null, targets: null, error: "" }; framesRender(); } else load(); } },
    open ? "Close" : "Send to a look…");
  if (!open) return toggle;
  const { targets, error } = frames.send;
  return h("div", { class: "send-design small" }, toggle,
    error && h("p", { class: "warn-text" }, error),
    !targets && !error && h("p", { class: "muted" }, "Loading…"),
    targets && !targets.length && h("p", { class: "muted" }, "Its shot has no look or set picture to send it to."),
    targets && h("ul", { class: "plain" }, targets.map((t) => {
      const page = t.lib_asset_id && `#lib=${encodeURIComponent(t.lib_asset_id)}&part=${encodeURIComponent(t.part_asset_id)}&slot=${encodeURIComponent(t.deliverable_id)}`;
      return h("li", {}, h("span", {}, t.label),
        t.sent_image_id
          ? [h("span", { class: "chip ok" }, t.picked ? "sent · picked" : "sent"),
             !t.picked && h("button", { type: "button", class: "btn ghost small", onclick: (e) => send(t, true, e.currentTarget) }, "Make it the pick")]
          : [h("button", { type: "button", class: "btn ghost small", title: "Add it beside the picture's takes; the pick stays", onclick: (e) => send(t, false, e.currentTarget) }, "Add as take"),
             h("button", { type: "button", class: "btn ghost small", title: "Add it and make it the picture's pick", onclick: (e) => send(t, true, e.currentTarget) }, "Add and pick")],
        page && h("a", { href: page, title: "Open the picture's takes" }, "open"));
    })));
}

// -- Developing a keyframe in a chat (docs/library-layers.md) ---------------------------

const frameLabel = (shotId, sub) => `${shotId}, keyframe ${sub}`;

// A chat for this sub-shot's keyframe, started from a draw ("<take>:<n>"; none: its
// approved draw, else its newest), opened in the workbench panel. Clicked again before
// writing anything, the same chat opens.
async function frameChat(take, fromFrame, button) {
  if (button) button.disabled = true;
  const label = frameLabel(take.shot_id, take.sub_shot);
  try {
    const target = { package_id: take.package_id, shot_id: take.shot_id, sub_shot: take.sub_shot, label };
    const s = await api("/v1/experiments", { method: "POST", body: JSON.stringify({ target, reuse_empty: true, from_frame: fromFrame }) });
    openBench("chat", s.session_id, label);
  } catch (e) {
    frames.error = e.message;
    framesRender();
  } finally {
    if (button) button.disabled = false;
  }
}

// "from chat #3": a take brought back from a chat, linked to it.
function frameSourceChip(take) {
  const m = (take.imported_from || "").match(/^experiment (\S+) #(\d+)$/);
  if (!m) return h("span", { class: "chip" }, take.imported_from);
  return h("a", { class: "chip", href: `#exp=${encodeURIComponent(m[1])}`, title: "The chat it came from",
    onclick: (e) => { if (e.button || e.metaKey || e.ctrlKey) return; e.preventDefault(); openBench("chat", m[1], frameLabel(take.shot_id, take.sub_shot)); } }, `from chat #${m[2]}`);
}

// The sub-shot's approved keyframe, large, at the top of its block: which round and draw
// (or chat image) it is and its check. With none approved, the newest finished draw, dashed.
function renderFrameHero(sub) {
  const [aid, an] = sub.approved || [];
  const approved = aid && sub.takes.find((t) => t.take_id === aid);
  const done = sub.takes.flatMap((t) => t.results.filter((d) => d.status === "done").map((d) => [t, d]));
  const newest = done.reduce((m, x) => (!m || x[0].created_at > m[0].created_at ? x : m), null);
  const [take, d] = approved ? [approved, approved.results.find((x) => x.n === an)] : newest || [];
  if (!take || !d) return null;
  const items = [{ src: drawUrl(take, d.n, "content"), caption: approved ? "Approved keyframe" : "Newest draw", likeness: take.likeness }];
  return h("div", { class: `pick-hero${approved ? "" : " unpicked"}` },
    h("button", { type: "button", class: "ref-open", "aria-label": "Enlarge the keyframe", onclick: () => enlarge(items, 0) },
      h("img", { src: drawUrl(take, d.n), alt: approved ? "approved keyframe" : "newest draw" }), lookalikeTag(take.likeness), idTag(`take:${take.take_id}:${d.n}`)),
    h("div", { class: "pick-hero-text" },
      h("span", { class: `chip ${approved ? "ok" : "warn"}` }, approved ? "✓ Approved keyframe" : "Not approved yet"),
      h("strong", {}, take.imported_from ? frameSourceChip(take) : `Round ${take.round}, draw ${d.n + 1}`),
      checkBadge(d),
      d.check && d.check.summary && h("p", { class: "small muted" }, d.check.summary),
      !approved && h("p", { class: "small muted" }, "The newest draw. Approve a draw below; the export and the next cut use the approved one."),
      (sub.outdated || []).length > 0 && approved && h("p", { class: "small warn-text" }, `Out of date: ${sub.outdated.join("; ")}`),
      h("a", { href: drawUrl(take, d.n, "content"), target: "_blank", rel: "noopener", class: "small" }, "Open full size")));
}

// "Develop in a chat" for the sub-shot (from its approved or newest draw) and its chats.
function renderFrameBench(pkg, shot, sub) {
  const mine = (frames.chats || []).filter((c) => c.target.shot_id === shot.shot_id && c.target.sub_shot === sub.index);
  const any = sub.takes.some((t) => t.results.some((d) => d.status === "done"));
  return h("div", { class: "design-row small" },
    h("button", { type: "button", class: "btn small", disabled: !any,
      title: any ? "Change the keyframe in a chat with the image model, starting from the approved (or newest) draw. A result's Use as keyframe brings it back as an approved take." : "Draw a take first",
      onclick: (e) => frameChat({ package_id: pkg.package_id, shot_id: shot.shot_id, sub_shot: sub.index }, null, e.currentTarget) }, "Develop in a chat"),
    mine.length > 0 && h("span", { class: "muted" }, "Chats:"),
    mine.slice(0, 6).map((c) => h("a", { href: `#exp=${encodeURIComponent(c.session_id)}`, title: `Updated ${timeAgo(c.updated_at)}`,
      onclick: (e) => { if (e.button || e.metaKey || e.ctrlKey) return; e.preventDefault(); openBench("chat", c.session_id, c.title); } },
      `${c.title}${c.messages ? ` (${c.messages})` : ""}`)));
}

function renderFrameTake(take, takes = []) {
  const busy = !["done", "failed", "paused"].includes(take.status);
  // The automatic round after this one, if it has started (same run, next round).
  const next = takes.find((t) => t.run_id === take.run_id && t.round === take.round + 1);
  const tid = encodeURIComponent(take.take_id);
  const uncheckable = take.status === "done" && take.results.some((d) => d.status === "done" && !d.check);
  return h("div", { class: "take" },
    h("div", { class: "take-head" },
      h("strong", {}, take.imported_from ? "From a chat" : `Round ${take.round}`),
      busy && h("span", { class: "chip info" }, take.pausing ? "pausing after this step…" : TAKE_STATUS[take.status]),
      take.status === "paused" && h("span", { class: "chip warn", title: "Stopped by you; Resume goes on with its remaining draws, its check and its rounds" }, "paused"),
      take.status === "failed" && h("span", { class: "chip bad", title: take.error || "" }, take.refused ? "refused" : "failed"),
      take.status === "done" && (take.results.some((d) => d.check && d.check.passed)
        ? h("span", { class: "chip ok" }, "passed")
        : next ? h("span", { class: "chip" }, `revised in round ${next.round}`)
        // The next automatic round is created as this one finishes, so it shows on the next poll.
        : h("span", { class: "chip warn" }, "no draw passed: your call")),
      take.direction && h("span", { class: "chip" }, `“${take.direction}”`),
      take.prompt_version && h("span", { class: "chip", title: "The prompt version this take draws" }, `prompt v${take.prompt_version}`),
      take.provider && take.provider !== "h3" && h("span", { class: "chip", title: "The image provider that drew this take" }, providerName(take.provider)),
      take.imported_from ? frameSourceChip(take) : take.prompt_source === "user" && h("span", { class: "chip" }, "your prompt"),
      take.warnings && take.warnings.length > 0 && h("span", { class: "chip warn", title: take.warnings.join("\n") }, `${take.warnings.length} rule finding${take.warnings.length > 1 ? "s" : ""}`),
      take.prompt_source === "template" && h("span", { class: "chip warn" }, "template prompt"),
      h("span", { class: "take-actions" },
        busy && !take.pausing && h("button", { type: "button", class: "btn ghost small", title: "Stop after the draw or prompt in progress; finished draws are kept",
          onclick: (e) => frameAction(`/v1/shot-takes/${tid}/pause`, e.currentTarget) }, "Pause"),
        take.status === "paused" && h("button", { type: "button", class: "btn ghost small", title: "Go on with the remaining draws, the check and the automatic rounds",
          onclick: (e) => frameAction(`/v1/shot-takes/${tid}/resume`, e.currentTarget) }, "Resume"),
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
    oninput: (e) => { frames.prompts[key] = e.target.value; sync(); },
    // ⌘/Ctrl+Enter: save the edit as the next version and draw it.
    onkeydown: (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !drawBtn.disabled) { e.preventDefault(); drawBtn.click(); } } });
  box.value = text;
  const note = h("input", { type: "text", placeholder: "What you changed (optional)",
    oninput: (e) => { frames.notes2[key] = e.target.value; } });
  note.value = frames.notes2[key] || "";
  const edited = () => sel ? box.value.trim() !== sel.prompt.trim() : box.value.trim() !== "";
  // The outcome of this editor's last save or draw, shown here (not only atop the board).
  const settle = () => { if (frames.error) frames.verr[key] = frames.error; else delete frames.verr[key]; };
  const saveBtn = h("button", { type: "button", class: "btn small", onclick: async (e) => {
    e.currentTarget.textContent = "Saving and checking…";
    const out = await frameAction(`${url}/prompts`, e.currentTarget, { revision_id: pkg.revision_id, sub_shot: sub.index, prompt: box.value, based_on: sel ? sel.v : null, note: frames.notes2[key] || "" });
    settle();
    if (out && !frames.error) { delete frames.prompts[key]; delete frames.notes2[key]; frames.sel[key] = out.v; }
    framesRender();
  } }, `Save as v${nextV}`);
  // Draws for this editor's button (defaults to the board's Draws setting).
  const draws = () => frames.vdraws[key] || frames.draws;
  const drawsSel = numberSelect(draws(), 4, (n) => { frames.vdraws[key] = n; sync(); }, "Draws for this prompt");
  const drawBtn = h("button", { type: "button", class: "btn small primary-outline", onclick: async (e) => {
    const body = { revision_id: pkg.revision_id, sub_shot: sub.index, draws: draws(), provider: frames.provider };
    if (edited()) Object.assign(body, { prompt: box.value, based_on: sel ? sel.v : null, note: frames.notes2[key] || "" });
    else body.prompt_version = sel.v;
    e.currentTarget.textContent = edited() ? "Saving, checking and drawing…" : "Starting…";
    await frameAction(url, e.currentTarget, body);
    settle();
    if (!frames.error) { delete frames.prompts[key]; delete frames.notes2[key]; delete frames.sel[key]; }
    framesRender();
  } }, "");
  const counter = h("span", { class: "muted small" });
  function sync() {
    const changed = edited();
    saveBtn.disabled = !changed || !box.value.trim();
    drawBtn.disabled = !box.value.trim();
    const n = draws();
    drawBtn.textContent = changed ? `Save as v${nextV} and draw ${n}` : sel ? `Draw v${sel.v} ×${n}` : `Draw ×${n}`;
    drawBtn.title = changed
      ? `Save your edit as v${nextV}${sel ? ` (from v${sel.v})` : ""} and draw it ${n} time${n > 1 ? "s" : ""} with the last take's seeds (⌘/Ctrl+Enter)`
      : `Draw v${sel ? sel.v : ""} as written, ${n} time${n > 1 ? "s" : ""}`;
    counter.textContent = `${box.value.length.toLocaleString()} characters${changed ? " · edited" : ""}`;
  }
  sync();
  const against = frames.compare[key];
  const base = against != null && against !== sel?.v && versions.find((v) => v.v === against);
  return stickyDetails(`kfv:${key}`, versions.length ? `Prompt · v${sel.v} · ${versions.length} version${versions.length > 1 ? "s" : ""}` : "Prompt",
    versions.length > 0 && h("div", { class: "version-row" },
      h("label", { class: "small" }, "Version ",
        h("select", { "aria-label": "Prompt version", onchange: (e) => { frames.sel[key] = Number(e.target.value); delete frames.prompts[key]; delete frames.compare[key]; framesRender(); } },
          [...versions].reverse().map((v) => h("option", { value: v.v, selected: v.v === sel.v },
            `v${v.v} · ${SOURCE_LABEL[v.source] || v.source}${v.based_on ? ` from v${v.based_on}` : ""} · ${timeAgo(v.created_at)}`)))),
      versions.length > 1 && h("label", { class: "small" }, " compare with ",
        h("select", { "aria-label": "Compare with", onchange: (e) => { frames.compare[key] = e.target.value === "" ? null : Number(e.target.value); framesRender(); } },
          h("option", { value: "" }, "—"),
          versions.filter((v) => v.v !== sel.v).reverse().map((v) => h("option", { value: v.v, selected: v.v === against }, `v${v.v}`))))),
    sel && sel.note && h("p", { class: "small muted" }, sel.note),
    sel && sel.findings.length > 0 && h("div", { class: "small warn-text" }, h("strong", {}, `v${sel.v} breaks the H3 image rules:`),
      h("ul", { class: "plain" }, sel.findings.map((f) => h("li", {}, f)))),
    base && h("div", { class: "prompt-diff small" }, h("div", { class: "muted" }, `Changes from v${base.v} to v${sel.v}:`), diffNodes(wordDiff(base.prompt, sel.prompt))),
    box,
    h("div", { class: "prompt-edit-actions" }, note),
    h("div", { class: "prompt-edit-actions" }, saveBtn, h("label", { class: "small muted", style: "display:inline-flex;align-items:center;gap:4px" }, "draws", drawsSel), drawBtn,
      draft != null && h("button", { type: "button", class: "btn ghost small", onclick: () => { delete frames.prompts[key]; framesRender(); } }, "Discard edits"),
      counter),
    frames.verr[key] && h("p", { class: "small warn-text", role: "alert" }, `Not saved or drawn: ${frames.verr[key]}`),
    h("p", { class: "muted small" }, "Edit any version and press Save and draw (⌘/Ctrl+Enter): it is saved as the next version and drawn right away. Saving keeps every version. Drawing a version uses the last take's seeds, so its draws compare one for one; a version is drawn as written, never rewritten. To restore an old version, pick it and draw it, or save it again as the newest."));
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
        if (!frames.error) { frames.directions[key] = ""; framesRender(); }
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
  const items = sub.references.map((r, i) => ({ src: refSrc(r, "content"), caption: `<Picture ${i + 1}>`, title: r.role, likeness: r.likeness }));
  return h("div", { class: "ref-strip" }, sub.references.map((r, i) =>
    h("figure", { class: "ref", title: `${r.role} (click to enlarge)` },
      h("button", { type: "button", class: "ref-open", "aria-label": `Enlarge <Picture ${i + 1}>: ${r.role}`, onclick: () => enlarge(items, i) },
        h("img", { src: refSrc(r), alt: r.role, loading: "lazy" }), lookalikeTag(r.likeness), idTag(r.ref_id.startsWith("frame:") ? `take:${r.ref_id.slice(6)}` : `design:${r.ref_id}`)),
      h("figcaption", {}, `<Picture ${i + 1}>`))));
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
        i > 0 && h("button", { type: "button", class: "btn ghost small", onclick: () => { rows.splice(i, 1); framesRender(); } }, "Remove")),
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
        framesRender();
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
        if (!frames.error) { delete frames.plans[key]; framesRender(); }
      } }, "Save plan"),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { delete frames.plans[key]; framesRender(); } }, "Cancel"),
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
        planSettings(pkg, shot),
        h("button", { type: "button", class: "btn ghost small", disabled: shot.busy, title: "Ask the text model where the cuts go, at this shot's pace and cut count",
          onclick: async (e) => {
            const o = planOpts(pkg, shot);
            await frameAction(`${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}/plan`, e.currentTarget,
              { revision_id: pkg.revision_id, pace: o.pace || null, cuts: o.cuts ? Number(o.cuts) : null });
            if (!frames.error) delete frames.planOpts[historyKey(pkg, shot)];
          } }, "Plan cuts"),
        p.history > 0 && h("button", { type: "button", class: "btn ghost small", "aria-expanded": String(!!frames.history[historyKey(pkg, shot)]),
          title: "Earlier cut plans of this shot, each with the keyframes approved for it", onclick: () => toggleHistory(pkg, shot) },
          `History (${p.history})`),
        h("button", { type: "button", class: "btn ghost small", disabled: shot.busy, onclick: () => {
          frames.plans[shot.shot_id] = { clip_s: Math.min(15, Math.max(4, Math.ceil(shot.clip_ms / 1000))), rows: shot.sub_shots.map((s) => ({ start_s: s.start_ms / 1000, framing: s.framing, camera: s.camera, action: s.action, pos: positionsOf(s) })) };
          framesRender();
        } }, "Edit"))),
    p.error && h("p", { class: "small warn-text" }, p.error),
    p.outdated && h("p", { class: "small warn-text" }, `This plan was written ${p.outdated}.`),
    p.problems && p.problems.length > 0 && h("div", { class: "small warn-text" },
      h("strong", {}, "Not a clean spec for video yet:"),
      h("ul", { class: "plain" }, p.problems.map((x) => h("li", {}, x)))),
    p.notes && h("p", { class: "small muted" }, p.notes),
    editing && renderPlanEditor(pkg, shot),
    renderHistory(pkg, shot),
  );
}

// A shot's own pace and cut count for its next plan (ShotPlan.pace, .cuts): what its plan
// has, until changed here (kept per story revision, like the history); sent with "Plan cuts".
const PACE_LABELS = { brisk: "Brisk", measured: "Measured", slow_burn: "Slow burn" };

function planOpts(pkg, shot) {
  return frames.planOpts[historyKey(pkg, shot)] || { pace: shot.plan.pace || "", cuts: shot.plan.cuts ? String(shot.plan.cuts) : "" };
}

function planSettings(pkg, shot) {
  const o = planOpts(pkg, shot);
  const set = (k) => (e) => { frames.planOpts[historyKey(pkg, shot)] = { ...planOpts(pkg, shot), [k]: e.target.value }; };
  const story = PACE_LABELS[shot.plan.story_pace] || "story's";
  return [
    h("select", { class: "plan-setting", "aria-label": "Pace of this shot", disabled: shot.busy, onchange: set("pace"),
      title: "This shot's pace for its next plan: how long a reaction may hold, how often the camera moves" },
      h("option", { value: "", selected: !o.pace }, `Pace: story's (${story})`),
      Object.entries(PACE_LABELS).map(([v, label]) => h("option", { value: v, selected: o.pace === v }, `Pace: ${label}`))),
    h("select", { class: "plan-setting", "aria-label": "Sub-shots in this shot", disabled: shot.busy, onchange: set("cuts"),
      title: "How many sub-shots the next plan cuts this shot into (at most 3, each at least 1 s)" },
      h("option", { value: "", selected: !o.cuts }, "Cuts: auto"),
      [1, 2, 3].map((n) => h("option", { value: String(n), selected: o.cuts === String(n) }, `Cuts: ${n}`))),
  ];
}

// A shot's earlier cut plans (shots/history.py), newest first: loaded when opened, and
// again whenever the board's count for the shot changes (a re-plan, an edit, a restore).
// Kept per story revision, so a panel never shows another story's plans.
const historyKey = (pkg, shot) => `${framesKey(pkg)}|${shot.shot_id}`;

function toggleHistory(pkg, shot) {
  const key = historyKey(pkg, shot);
  if (frames.history[key]) { delete frames.history[key]; framesRender(); return; }
  loadHistory(pkg, shot);
}

async function loadHistory(pkg, shot) {
  const key = historyKey(pkg, shot);
  const count = shot.plan.history;
  frames.history[key] = { loading: true, count };
  framesRender();
  let next;
  try {
    const list = await api(`${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}/plans?revision_id=${encodeURIComponent(pkg.revision_id)}`);
    next = { list, count };
  } catch (err) {
    next = { error: err.message, count };
  }
  if (frames.history[key]) { frames.history[key] = next; framesRender(); }  // unless closed meanwhile
}

const REPLACED_BY = { model: "a re-plan", user: "your edit", restore: "a restore" };

function renderHistory(pkg, shot) {
  const open = frames.history[historyKey(pkg, shot)];
  if (!open) return null;
  if (!open.loading && open.count !== shot.plan.history) {
    if (!shot.plan.history) { delete frames.history[historyKey(pkg, shot)]; return null; }
    queueMicrotask(() => loadHistory(pkg, shot));  // not while rendering
  }
  if (open.loading) return h("p", { class: "small muted" }, "Loading earlier plans…");
  if (open.error) return h("p", { class: "small warn-text" }, open.error);
  return h("div", { class: "plan-history" }, open.list.map((v) => {
    const kept = Object.keys(v.approved).length;
    const made = v.plan.source === "user" ? "your plan" : v.plan.source === "model" ? `planned by ${v.plan.model || "the text model"}` : "the whole shot as one sub-shot";
    return h("div", { class: "plan-version" },
      h("div", { class: "take-head small" },
        h("strong", {}, `Plan ${v.version}`),
        h("span", { class: "muted" }, `${made} · replaced by ${REPLACED_BY[v.replaced_by] || v.replaced_by} ${timeAgo(v.replaced_at)} · ${kept} approved keyframe${kept === 1 ? "" : "s"}`),
        h("button", { type: "button", class: "btn ghost small", disabled: shot.busy,
          title: "Make this the shot's plan again: its keyframes come back, and the plan in force is kept in this history",
          onclick: async (e) => {
            await frameAction(`${framesBase(pkg)}/${encodeURIComponent(shot.shot_id)}/plans/${v.version}/restore`, e.currentTarget, { revision_id: pkg.revision_id });
            // A restore swaps one plan for another (the count stays): reload the list.
            if (!frames.error) loadHistory(pkg, shot); else framesRender();
          } }, "Restore")),
      h("ol", { class: "plain small" }, v.plan.sub_shots.map((s) => h("li", {},
        h("span", { class: "muted" }, `${(s.start_ms / 1000).toFixed(1)}-${(s.end_ms / 1000).toFixed(1)} s · `),
        [s.framing, s.camera, s.action].filter(Boolean).join(" · ")))),
      v.plan.notes && h("p", { class: "small muted" }, v.plan.notes));
  }));
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
    const pool = c.candidates.some((x) => x.image_id === c.image_id) ? c.candidates : [{ image_id: c.image_id, label: c.name, shows: c.shows, likeness: c.likeness }, ...c.candidates];
    const items = pool.map((x) => ({ src: `/v1/generated-images/${encodeURIComponent(x.image_id)}/content`,
      caption: `${c.name}${x.image_id === c.image_id ? " (drawn from)" : ""}`, title: x.label, likeness: x.likeness }));
    return h("div", { class: "choice" },
      h("button", { type: "button", class: "ref-open", "aria-label": `Enlarge ${c.name}'s picture`, title: `${c.shows || c.name} (click to enlarge)`,
                    onclick: () => enlarge(items, pool.findIndex((x) => x.image_id === c.image_id)) },
        h("img", { src: `/v1/generated-images/${encodeURIComponent(c.image_id)}/thumb`, alt: c.name, loading: "lazy" }), lookalikeTag(c.likeness), idTag(`design:${c.image_id}`)),
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
    renderFrameHero(sub),
    renderFrameBench(pkg, shot, sub),
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
        framesRender();
      } }, "Plan cuts and draw first keyframes for every shot"),
      outdated > 0 && h("button", { type: "button", class: "btn", title: "A new take for each shot's first out-of-date cut; approving it starts the next out-of-date cut", onclick: async (e) => {
        const out = await frameAction(framesBase(pkg), e.currentTarget, { revision_id: pkg.revision_id, draws: frames.draws, max_rounds: frames.rounds, outdated: true, provider: frames.provider });
        if (out) frames.notes = out.skipped.map((s) => `${s.shot_id}: ${s.reason}`);
        framesRender();
      } }, `Redraw out-of-date keyframes (${outdated} shots)`),
      episodes.length > 1 && h("select", { "aria-label": "Episode", onchange: (e) => { frames.episode = e.target.value; framesRender(); } },
        h("option", { value: "all", selected: frames.episode === "all" }, "All episodes"),
        episodes.map((ep) => h("option", { value: ep, selected: frames.episode === ep }, ep)))),
    frames.error && h("p", { class: "small warn-text" }, frames.error),
    frames.notes.length > 0 && stickyDetails("kf-notes", `${frames.notes.length} shot${frames.notes.length > 1 ? "s" : ""} not started`,
      h("ul", { class: "plain small" }, frames.notes.map((n) => h("li", {}, n)))),
  );
  return h("div", {}, controls, shown.map((s) => renderShotCard(pkg, s)));
}
