"use strict";

/*
 * Face Lab: upload a photo, GPT writes the most descriptive prompt for it, then the prompt
 * is tested by itself (text to image) with chosen draws and seeds. A study is an Experiment
 * session of kind "face". Uses h(), api(), $, timeAgo(), uploadFile() from app.js and
 * refUrl(), labelsOf(), restrictiveBox from experiment.js. Hash: #face or #face=<id>.
 */

const face = {
  sessions: [],
  sid: null,
  view: null,
  timer: null,
  mounted: null,
  el: {},
  // Per study: the prompt being edited, and the describe turn it was loaded from (so a
  // new description replaces it only while the user hasn't edited it).
  drafts: {},
  error: "",
  sending: false,
  // The workbench panel's element when the study is open over a character page
  // (bench.js); null: the Face Lab tab, in #main.
  host: null,
  used: {},  // renders made the face's pick from this study, by reference
  notice: "",
};

const faceRoot = () => face.host || $("#main");

const FACE_POLL_MS = 1000;
const inFace = () => (face.host ? face.host.isConnected : document.body.dataset.mode === "face");
const faceBusy = (v) => v && v.turns.some((t) => t.status === "pending" || t.status === "running");

function faceDraft(sid) {
  // seen: the newest description already offered (filled in, loaded, or noticed);
  // edited: the box holds the user's own text (or an older version they chose).
  return (face.drafts[sid] ||= { prompt: "", seen: null, edited: false, draws: 2, seedMode: "random", seeds: "", hint: "", size: null });
}

function goFace(sid) {
  location.hash = sid ? `face=${encodeURIComponent(sid)}` : "face";
}

function stopFace() {
  clearTimeout(face.timer);
  face.timer = null;
  face.mounted = null;
  face.sid = null;
  face.view = null;
}

async function faceRoute(sid) {
  setMode("face");
  clearTimeout(face.timer);
  if (face.sid !== sid) {
    face.sid = sid;
    face.view = null;
    face.mounted = null;
    face.error = "";
    face.used = {};
    face.notice = "";
  }
  await loadFaceSessions();
  if (!sid) {
    face.mounted = null;
    $("#main").replaceChildren(
      h("div", { class: "card placeholder" },
        h("h2", {}, "Face Lab"),
        h("p", {}, "Upload a photo of a face. GPT writes the most descriptive text-to-image prompt it can for it, never naming the person. Then edit the prompt and test it by itself, with chosen draws and seeds, and compare the renders with the photo."),
        h("button", { type: "button", class: "btn primary-outline", onclick: () => $("#face-file").click() }, "New study from a photo"),
      ),
    );
    return;
  }
  if (!face.view) $("#main").replaceChildren(h("div", { class: "loading" }, "Loading…"));
  await loadFaceStudy();
}

async function loadFaceSessions() {
  try {
    face.sessions = await api("/v1/experiments?kind=face");
  } catch {
    face.sessions = [];
  }
  const list = $("#face-list");
  if (!face.sessions.length) {
    list.replaceChildren(h("li", { class: "empty" }, "No studies yet."));
    return;
  }
  const open = (sid) => (ev) => {
    if (ev.type === "keydown" && ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    goFace(sid);
  };
  list.replaceChildren(...face.sessions.map((s) =>
    h("li", {},
      h("div", {
        class: `job-item${s.session_id === face.sid ? " active" : ""}`, role: "link", tabindex: "0",
        "aria-current": s.session_id === face.sid ? "page" : null,
        onclick: open(s.session_id), onkeydown: open(s.session_id),
      },
        h("div", { class: "seed" }, s.title),
        h("div", { class: "meta" }, h("span", { class: "muted small" }, timeAgo(s.updated_at))),
      ),
    )));
}

// New study: upload, create, describe.
async function newFaceStudy(file) {
  const err = $("#face-side-error");
  const btn = $("#new-face");
  err.textContent = "";
  if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
    err.textContent = `${file.name}: use a JPEG, PNG or WebP photo.`;
    return;
  }
  btn.disabled = true;
  let study = null;
  try {
    btn.textContent = "Uploading…";
    const asset = await uploadFile(file, (p) => { btn.textContent = `Uploading (${Math.round(p * 100)}%)…`; }, () => { btn.textContent = "Processing…"; });
    btn.textContent = "Describing…";
    study = await api("/v1/experiments", { method: "POST", body: JSON.stringify({ kind: "face" }) });
    await api(`/v1/experiments/${encodeURIComponent(study.session_id)}/describe`, {
      method: "POST",
      body: JSON.stringify({ asset_id: asset.asset_id, restrictive: restrictiveBox.checked }),
    });
  } catch (e) {
    err.textContent = e.message;
  } finally {
    // Open the study even if describing failed, so it isn't left behind unseen.
    if (study) goFace(study.session_id);
    btn.disabled = false;
    btn.textContent = "New study from a photo";
  }
}

