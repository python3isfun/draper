"use strict";

/*
 * Experiment tab: a chat with the image model. Each message changes the current image
 * (sent as <Picture 1>, with any attached images after it). Uses h(), api(), $, timeAgo()
 * and uploadFile() from app.js. Hash: #exp or #exp=<session_id>.
 */

const exp = {
  sessions: [],
  sid: null,
  view: null,  // { session, turns, sizes }
  timer: null,
  mounted: null,  // session id the main view is built for
  el: {},  // persistent elements of the mounted view
  // Composer state survives redraws and session switches (per session).
  drafts: {},
  error: "",
  sending: {},  // session id -> label of the send in progress ("Uploading 1 of 2…")
  renaming: false,  // the title is being edited: polls leave the header alone
  labels: {},  // image numbers of the open chat, by reference (see labelsOf)
  likeness: {},  // results that show a real person's likeness, by reference
  used: {},  // results made a design picture's pick from this chat, by reference
  notice: "",  // what Use as pick did (shown under the chat's title)
  // The chats of the picture the open chat is for, newest first: its history row.
  siblings: { key: null, list: [] },
  // The workbench panel's element when the chat is open over a character or set page
  // (bench.js); null: the Experiment tab, in #main.
  host: null,
};

const expRoot = () => exp.host || $("#main");
// The panel's scrolling body, or null for the page (the Experiment tab).
const expScroller = () => exp.host && exp.host.closest(".bench-body");

const EXP_POLL_MS = 1000;

// Restrictive mode: a top-of-page switch, off by default, remembered in this browser.
const restrictiveBox = document.getElementById("restrictive");
try { restrictiveBox.checked = localStorage.getItem("drama.restrictive") === "1"; } catch { /* storage blocked */ }
restrictiveBox.addEventListener("change", () => {
  try { localStorage.setItem("drama.restrictive", restrictiveBox.checked ? "1" : "0"); } catch { /* storage blocked */ }
});
const expBusy = (v) => v && v.turns.some((t) => t.status === "pending" || t.status === "running");
// Whether the open chat is still on screen: the Experiment tab, or the panel it is in.
const inExp = () => (exp.host ? exp.host.isConnected : document.body.dataset.mode === "exp");

function expDraft(sid) {
  return (exp.drafts[sid] ||= { text: "", files: [], mode: "rewrite", draws: 1 });
}

function refUrl(ref, variant = "thumb") {
  if (!ref) return null;
  if (ref.startsWith("asset:")) return `/v1/source-assets/${encodeURIComponent(ref.slice(6))}/content`;
  const [, tid, n] = ref.split(":");
  return `/v1/experiment-turns/${encodeURIComponent(tid)}/results/${n}/${variant}`;
}

function goExp(sid) {
  location.hash = sid ? `exp=${encodeURIComponent(sid)}` : "exp";
}

// Mode switch (Drama / Experiment) -----------------------------------------------

function setMode(mode) {
  document.body.dataset.mode = mode;
  $("#drama-side").hidden = mode !== "drama";
  $("#exp-side").hidden = mode !== "exp";
  $("#face-side").hidden = mode !== "face";
  $("#lib-side").hidden = mode !== "chars" && mode !== "sets";
  for (const a of document.querySelectorAll(".modes a")) {
    const on = a.dataset.mode === mode;
    a.classList.toggle("active", on);
    a.setAttribute("aria-current", on ? "page" : "false");
  }
}

// Leaving the tab: forget the open chat, so a response still in flight is recognized as
// stale (loadExpSession compares session ids) and can't draw over the Drama view.
function stopExp() {
  clearTimeout(exp.timer);
  exp.timer = null;
  exp.mounted = null;
  exp.sid = null;
  exp.view = null;
  exp.renaming = false;
}

async function expRoute(sid) {
  setMode("exp");
  clearTimeout(exp.timer);
  if (exp.sid !== sid) {
    exp.sid = sid;
    exp.view = null;
    exp.mounted = null;
    exp.error = "";
    exp.renaming = false;
    exp.used = {};
    exp.notice = "";
  }
  await loadExpSessions();
  if (!sid) {
    exp.mounted = null;
    $("#main").replaceChildren(
      h("div", { class: "card placeholder" },
        h("h2", {}, "Experiment with the image model"),
        h("p", {}, "Start a chat, upload a base image, then describe changes in plain words. Each message edits the current image with MiniMax-H3-Image; GPT rewrites your message into a full H3 prompt first, or switch to Exact prompt to send your own wording."),
        h("button", { type: "button", class: "btn primary-outline", onclick: newExpSession }, "New chat"),
      ),
    );
    return;
  }
  if (!exp.view) $("#main").replaceChildren(h("div", { class: "loading" }, "Loading…"));
  await loadExpSession();
}

async function loadExpSessions() {
  try {
    exp.sessions = await api("/v1/experiments?kind=chat");
  } catch {
    exp.sessions = [];
  }
  renderExpSessions();
}

function renderExpSessions() {
  const list = $("#exp-list");
  if (!exp.sessions.length) {
    list.replaceChildren(h("li", { class: "empty" }, "No chats yet."));
    return;
  }
  const open = (sid) => (ev) => {
    if (ev.type === "keydown" && ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    goExp(sid);
  };
  list.replaceChildren(...exp.sessions.map((s) =>
    h("li", {},
      h("div", {
        class: `job-item${s.session_id === exp.sid ? " active" : ""}`, role: "link", tabindex: "0",
        "aria-current": s.session_id === exp.sid ? "page" : null,
        onclick: open(s.session_id), onkeydown: open(s.session_id),
      },
        h("div", { class: "seed" }, s.title),
        h("div", { class: "meta" }, h("span", { class: "muted small" }, timeAgo(s.updated_at))),
      ),
    )));
}

async function newExpSession() {
  try {
    const s = await api("/v1/experiments", { method: "POST", body: "{}" });
    goExp(s.session_id);
  } catch (e) {
    exp.error = e.message;
  }
}

async function loadExpSession() {
  const sid = exp.sid;
  try {
    const v = await api(`/v1/experiments/${encodeURIComponent(sid)}`);
    if (exp.sid !== sid || !inExp()) return;  // switched chats or tabs meanwhile
    const titleChanged = exp.view?.session.title !== v.session.title;
    const turnsChanged = exp.view?.turns.length !== v.turns.length;
    exp.view = v;
    renderExp();
    if (titleChanged) loadExpSessions();
    if (v.session.target && (titleChanged || turnsChanged)) loadExpSiblings(v.session.target);
  } catch (e) {
    if (exp.sid !== sid || !inExp()) return;
    exp.mounted = null;
    expRoot().replaceChildren(h("div", { class: "card placeholder" }, h("p", { class: "form-error" }, e.message)));
    return;
  }
  clearTimeout(exp.timer);
  if (expBusy(exp.view)) exp.timer = setTimeout(loadExpSession, EXP_POLL_MS);
}

// A picture's chats are saved, each one: the history row lists them to switch between, with
// New chat. The list is the server's (GET /v1/experiments for that picture).
const targetKey = (t) => `${t.package_id}|${t.requirement_id}|${t.deliverable_id}`;

async function loadExpSiblings(target) {
  const key = targetKey(target);
  const q = new URLSearchParams({ kind: "chat", package_id: target.package_id, requirement_id: target.requirement_id, deliverable_id: target.deliverable_id });
  let list;
  try {
    list = await api(`/v1/experiments?${q}`);
  } catch {
    return;  // the row keeps what it had
  }
  exp.siblings = { key, list };
  const t = exp.view?.session.target;
  if (t && targetKey(t) === key && exp.el.header && !exp.renaming) renderExpHeader(exp.view);
}

// Open another chat in the same place: the panel, or the full page.
function switchExpChat(s) {
  if (s.session_id === exp.sid) return;
  if (exp.host) openBench("chat", s.session_id, (s.target && s.target.label) || s.title);
  else goExp(s.session_id);
}

// A new chat for the picture, starting from its current pick (an untouched one is reused).
async function newChatFor(target, button) {
  button.disabled = true;
  try {
    const s = await api("/v1/experiments", { method: "POST", body: JSON.stringify({ target, reuse_empty: true }) });
    if (s.session_id === exp.sid) { button.disabled = false; return; }
    switchExpChat(s);
    if (typeof libLoadScratch === "function" && lib.kind) libLoadScratch();  // the page's list of chats
  } catch (e) {
    button.disabled = false;
    exp.error = e.message;
    renderExp();
  }
}

function renderExpHistory(s) {
  const t = s.target;
  const list = exp.siblings.key === targetKey(t) ? exp.siblings.list : [];
  const shown = list.some((x) => x.session_id === s.session_id) ? list : [{ ...s, messages: exp.view.turns.filter((x) => !["pick", "import"].includes(x.mode)).length }, ...list];
  return h("div", { class: "exp-history", role: "navigation", "aria-label": "Chats for this picture" },
    h("span", { class: "muted small" }, `Chats for this picture (${shown.length}):`),
    h("div", { class: "exp-history-list" }, shown.map((x) => {
      const on = x.session_id === s.session_id;
      return h("button", {
        type: "button", class: `exp-history-item${on ? " active" : ""}`, "aria-current": on ? "true" : null,
        title: `${x.title}, updated ${timeAgo(x.updated_at)}`, onclick: () => switchExpChat(x),
      },
        h("span", { class: "exp-history-title" }, x.title),
        h("span", { class: "muted small" }, `${x.messages ? `${x.messages} message${x.messages === 1 ? "" : "s"}` : "no messages yet"} · ${timeAgo(x.updated_at)}`));
    })),
    h("button", { type: "button", class: "btn small", title: "Start another chat from the picture's current pick; this one stays saved", onclick: (e) => newChatFor(t, e.currentTarget) }, "New chat"));
}

async function patchExp(body) {
  const sid = exp.sid;
  let session = null, error = "";
  try {
    session = await api(`/v1/experiments/${encodeURIComponent(sid)}`, { method: "PATCH", body: JSON.stringify(body) });
  } catch (e) {
    error = e.message;
  }
  if (exp.sid !== sid || !exp.view) return;
  if (session) exp.view.session = session;
  exp.error = error;
  renderExp();
}

// Main view ------------------------------------------------------------------------

// Image numbers (#7) by reference string ("asset:<id>", "turn:<id>:<n>").
function labelsOf(v) {
  const out = {};
  for (const t of v.turns) {
    t.attachments.forEach((a, i) => { if (t.attachment_labels[i] != null) out[`asset:${a}`] = t.attachment_labels[i]; });
    t.results.forEach((r, n) => { if (r.label != null) out[`turn:${t.turn_id}:${n}`] = r.label; });
  }
  return out;
}

// n: a chat number (3, written "#3") or a global one ("@1234").
function insertMention(n) {
  const el = exp.el.text, d = expDraft(exp.sid);
  const at = el.selectionStart ?? el.value.length;
  const before = el.value.slice(0, at), after = el.value.slice(el.selectionEnd ?? at);
  const token = `${before && !/\s$/.test(before) ? " " : ""}${typeof n === "number" ? `#${n}` : n} `;
  el.value = before + token + after;
  d.text = el.value;
  el.focus();
  el.selectionStart = el.selectionEnd = before.length + token.length;
  updateSendButton();
}

function renderExp() {
  const v = exp.view;
  if (!v) return;
  exp.labels = labelsOf(v);
  exp.likeness = {};
  for (const t of v.turns) t.results.forEach((_, n) => { if (t.likeness) exp.likeness[`turn:${t.turn_id}:${n}`] = true; });
  if (exp.mounted !== v.session.session_id) mountExp(v);
  const sc = expScroller();
  const nearBottom = sc ? sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 120
    : window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  const before = exp.el.transcript.childElementCount;
  renderExpHeader(v);
  exp.el.transcript.replaceChildren(...v.turns.map(renderTurn));
  if (!v.turns.length) {
    exp.el.transcript.replaceChildren(h("p", { class: "muted exp-empty" },
      "Attach a base image below (or describe a new image), then tell the model what to change."));
  }
  renderComposer(v);
  if (nearBottom || before !== exp.el.transcript.childElementCount) {
    if (sc) sc.scrollTop = sc.scrollHeight;
    else window.scrollTo(0, document.body.scrollHeight);
  }
}

function mountExp(v) {
  exp.mounted = v.session.session_id;
  const d = expDraft(exp.sid);
  const el = exp.el = {};
  el.header = h("div", { class: "card exp-header" });
  el.transcript = h("div", { class: "exp-transcript" });
  el.text = h("textarea", {
    rows: 3, "aria-label": "Message",
    placeholder: "Describe the change, e.g. \"gather her hair into a low bun\". Refer to earlier images by number: \"go back to #3 and add a hat\", \"hair like #4\"; any picture in the app by its @number: \"her jacket from @1234\". (Enter to send, Shift+Enter for a new line)",
    oninput: (e) => { d.text = e.target.value; updateSendButton(); },
    onkeydown: (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendExp();
      }
    },
  });
  el.text.value = d.text;
  el.file = h("input", {
    type: "file", accept: "image/jpeg,image/png,image/webp", multiple: true, hidden: true,
    onchange: (e) => { addExpFiles([...e.target.files]); e.target.value = ""; },
  });
  el.current = h("div", { class: "exp-current" });
  el.files = h("div", { class: "exp-files" });
  el.options = h("div", { class: "exp-options" });
  el.send = h("button", { type: "button", class: "btn primary exp-send", onclick: sendExp }, "Send");
  el.error = h("p", { class: "form-error", role: "alert" });
  el.composer = h("div", {
    class: "card exp-composer",
    ondragover: (e) => { e.preventDefault(); el.composer.classList.add("over"); },
    ondragleave: () => el.composer.classList.remove("over"),
    ondrop: (e) => { e.preventDefault(); el.composer.classList.remove("over"); addExpFiles([...e.dataTransfer.files]); },
  },
    el.current, el.files, el.text,
    h("div", { class: "exp-actions" },
      h("button", { type: "button", class: "btn small", onclick: () => el.file.click() }, "Attach image"),
      el.file, el.options, el.send),
    el.error,
  );
  expRoot().replaceChildren(h("div", { class: "exp" }, el.header, el.transcript, el.composer));
}