$("#new-face").addEventListener("click", () => $("#face-file").click());
$("#face-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) newFaceStudy(file);
});

async function loadFaceStudy() {
  const sid = face.sid;
  try {
    const v = await api(`/v1/experiments/${encodeURIComponent(sid)}`);
    if (face.sid !== sid || !inFace()) return;
    const titleChanged = face.view?.session.title !== v.session.title;
    face.view = v;
    renderFace();
    if (titleChanged) loadFaceSessions();
  } catch (e) {
    if (face.sid !== sid || !inFace()) return;
    face.mounted = null;
    faceRoot().replaceChildren(h("div", { class: "card placeholder" }, h("p", { class: "form-error" }, e.message)));
    return;
  }
  clearTimeout(face.timer);
  if (faceBusy(face.view)) face.timer = setTimeout(loadFaceStudy, FACE_POLL_MS);
}

// ---------------------------------------------------------------------------------
// Study view

const describes = (v) => v.turns.filter((t) => t.mode === "describe");
const tests = (v) => v.turns.filter((t) => t.mode !== "describe" && t.mode !== "upload");

function photoRef(v) {
  const d = [...describes(v)].reverse().find((t) => t.attachments.length);
  return d ? `asset:${d.attachments[0]}` : null;
}

function renderFace() {
  const v = face.view;
  if (!v) return;
  face.labels = labelsOf(v);
  if (face.mounted !== v.session.session_id) mountFace(v);
  syncPromptFromDescription(v);
  renderFaceHeader(v);
  renderPhoto(v);
  renderPromptControls(v);
  renderTests(v);
}

function mountFace(v) {
  face.mounted = v.session.session_id;
  const d = faceDraft(face.sid);
  const el = face.el = {};
  el.header = h("div", { class: "card exp-header" });
  el.photo = h("div", { class: "card face-photo" });
  el.prompt = h("textarea", {
    rows: 14, class: "mono", "aria-label": "Prompt",
    placeholder: "The description appears here. Edit it, then render to test it.",
    oninput: (e) => { d.prompt = e.target.value; d.edited = true; updateFaceCount(); },
  });
  el.prompt.value = d.prompt;
  el.count = h("span", { class: "muted small" });
  el.notice = h("div", { class: "face-notice", hidden: true, role: "status" });
  el.controls = h("div", { class: "face-controls" });
  el.render = h("button", { type: "button", class: "btn primary face-render", onclick: renderTest }, "Render");
  el.error = h("p", { class: "form-error", role: "alert" });
  el.tests = h("div", { class: "face-tests" });
  faceRoot().replaceChildren(h("div", { class: "face" },
    el.header,
    h("div", { class: "face-grid" },
      el.photo,
      h("div", { class: "face-work" },
        h("div", { class: "card face-prompt" },
          h("div", { class: "face-prompt-head" }, h("h3", {}, "Prompt"), el.count),
          el.notice, el.prompt, el.controls,
          h("div", { class: "face-actions" }, el.render), el.error),
        el.tests,
      ),
    ),
  ));
}

function syncPromptFromDescription(v) {
  // A finished description fills the prompt box, unless the user has edited it since: then
  // it waits behind a notice, so edits are never overwritten.
  const d = faceDraft(face.sid);
  const latest = [...describes(v)].reverse().find((t) => t.status === "done" && t.prompt);
  const fresh = latest && latest.turn_id !== d.seen;
  if (fresh && !d.edited) {
    d.prompt = latest.prompt;
    d.seen = latest.turn_id;
    if (document.activeElement !== face.el.prompt) face.el.prompt.value = d.prompt;
  }
  const waiting = fresh && d.edited;
  face.el.notice.replaceChildren(...(waiting
    ? [h("span", {}, "A new description is ready. Your edits are kept."),
      h("button", { type: "button", class: "btn small", onclick: () => loadPrompt(latest.prompt, latest.turn_id) }, "Load it")]
    : []));
  face.el.notice.hidden = !waiting;
  updateFaceCount();
}

function updateFaceCount() {
  const n = face.el.prompt.value.length;
  face.el.count.textContent = `${n.toLocaleString()} characters`;
  face.el.count.classList.toggle("warn-text", n > 6000);
}