function renderExpHeader(v) {
  if (exp.renaming) return;  // a redraw would throw away the title being typed
  const s = v.session;
  const sizeSelect = h("select", {
    "aria-label": "Image size",
    onchange: (e) => patchExp({ size: e.target.value }),
  },
    h("option", { value: "auto", selected: s.size === "auto" }, "Size: match the current image"),
    Object.entries(v.sizes).map(([aspect, size]) => h("option", { value: size, selected: s.size === size }, `${aspect} · ${size}`)),
  );
  const rename = () => {
    const input = h("input", { value: s.title, "aria-label": "Chat title", maxlength: "120" });
    let done = false;
    const finish = (keep) => {
      if (done) return;
      done = true;
      exp.renaming = false;
      const t = input.value.trim();
      if (keep && t && t !== s.title) patchExp({ title: t }).then(loadExpSessions);
      else renderExp();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
    exp.renaming = true;
    exp.el.header.querySelector(".exp-title").replaceWith(input);
    input.focus();
    input.select();
  };
  const target = s.target;
  const job = target && jobOf(target.package_id);
  exp.el.header.replaceChildren(
    target && h("p", { class: "small exp-target" }, "For ",
      job ? h("a", { href: `#job=${encodeURIComponent(job.job_id)}&tab=casting` }, h("strong", {}, target.label || "a design picture")) : h("strong", {}, target.label || "a design picture"),
      ". Use as pick on a result makes it that picture's pick. ",
      h("button", { type: "button", class: "btn ghost small", title: "Move this chat back to Scratch, tied to no picture", onclick: () => unlinkSession(s.session_id, () => loadExpSession()) }, "Unlink")),
    exp.notice && h("p", { class: "small hint" }, exp.notice),
    target && renderExpHistory(s),
    h("div", { class: "exp-head-row" },
      h("h2", { class: "exp-title", title: "Click to rename", onclick: rename }, s.title),
      // The pick it started from is not a message (as in the history row).
      ((n) => h("span", { class: "muted small" }, n ? `${n} message${n === 1 ? "" : "s"}` : "no messages yet"))(v.turns.filter((t) => !["pick", "import"].includes(t.mode)).length),
      h("div", { class: "exp-head-actions" },
        sizeSelect,
        h("button", { type: "button", class: "btn ghost small", onclick: deleteExp, disabled: expBusy(v) }, "Delete chat"),
      ),
    ),
  );
}

async function deleteExp() {
  // No confirm(): browser dialogs block automation; deleting needs a second click instead.
  const btn = exp.el.header.querySelector(".exp-head-actions .btn");
  if (btn.dataset.armed !== "1") {
    btn.dataset.armed = "1";
    btn.textContent = "Click again to delete";
    setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ""; btn.textContent = "Delete chat"; } }, 4000);
    return;
  }
  try {
    await api(`/v1/experiments/${encodeURIComponent(exp.sid)}`, { method: "DELETE" });
    delete exp.drafts[exp.sid];
    if (exp.host) closeBench();
    else goExp(null);
  } catch (e) {
    exp.error = e.message;
    renderExp();
  }
}

// Transcript -----------------------------------------------------------------------

const TURN_STATUS = { pending: "queued", running: "working", done: "done", failed: "failed" };

function renderTurn(t) {
  const current = exp.view.session.current;
  const user = h("div", { class: "exp-msg user" },
    h("div", { class: "bubble" },
      t.text && h("div", { class: `exp-text${t.mode === "exact" ? " mono" : ""}` }, t.text),
      t.attachments.length > 0 && h("div", { class: "exp-thumbs" },
        t.attachments.map((a) => expImage(`asset:${a}`, current, "Uploaded image"))),
      h("div", { class: "muted small exp-meta" },
        t.mode === "exact" ? "Exact prompt" : t.mode === "upload" ? "Upload" : "Rewritten by GPT",
        ` · ${timeAgo(t.created_at)}`),
    ),
  );
  if (t.mode === "pick") {
    // A chat started from a design picture's pick (docs/workbench.md): #1 is that picture.
    return h("div", { class: "exp-turn" },
      h("div", { class: "exp-msg bot" }, h("div", { class: "bubble" },
        h("p", { class: "muted small" }, t.text, " Your next message changes it."),
        h("div", { class: "exp-results" }, t.results.map((r, n) => expImage(`turn:${t.turn_id}:${n}`, current, "The current pick", null, t.from_image ? `design:${t.from_image}` : undefined))),
        t.prompt && h("details", { class: "exp-prompt" },
          h("summary", {}, "The prompt that made it"),
          h("pre", {}, t.prompt),
          h("button", { type: "button", class: "btn small", onclick: () => usePrompt(t.prompt) }, "Edit this prompt")))));
  }
  if (t.mode === "import") {
    // An image a message brought in by its global number (@1234, docs/image-ids.md).
    return h("div", { class: "exp-turn" },
      h("div", { class: "exp-msg bot" }, h("div", { class: "bubble" },
        h("p", { class: "muted small" }, t.text),
        h("div", { class: "exp-results" }, t.results.map((r, n) => expImage(`turn:${t.turn_id}:${n}`, current, "Brought in", null, t.import_ref || undefined))),
        t.prompt && h("details", { class: "exp-prompt" },
          h("summary", {}, "The prompt that made it"),
          h("pre", {}, t.prompt),
          h("button", { type: "button", class: "btn small", onclick: () => usePrompt(t.prompt) }, "Edit this prompt")))));
  }
  if (t.mode === "upload") {
    return h("div", { class: "exp-turn" }, user,
      h("div", { class: "exp-msg bot" }, h("div", { class: "bubble muted small" },
        `Uploaded ${t.attachments.length > 1 ? `${t.attachments.length} images` : "an image"}. Use the image marked "current" as the base; your next message edits it.`)));
  }
  const busy = t.status === "pending" || t.status === "running";
  let progress = null;
  if (busy) {
    const step = !t.prompt ? "Writing the prompt…" : `Rendering ${Math.min(t.results.length + 1, t.draws)} of ${t.draws}…`;
    progress = h("div", { class: "exp-progress" }, h("span", { class: "spinner", "aria-hidden": "true" }), t.status === "pending" ? "Queued…" : step);
  }
  const results = t.results.length > 0 && h("div", { class: "exp-results" },
    t.results.map((r, n) => expImage(`turn:${t.turn_id}:${n}`, current, `Seed ${r.seed}`, r)));
  const meta = [
    t.size,
    t.results.length && `seed${t.results.length > 1 ? "s" : ""} ${t.results.map((r) => r.seed).join(", ")}`,
    t.references.length
      ? `sent ${t.references.map((r) => (exp.labels[r] != null ? `#${exp.labels[r]}` : "an image")).join(", ")}`
      : "new image (no picture)",
    t.prompt_model && `prompt by ${t.prompt_model}`,
    t.restrictive && "restrictive mode",
  ].filter(Boolean).join(" · ");
  const bot = h("div", { class: "exp-msg bot" },
    h("div", { class: "bubble" },
      h("div", { class: "exp-bot-head" },
        h("span", { class: `chip ${t.status === "done" ? "ok" : t.status === "failed" ? "bad" : "accent"}` }, t.refused ? "not allowed" : TURN_STATUS[t.status]),
        h("span", { class: "muted small" }, meta)),
      t.reply && h("p", { class: "exp-reply" }, t.reply),
      progress,
      results,
      t.error && h("div", { class: "exp-error" },
        h("p", { class: "form-error" }, t.error),
        h("button", {
          type: "button", class: "btn small", onclick: (e) => retryTurn(t, e.currentTarget),
          title: t.refused ? "Check this message again under the current rules" : null,
        }, t.refused ? "Check again" : "Retry")),
      t.prompt && h("details", { class: "exp-prompt" },
        h("summary", {}, "Prompt sent to the image model"),
        h("pre", {}, t.prompt),
        h("button", { type: "button", class: "btn small", onclick: () => usePrompt(t.prompt) }, "Edit this prompt"),
      ),
    ),
  );
  return h("div", { class: "exp-turn" }, user, bot);
}