function renderFaceHeader(v) {
  const s = v.session;
  if (face.el.header.contains(document.activeElement)) return;  // renaming
  const title = h("input", {
    value: s.title, "aria-label": "Study title", maxlength: "120", class: "face-title",
    onchange: async (e) => {
      const t = e.target.value.trim();
      if (!t || t === s.title) return;
      try {
        face.view.session = await api(`/v1/experiments/${encodeURIComponent(face.sid)}`, { method: "PATCH", body: JSON.stringify({ title: t }) });
        loadFaceSessions();
      } catch (err) { face.error = err.message; renderFace(); }
    },
  });
  const del = h("button", { type: "button", class: "btn ghost small", disabled: faceBusy(v), onclick: deleteFace }, "Delete study");
  face.el.header.replaceChildren(
    s.target && h("p", { class: "small exp-target" }, "For ", h("strong", {}, s.target.label || "a face"),
      ". Use as pick on a render makes it that face's pick."),
    face.notice && h("p", { class: "small hint" }, face.notice),
    h("div", { class: "exp-head-row" }, title,
      h("span", { class: "muted small" }, `${describes(v).length} description${describes(v).length === 1 ? "" : "s"} · ${tests(v).length} test${tests(v).length === 1 ? "" : "s"}`),
      h("div", { class: "exp-head-actions" }, del)));
}

async function deleteFace(e) {
  const btn = e.currentTarget;
  if (btn.dataset.armed !== "1") {
    btn.dataset.armed = "1";
    btn.textContent = "Click again to delete";
    setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ""; btn.textContent = "Delete study"; } }, 4000);
    return;
  }
  try {
    await api(`/v1/experiments/${encodeURIComponent(face.sid)}`, { method: "DELETE" });
    delete face.drafts[face.sid];
    if (face.host) closeBench();
    else goFace(null);
  } catch (err) {
    face.error = err.message;
    renderFace();
  }
}