// gref: the picture's global address when it's a copy (a pick start or an image brought
// in), so it shows the original's @number.
function expImage(ref, current, label, result, gref = ref) {
  const isCurrent = ref === current;
  const n = exp.labels[ref];
  return h("figure", { class: `exp-image${isCurrent ? " current" : ""}` },
    h("a", { href: refUrl(ref, "content"), target: "_blank", rel: "noopener", title: "Open full size" },
      h("img", { src: refUrl(ref, "thumb"), alt: n != null ? `#${n} ${label}` : label, loading: "lazy" }), lookalikeTag(exp.likeness[ref]), idTag(gref)),
    h("figcaption", {},
      n != null && h("button", {
        type: "button", class: "exp-id", title: `Image #${n}: click to mention it in your message`,
        onclick: () => insertMention(n),
      }, `#${n}`),
      isCurrent
        ? h("span", { class: "chip accent" }, "current")
        : h("button", { type: "button", class: "btn small", disabled: expBusy(exp.view), onclick: () => patchExp({ current: ref }) }, "Edit this one"),
      result && h("span", { class: "muted small" }, `seed ${result.seed}`),
      result && exp.view.session.target && h("button", {
        type: "button", class: "btn small", disabled: !!exp.used[ref],
        title: `Make this the pick of ${exp.view.session.target.label || "the picture this chat is for"}`,
        onclick: (e) => usePick(ref, e.currentTarget),
      }, exp.used[ref] ? "Now the pick" : "Use as pick"),
    ),
  );
}

// Unlink a chat or study from its picture: it goes back to Scratch (docs/workbench.md).
// `then` reloads the view; the page under the workbench panel shows the change too.
async function unlinkSession(sid, then) {
  try {
    await api(`/v1/experiments/${encodeURIComponent(sid)}`, { method: "PATCH", body: JSON.stringify({ unlink: true }) });
  } catch (e) {
    exp.notice = e.message;
  }
  await then();
  if (typeof benchRefreshPage === "function" && (exp.host || face.host)) benchRefreshPage();
}

// Use as pick (docs/workbench.md): the image becomes the pick of the chat's design picture.
// What was drawn from the old pick is then out of date (Redraw out-of-date on its page).
async function usePick(ref, button) {
  const n = exp.labels[ref];
  if (n == null) return;
  button.disabled = true;
  try {
    await api(`/v1/experiments/${encodeURIComponent(exp.view.session.session_id)}/use`, { method: "POST", body: JSON.stringify({ image: n }) });
    exp.used[ref] = true;
    exp.notice = `#${n} is now the pick of ${exp.view.session.target.label || "its picture"}. Pictures and keyframes drawn from the old one are marked out of date; redraw them from its page.`;
    if (exp.host) benchRefreshPage();  // the page under the panel shows the new pick
  } catch (e) {
    exp.notice = e.message;
  }
  renderExp();
}