function renderPhoto(v) {
  const el = face.el.photo;
  if (el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // typing a hint
  const d = faceDraft(face.sid);
  const ref = photoRef(v);
  const busy = faceBusy(v);
  const hint = h("input", {
    value: d.hint, placeholder: "Optional focus, e.g. more detail on the eyes", "aria-label": "Describe hint",
    oninput: (e) => { d.hint = e.target.value; },
  });
  const swap = h("input", {
    type: "file", accept: "image/jpeg,image/png,image/webp", hidden: true,
    onchange: async (e) => {
      const file = e.target.files[0];
      e.target.value = "";
      if (!file) return;
      try {
        const asset = await uploadFile(file, () => {}, () => {});
        await describe({ asset_id: asset.asset_id });
      } catch (err) { face.error = err.message; renderFace(); }
    },
  });
  const descs = [...describes(v)].reverse();
  // h() flattens nested lists and drops false/null; replaceChildren alone would print them.
  el.replaceChildren(h("div", { class: "contents" },
    h("h3", {}, "Photo"),
    ref
      ? h("a", { href: refUrl(ref, "content"), target: "_blank", rel: "noopener", title: "Open full size" },
        h("img", { src: refUrl(ref), alt: "The photo being described", class: "face-photo-img" }))
      : h("p", { class: "muted" }, "No photo yet."),
    ref && face.labels[ref] != null && h("span", { class: "exp-id" }, `#${face.labels[ref]}`),
    h("div", { class: "face-describe" },
      hint,
      h("div", { class: "face-actions" },
        h("button", { type: "button", class: "btn small", disabled: busy || !ref, onclick: () => describe({ hint: d.hint }) }, "Describe again"),
        h("button", { type: "button", class: "btn ghost small", disabled: busy, onclick: () => swap.click() }, "Use another photo"),
        swap),
    ),
    h("h3", { class: "face-sub" }, "Descriptions"),
    descs.length === 0 && h("p", { class: "muted small" }, "None yet."),
    descs.map((t) => h("div", { class: "face-desc" },
      h("div", { class: "exp-bot-head" },
        h("span", { class: `chip ${t.status === "done" ? "ok" : t.status === "failed" ? "bad" : "accent"}` }, t.refused ? "not allowed" : t.status === "running" || t.status === "pending" ? "describing…" : t.status),
        t.text && h("span", { class: "muted small" }, `focus: ${t.text}`),
        t.likeness && h("span", { class: "chip warn", title: "The photo shows an identifiable person: renders of this prompt carry their likeness (nothing sexualized)" }, "real-person likeness"),
        t.restrictive && h("span", { class: "muted small" }, "restrictive"),
      ),
      t.reply && h("p", { class: "small" }, t.reply),
      t.error && h("p", { class: "form-error" }, t.error),
      t.prompt && h("button", { type: "button", class: "btn small", onclick: () => loadPrompt(t.prompt, t.turn_id) }, "Load this description"),
    )),
  ));
}

function loadPrompt(prompt, fromTurn = null) {
  const d = faceDraft(face.sid);
  const latest = [...describes(face.view)].reverse().find((t) => t.status === "done" && t.prompt);
  d.prompt = prompt;
  if (latest && fromTurn === latest.turn_id) {
    d.seen = fromTurn;  // the newest description: new ones may replace it
    d.edited = false;
  } else {
    d.edited = true;  // an older version or a test's prompt: the user's choice, kept
  }
  face.el.prompt.value = prompt;
  syncPromptFromDescription(face.view);
  face.el.prompt.focus();
  face.el.prompt.scrollIntoView({ block: "center" });
}

const SEED_MAX = 2147483647;

function parseSeeds(text) {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const seeds = parts.map((p) => Number(p));
  if (seeds.some((s) => !Number.isInteger(s) || s < 1 || s > SEED_MAX)) throw new Error(`Seeds are whole numbers from 1 to ${SEED_MAX}.`);
  return seeds;
}

function lastSeeds(v) {
  const t = [...tests(v)].reverse().find((x) => x.results.length);
  return t ? t.results.map((r) => r.seed) : [];
}

function renderPromptControls(v) {
  const el = face.el.controls;
  if (el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // typing seeds
  const d = faceDraft(face.sid);
  d.size ||= v.session.size;
  const prev = lastSeeds(v);
  const seedInput = h("input", {
    value: d.seeds, placeholder: "e.g. 11, 22, 33", "aria-label": "Seeds", class: "face-seeds", disabled: d.seedMode !== "fixed",
    oninput: (e) => { d.seeds = e.target.value; updateRenderButton(); },
  });
  const mode = (m, label) => h("button", {
    type: "button", class: `seg-btn${d.seedMode === m ? " on" : ""}`, "aria-pressed": String(d.seedMode === m),
    onclick: () => { d.seedMode = m; renderPromptControls(face.view); updateRenderButton(); },
  }, label);
  el.replaceChildren(h("div", { class: "contents" },
    h("div", { class: "face-control" }, h("span", { class: "muted small" }, "Seeds"),
      h("div", { class: "seg", role: "group", "aria-label": "Seed mode" }, mode("random", "Random"), mode("fixed", "Fixed")),
      seedInput,
      h("button", {
        type: "button", class: "btn ghost small", disabled: !prev.length,
        title: "Reuse the last test's seeds, so only the prompt changes",
        onclick: () => { d.seedMode = "fixed"; d.seeds = prev.join(", "); renderPromptControls(face.view); updateRenderButton(); },
      }, prev.length ? `Same as last (${prev.join(", ")})` : "Same as last"),
    ),
    h("div", { class: "face-control" },
      d.seedMode === "random" && h("label", { class: "exp-draws" }, "Draws ",
        h("select", { "aria-label": "Draws", onchange: (e) => { d.draws = Number(e.target.value); updateRenderButton(); } },
          [1, 2, 3, 4].map((n) => h("option", { value: n, selected: n === d.draws }, n)))),
      h("label", { class: "exp-draws" }, "Size ",
        h("select", { "aria-label": "Size", onchange: (e) => { d.size = e.target.value; } },
          Object.entries(v.sizes).map(([aspect, size]) => h("option", { value: size, selected: size === d.size }, `${aspect} · ${size}`)))),
    ),
  ));
  updateRenderButton();
}

function updateRenderButton() {
  const v = face.view, d = faceDraft(face.sid);
  if (!v || !face.el.render) return;
  let n = d.draws;
  if (d.seedMode === "fixed") { try { n = parseSeeds(d.seeds).length; } catch { n = 0; } }
  face.el.render.disabled = faceBusy(v) || face.sending;
  face.el.render.textContent = face.sending ? "Sending…" : faceBusy(v) ? "Working…" : `Render ${n || ""} ${n === 1 ? "image" : "images"}`.replace("  ", " ");
  face.el.error.textContent = face.error;
}

async function describe(body) {
  face.error = "";
  try {
    await api(`/v1/experiments/${encodeURIComponent(face.sid)}/describe`, {
      method: "POST", body: JSON.stringify({ ...body, restrictive: restrictiveBox.checked }),
    });
  } catch (e) {
    face.error = e.message;
  }
  await loadFaceStudy();
}

async function renderTest() {
  const d = faceDraft(face.sid);
  face.error = "";
  let body;
  try {
    body = { prompt: face.el.prompt.value, size: d.size, restrictive: restrictiveBox.checked };
    if (d.seedMode === "fixed") {
      const seeds = parseSeeds(d.seeds);
      if (!seeds.length) throw new Error("Enter at least one seed, or choose Random.");
      body.seeds = seeds;
    } else {
      body.draws = d.draws;
    }
  } catch (e) {
    face.error = e.message;
    updateRenderButton();
    return;
  }
  face.sending = true;
  updateRenderButton();
  try {
    await api(`/v1/experiments/${encodeURIComponent(face.sid)}/tests`, { method: "POST", body: JSON.stringify(body) });
  } catch (e) {
    face.error = e.message;
  } finally {
    face.sending = false;
  }
  await loadFaceStudy();
}

function renderTests(v) {
  const list = [...tests(v)].reverse();
  const photo = photoRef(v);
  face.el.tests.replaceChildren(h("div", { class: "contents" },
    h("h3", { class: "face-sub" }, `Tests (${list.length})`),
    list.length === 0 && h("p", { class: "muted small" }, "No tests yet. Render the prompt to see how close words alone get."),
    list.map((t) => {
      const busy = t.status === "pending" || t.status === "running";
      return h("div", { class: "card face-test" },
        h("div", { class: "exp-bot-head" },
          h("span", { class: `chip ${t.status === "done" ? "ok" : t.status === "failed" ? "bad" : "accent"}` }, t.refused ? "not allowed" : busy ? `rendering ${Math.min(t.results.length + 1, t.draws)} of ${t.draws}` : t.status),
          h("span", { class: "muted small" }, [
            t.size,
            t.seeds.length ? `fixed seeds ${t.seeds.join(", ")}` : "random seeds",
            timeAgo(t.created_at),
          ].join(" · ")),
          t.likeness && h("span", { class: "chip warn", title: "Recreates an identifiable person: nothing sexualized can be made from it" }, "real-person likeness"),
        ),
        h("div", { class: "face-results" },
          photo && h("figure", { class: "exp-image face-original" },
            h("img", { src: refUrl(photo), alt: "The photo" }),
            h("figcaption", {}, h("span", { class: "muted small" }, "photo"))),
          t.results.map((r, n) => {
            const ref = `turn:${t.turn_id}:${n}`;
            return h("figure", { class: "exp-image" },
              h("a", { href: refUrl(ref, "content"), target: "_blank", rel: "noopener", title: "Open full size" },
                h("img", { src: refUrl(ref), alt: `Seed ${r.seed}`, loading: "lazy" })),
              h("figcaption", {},
                r.label != null && h("span", { class: "exp-id" }, `#${r.label}`),
                h("span", { class: "muted small" }, `seed ${r.seed}`),
                v.session.target && r.label != null && h("button", {
                  type: "button", class: "btn small", disabled: !!face.used[ref],
                  title: `Make this the pick of ${v.session.target.label || "the face this study is for"}`,
                  onclick: (e) => faceUsePick(ref, r.label, e.currentTarget),
                }, face.used[ref] ? "Now the pick" : "Use as pick")));
          }),
        ),
        t.error && h("div", { class: "exp-error" },
          h("p", { class: "form-error" }, t.error),
          h("button", { type: "button", class: "btn small", onclick: (e) => faceRetry(t, e.currentTarget) }, t.refused ? "Check again" : "Retry")),
        h("details", { class: "exp-prompt" },
          h("summary", {}, "Prompt"),
          h("pre", {}, t.prompt),
          h("button", { type: "button", class: "btn small", onclick: () => loadPrompt(t.prompt) }, "Load this prompt")),
      );
    }),
  ));
}

// Use as pick (docs/workbench.md): a render becomes the pick of the face this study is for.
async function faceUsePick(ref, n, button) {
  button.disabled = true;
  try {
    await api(`/v1/experiments/${encodeURIComponent(face.view.session.session_id)}/use`, { method: "POST", body: JSON.stringify({ image: n }) });
    face.used[ref] = true;
    face.notice = `#${n} is now the pick of ${face.view.session.target.label || "the face"}. Looks drawn from the old face are marked out of date; redraw them from the page.`;
    if (face.host) benchRefreshPage();
  } catch (e) {
    face.notice = e.message;
  }
  renderFace();
}

async function faceRetry(t, button) {
  button.disabled = true;
  try {
    await api(`/v1/experiment-turns/${encodeURIComponent(t.turn_id)}/retry`, { method: "POST" });
    face.error = "";
  } catch (e) {
    face.error = e.message;
  }
  await loadFaceStudy();
}