async function retryTurn(t, button) {
  button.disabled = true;
  try {
    await api(`/v1/experiment-turns/${encodeURIComponent(t.turn_id)}/retry`, { method: "POST" });
    exp.error = "";
  } catch (e) {
    exp.error = e.message;
  }
  await loadExpSession();
}

function usePrompt(prompt) {
  const d = expDraft(exp.sid);
  d.text = prompt;
  d.mode = "exact";
  exp.el.text.value = prompt;
  renderComposer(exp.view);
  exp.el.text.focus();
  exp.el.text.scrollIntoView({ block: "center" });
}

// Composer -------------------------------------------------------------------------

function addExpFiles(files) {
  const d = expDraft(exp.sid);
  exp.error = "";
  for (const file of files) {
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
      exp.error = `${file.name}: use a JPEG, PNG or WebP image.`;
      continue;
    }
    d.files.push({ file, url: URL.createObjectURL(file), asset: null });
  }
  renderComposer(exp.view);
}

function renderComposer(v) {
  const d = expDraft(exp.sid);
  const el = exp.el;
  const busy = expBusy(v);
  const current = v.session.current;
  el.current.replaceChildren(current
    ? h("div", { class: "exp-current-row" },
      h("img", { src: refUrl(current), alt: "Current image", class: exp.likeness[current] ? "lookalike-thumb" : null, title: exp.likeness[current] ? LOOKALIKE_TIP : null }),
      h("div", {},
        h("strong", {}, `Editing the current image${exp.labels[current] != null ? ` (#${exp.labels[current]})` : ""}`),
        exp.likeness[current] && h("span", { class: "lookalike-tag inline", title: LOOKALIKE_TIP }, "Lookalike"),
        h("div", { class: "muted small" }, d.files.length ? `Sent as <Picture 1>; your attachments follow as <Picture 2>${d.files.length > 1 ? `–${d.files.length + 1}` : ""}.` : "Sent as <Picture 1> with your message.")),
      h("button", { type: "button", class: "btn ghost small", disabled: busy, onclick: () => patchExp({ clear_current: true }) }, "Start a new image"))
    : h("div", { class: "muted small" }, d.files.length
      ? "No current image: your attachments are the pictures. Send without text to just set the first one as the base."
      : "No current image: your message makes a new image. Attach an image to start from it."));
  el.files.replaceChildren(...d.files.map((f) =>
    h("div", { class: "exp-file" },
      h("img", { src: f.url, alt: f.file.name }),
      h("button", {
        type: "button", class: "btn ghost small", "aria-label": `Remove ${f.file.name}`,
        onclick: () => { URL.revokeObjectURL(f.url); d.files = d.files.filter((x) => x !== f); renderComposer(exp.view); },
      }, "×"))));
  el.options.replaceChildren(
    h("div", { class: "seg", role: "group", "aria-label": "Prompt mode" },
      [["rewrite", "Rewrite with GPT", "GPT turns your message into a full H3 prompt (it sees the pictures and the chat)"],
        ["exact", "Exact prompt", "Your text is sent to the image model as written"]].map(([m, label, title]) =>
        h("button", {
          type: "button", class: `seg-btn${d.mode === m ? " on" : ""}`, title, "aria-pressed": String(d.mode === m),
          onclick: () => { d.mode = m; renderComposer(exp.view); },
        }, label))),
    h("label", { class: "exp-draws" }, "Draws ",
      h("select", { "aria-label": "Draws", onchange: (e) => { d.draws = Number(e.target.value); } },
        [1, 2, 3, 4].map((n) => h("option", { value: n, selected: n === d.draws }, n)))),
  );
  el.text.classList.toggle("mono", d.mode === "exact");
  updateSendButton();
  el.error.textContent = exp.error;
}

function updateSendButton() {
  const v = exp.view, el = exp.el;
  if (!v || !el.send) return;
  const d = expDraft(exp.sid);
  const sending = exp.sending[exp.sid];
  el.send.disabled = expBusy(v) || sending != null;
  el.send.textContent = sending != null ? sending || "Sending…"
    : expBusy(v) ? "Working…"
    : d.text.trim() || !d.files.length ? "Send" : "Upload";
}

async function sendExp() {
  const v = exp.view;
  const sid = exp.sid;
  const d = expDraft(sid);
  if (exp.sending[sid] != null || expBusy(v)) return;
  d.text = exp.el.text.value;
  if (!d.text.trim() && !d.files.length) {
    exp.error = "Write a message or attach an image.";
    renderComposer(v);
    return;
  }
  // Progress shows only while this chat is on screen (the user may switch mid-upload).
  const progress = (label) => {
    exp.sending[sid] = label;
    if (exp.sid === sid) updateSendButton();
  };
  progress("Sending…");
  exp.error = "";
  let error = "";
  try {
    const files = [...d.files];
    for (const [i, f] of files.entries()) {
      if (f.asset) continue;  // uploaded before a failed send
      const n = files.length > 1 ? ` ${i + 1} of ${files.length}` : "";
      progress(`Uploading${n}…`);
      f.asset = await uploadFile(f.file, (p) => progress(`Uploading${n} (${Math.round(p * 100)}%)…`), () => {});
    }
    progress("Sending…");
    await api(`/v1/experiments/${encodeURIComponent(sid)}/turns`, {
      method: "POST",
      body: JSON.stringify({ text: d.text, attachments: files.map((f) => f.asset.asset_id), mode: d.mode, draws: d.draws, restrictive: restrictiveBox.checked }),
    });
    for (const f of files) URL.revokeObjectURL(f.url);
    d.files = [];
    d.text = "";
    if (exp.sid === sid) exp.el.text.value = "";
  } catch (e) {
    error = e.message;
  } finally {
    delete exp.sending[sid];
  }
  if (exp.sid === sid && inExp()) {
    exp.error = error;
    await loadExpSession();
    loadExpSessions();
  }
}

document.getElementById("new-exp").addEventListener("click", newExpSession);
