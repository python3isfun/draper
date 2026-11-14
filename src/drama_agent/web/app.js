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

const state = { jobs: [], job: null, pkg: null, tab: "overview", pollTimer: null, assetFilter: "all", revise: null, offline: false, attachments: [], assets: {}, sourcesOpen: null, openDetails: new Set(), images: { key: null, view: null, error: "", timer: null } };

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
  const params = new URLSearchParams(location.hash.slice(1));
  if (params.has("exp")) {
    clearTimeout(state.pollTimer);
    stopActivity();
    await expRoute(params.get("exp") || null);
    return;
  }
  stopExp();
  setMode("drama");
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
    stopActivity();
    state.revise = null;
    state.sourcesOpen = null;
    clearTimeout(state.images.timer);
    state.images = { key: null, view: null, error: "", timer: null };
    renderMain();
  } else if (state.job) {
    // Same job, different tab: a user action, so redraw now. (loadJob below only redraws
    // when the job's data changed, which a tab switch doesn't do.)
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
    // Built with h() so nested arrays are flattened (replaceChildren would stringify them).
    $("#meta").replaceChildren(h("span", { class: "meta-models" },
      h("span", { class: `chip ${live ? "ok" : "warn"}` }, live ? "live model" : "offline fixture"),
      " ", h("span", { class: "mono", title: "Default model for every step without an override" }, m.model_id),
      // Per-step overrides, e.g. "review: gpt-6-luna".
      Object.entries(m.stage_models || {}).map(([stage, model]) =>
        [" ", h("span", { class: "chip mono", title: `The ${stage} step uses ${model}` }, `${stage}: ${model}`)]),
    ));
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
    if (job.package_id && !job.archived && (changed || !pkg)) {
      const q = new URLSearchParams({ revision_id: job.revision_id || "" });
      pkg = await api(`/v1/drama-packages/${encodeURIComponent(job.package_id)}?${q}`);
      if (!stillCurrent()) return;
    }
    // Polling must not rebuild the page when nothing changed: rebuilding destroys any text
    // the user has selected, which made running jobs impossible to copy from.
    if (!job.archived) await loadAssets(job);
    if (!stillCurrent()) return;
    const sig = JSON.stringify(job) + "|" + (pkg?.revision_id || "") + "|" + assetSig(job);
    const unchanged = sig === state.renderedSig;
    state.job = job;
    state.pkg = pkg;
    if (!unchanged) {
      state.renderedSig = sig;
      renderWhenNotSelecting();
    }
    kickActivity();  // e.g. a resumed job starts streaming again
    if (changed) loadJobs();
    clearTimeout(state.pollTimer);
    if (isActive(job.status)) state.pollTimer = setTimeout(() => loadJob(jobId), 1500);
  } catch (err) {
    if (!stillCurrent()) return;
    $("#main").replaceChildren(h("div", { class: "card placeholder" }, h("h2", {}, "Couldn't load job"), err.message));
  }
}

const assetIds = (job) => job.request.sources.map((src) => src.asset_id).filter(Boolean);
const assetSig = (job) => assetIds(job).map((id) => state.assets[id]?.transcript_status || "").join(",");

// Uploaded media of the job's sources. Re-fetched while a transcript is still pending.
async function loadAssets(job) {
  await Promise.all(assetIds(job).map(async (id) => {
    const cached = state.assets[id];
    if (cached && !(cached.transcript_status === "pending" && isActive(job.status))) return;
    try {
      state.assets[id] = await api(`/v1/source-assets/${encodeURIComponent(id)}`);
    } catch { /* shown as unavailable */ }
  }));
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

// New-job attachments ----------------------------------------------------------

const USE_AS = [
  ["", "Auto"],
  ["story_seed", "Story seed"],
  ["character_reference", "Character reference"],
  ["location_reference", "Location reference"],
  ["visual_style", "Visual style"],
  ["plot_reference", "Plot reference"],
];
const ACCEPTED = /^(image\/(jpeg|png|webp|gif)|video\/(mp4|quicktime|webm))$/;

function fmtBytes(n) {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function addFiles(files) {
  const err = $("#new-job-error");
  err.textContent = "";
  for (const file of files) {
    if (!ACCEPTED.test(file.type)) {
      err.textContent = `${file.name}: unsupported type. Use JPEG, PNG, WebP or GIF images, or MP4, MOV or WebM videos.`;
      continue;
    }
    state.attachments.push({ file, url: URL.createObjectURL(file), use_as: "", text: "" });
  }
  renderAttachments();
}

function removeAttachment(a) {
  URL.revokeObjectURL(a.url);
  state.attachments = state.attachments.filter((x) => x !== a);
  renderAttachments();
}

function renderAttachments() {
  $("#attachments").replaceChildren(...state.attachments.map((a) => {
    const isVideo = a.file.type.startsWith("video/");
    const preview = isVideo
      ? h("video", { src: a.url, muted: true, preload: "metadata", class: "thumb" })
      : h("img", { src: a.url, alt: "", class: "thumb" });
    const select = h("select", { "aria-label": "Use as", onchange: (e) => { a.use_as = e.target.value; } },
      USE_AS.map(([v, label]) => h("option", { value: v, selected: v === a.use_as }, label)));
    const prompt = h("textarea", {
      rows: 2, placeholder: isVideo ? "Optional: what to take from this video, e.g. keep the father-daughter reunion" : "Optional: what to do with this image, e.g. make her the lead in a revenge story",
      oninput: (e) => { a.text = e.target.value; },
    });
    prompt.value = a.text;
    return h("div", { class: "attachment" },
      preview,
      h("div", { class: "attachment-body" },
        h("div", { class: "attachment-head" },
          h("span", { class: "name", title: a.file.name }, a.file.name),
          h("span", { class: "muted small" }, `${isVideo ? "video" : "image"} · ${fmtBytes(a.file.size)}${a.asset ? " · uploaded" : ""}`),
          h("button", { type: "button", class: "btn ghost small", onclick: () => removeAttachment(a), "aria-label": `Remove ${a.file.name}` }, "Remove"),
        ),
        select,
        prompt,
      ),
    );
  }));
}

$("#file-input").addEventListener("change", (e) => {
  addFiles([...e.target.files]);
  e.target.value = "";  // choosing the same file again still fires change
});
const dropzone = $("#dropzone");
for (const t of ["dragenter", "dragover"]) dropzone.addEventListener(t, (e) => { e.preventDefault(); dropzone.classList.add("over"); });
for (const t of ["dragleave", "drop"]) dropzone.addEventListener(t, () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", (e) => { e.preventDefault(); addFiles([...e.dataTransfer.files]); });

// XHR, not fetch: fetch can't report upload progress, and videos can be large.
// onSent fires when the bytes are all sent; the server then processes the file (videos:
// keyframe extraction) before it answers.
function uploadFile(file, onProgress, onSent) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/v1/source-assets?filename=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.upload.onload = onSent;
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 201 && body) resolve(body);
      else reject(new Error(`${file.name}: ${body?.detail || xhr.statusText || "upload failed"}`));
    };
    xhr.onerror = () => reject(new Error(`${file.name}: network error during upload`));
    xhr.send(file);
  });
}

$("#new-job").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const f = new FormData(form);
  const button = form.querySelector("button[type=submit]");
  const label = button.textContent;
  const err = $("#new-job-error");
  err.textContent = "";
  const type = f.get("type");
  const text = f.get("text").trim();
  if (!text && !state.attachments.length) {
    err.textContent = "Enter an idea, or attach an image or video.";
    return;
  }
  const sources = [];
  if (text) sources.push({ type, text, use_as: type === "script" ? "plot_reference" : "story_seed" });
  button.disabled = true;
  try {
    const attachments = [...state.attachments];
    for (const [i, a] of attachments.entries()) {
      const n = attachments.length > 1 ? ` ${i + 1} of ${attachments.length}` : "";
      // Files that uploaded before a failed submit are reused, not uploaded again.
      if (!a.asset) {
        button.textContent = `Uploading${n}…`;
        a.asset = await uploadFile(
          a.file,
          (p) => { button.textContent = `Uploading${n} (${Math.round(p * 100)}%)…`; },
          () => { button.textContent = a.file.type.startsWith("video/") ? `Extracting frames${n}…` : `Processing${n}…`; },
        );
        renderAttachments();
      }
      sources.push({ type: a.asset.kind, asset_id: a.asset.asset_id, text: a.text.trim() || null, use_as: a.use_as || null });
    }
    button.textContent = "Starting…";
    const body = {
      sources,
      options: {
        language: "en-US",
        story_scope: f.get("story_scope"),
        reference_mode: f.get("reference_mode"),
        content_rating: f.get("content_rating"),
        genre: f.get("genre").trim() || null,
        audience: f.get("audience").trim() || null,
      },
    };
    const created = await api("/v1/drama-jobs", { method: "POST", body: JSON.stringify(body) });
    for (const a of state.attachments) URL.revokeObjectURL(a.url);
    state.attachments = [];
    renderAttachments();
    form.querySelector("textarea[name=text]").value = "";
    await loadJobs();
    go(created.job_id);
  } catch (e) {
    err.textContent = e.message;
  } finally {
    button.disabled = false;
    button.textContent = label;
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
function typingIn(el) {
  const a = document.activeElement;
  return a && el.contains(a) && (a.tagName === "INPUT" || a.tagName === "TEXTAREA") && a.type !== "checkbox";
}

function renderWhenNotSelecting() {
  if (selectingIn($("#main")) || typingIn($("#main"))) {
    state.renderPending = true;
    return;
  }
  state.renderPending = false;
  renderMain();
}

// A redraw postponed while typing runs once the field loses focus.
document.addEventListener("focusout", () => setTimeout(() => {
  if (state.renderPending && !typingIn($("#main")) && !selectingIn($("#main"))) renderWhenNotSelecting();
}, 0));

document.addEventListener("selectionchange", () => {
  if (state.renderPending && !selectingIn($("#main"))) renderWhenNotSelecting();
  if (state.jobListPending && !selectingIn($("#job-list"))) renderJobList();
});

function renderMain() {
  // A drama poll that lands while the Experiment tab is open must not draw over it.
  if (document.body.dataset.mode === "exp") return;
  const main = $("#main");
  const job = state.job;
  if (!readHash().job) {
    main.replaceChildren(
      h("div", { class: "card placeholder" },
        h("h2", {}, "Turn a seed into a 3-episode short drama"),
        h("p", {}, "Enter a one-line idea, paste a script, or upload images or videos on the left. Each job produces scripts, a timed shot list and art asset requirements."),
      ),
    );
    return;
  }
  if (!job) {
    main.replaceChildren(h("div", { class: "loading" }, "Loading…"));
    return;
  }
  if (job.archived) {
    // Archived stories (e.g. Chinese-language ones from before the app was English-only) are
    // kept in the database but not shown.
    main.replaceChildren(
      h("div", { class: "card placeholder" },
        h("h2", {}, "Archived story"),
        h("p", {}, job.archive_reason || "This story is archived."),
        h("p", { class: "muted small" }, "It is kept in the database but not shown, and it can't be retried or resumed."),
        h("code", { class: "muted" }, job.job_id),
      ),
    );
    return;
  }
  main.replaceChildren(renderJobHeader(job), state.pkg ? renderPackage(state.pkg) : renderWaiting(job));
}

function renderJobHeader(job) {
  const pkg = state.pkg;
  const firstText = job.request.sources.find((src) => src.text)?.text;
  const firstFile = state.assets[assetIds(job)[0]]?.filename;
  const title = pkg?.story_bible?.title || firstText?.slice(0, 60) || firstFile || job.job_id;
  const actions = [];
  if (pkg) {
    const q = new URLSearchParams({ revision_id: pkg.revision_id });
    const base = `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}`;
    const zipQ = new URLSearchParams({ revision_id: pkg.revision_id, job_id: job.job_id });
    actions.push(
      h("a", { class: "btn small primary-outline", href: `${base}/export?${zipQ}`,
        title: "Production hand-off: story bible, scripts, storyboard and shot list (CSV), cast/set/prop sheets, asset requirements with reference images, QA report and sources, in one zip" }, "Download zip"),
      h("a", { class: "btn small", href: `${base}?${q}&format=markdown`, target: "_blank", rel: "noopener" }, "Markdown"),
      h("a", { class: "btn small", href: `${base}?${q}`, download: `${pkg.package_id}.json` }, "JSON"),
    );
  }
  if (isActive(job.status)) {
    if (job.status === "running") {
      // For a model call that hangs: interrupt it now; the step restarts on resume.
      actions.unshift(h("button", { class: "btn small", type: "button", onclick: (e) => jobAction(job.job_id, "pause?now=true", e.currentTarget),
        title: "Stop now: cancel the current model call. That step restarts on resume; steps that passed are kept." }, "Stop now"));
    }
    actions.unshift(job.pause_requested
      ? h("button", { class: "btn small", type: "button", disabled: true }, "Pausing…")
      : h("button", { class: "btn small primary-outline", type: "button", onclick: (e) => jobAction(job.job_id, "pause", e.currentTarget),
          title: "Stop after the current model call. Steps that passed are kept." }, "Pause"));
  }
  if (job.status === "paused") {
    actions.unshift(h("button", { class: "btn small primary-outline", type: "button", onclick: (e) => jobAction(job.job_id, "resume", e.currentTarget),
      title: "Continue from where it stopped." }, "Resume"));
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
  if (job.pause_requested) {
    banners.push(h("div", { class: "banner info" }, h("strong", {}, "Pausing. "),
      "The job stops once the current model call finishes, so that call's work isn't lost. If the call seems stuck, use Stop now."));
  }
  if (job.status === "paused") {
    const next = Object.entries(job.stages).find(([, s]) => s.status !== "passed")?.[0];
    banners.push(h("div", { class: "banner info" }, h("strong", {}, "Paused. "),
      `Steps that passed are kept. Resume continues with the ${next || "next"} step.`));
  }
  if (job.status === "failed") {
    banners.push(h("div", { class: "banner bad" }, h("strong", {}, "Job failed. "), job.failure_reason || ""));
  }
  if (job.status === "needs_revision") {
    // Concurrent steps (the design sheets) can fail together: list every one.
    const failed = Object.entries(job.stages).filter(([, s]) => s.status === "failed");
    const names = failed.map(([n]) => n);
    const where = names.length ? names.join(", ") : "?";
    banners.push(
      h("div", { class: "banner warn" },
        h("strong", {}, `Needs revision at the ${where} step${names.length > 1 ? "s" : ""}. `),
        "Hard checks still failed after the retry budget. The draft below is kept for inspection.",
        failed.map(([n, st]) => [
          names.length > 1 && h("div", { class: "banner-sub" }, n),
          h("ul", {}, st.issues.slice(0, 8).map((i) => h("li", {}, h("code", {}, i.object_id), ` ${i.check}: ${i.evidence}`))),
          st.issues.length > 8 && h("p", { class: "small" }, `…and ${st.issues.length - 8} more`),
        ]),
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
    renderSources(job),
  );
}

// Sources -------------------------------------------------------------------

const USE_AS_LABEL = Object.fromEntries(USE_AS);
const assetUrl = (id, path = "content") => `/v1/source-assets/${encodeURIComponent(id)}/${path}`;

function renderSources(job) {
  const sources = job.request.sources;
  // Open by default until there's a package to look at; then it remembers the user's choice.
  const open = state.sourcesOpen ?? !state.pkg;
  const d = h("details", { class: "sources", open },
    h("summary", {}, `Sources (${sources.length})`),
    sources.map((src, i) => renderSource(src, i)),
  );
  d.addEventListener("toggle", () => { state.sourcesOpen = d.open; });
  return d;
}

function renderSource(src, i) {
  const tag = h("span", { class: "chip mono" }, `source[${i}]`);
  const use = src.use_as && h("span", { class: "chip" }, USE_AS_LABEL[src.use_as] || src.use_as);
  if (src.type === "text" || src.type === "script") {
    return h("div", { class: "source" },
      h("div", { class: "source-head" }, tag, h("span", { class: "chip" }, src.type === "script" ? "Script" : "Idea"), use),
      h("p", { class: "source-text" }, src.text || ""),
    );
  }
  const asset = state.assets[src.asset_id];
  const prompt = src.text && h("p", { class: "source-text" }, h("span", { class: "muted" }, "Prompt: "), src.text);
  if (!asset) {
    return h("div", { class: "source" },
      h("div", { class: "source-head" }, tag, h("span", { class: "chip" }, src.type), h("code", {}, src.asset_id)), prompt);
  }
  const head = h("div", { class: "source-head" }, tag, h("span", { class: "chip" }, asset.kind), use,
    h("span", { class: "name" }, asset.filename),
    h("span", { class: "muted small" }, [
      asset.width && `${asset.width}×${asset.height}`,
      asset.duration_ms != null && fmtMs(asset.duration_ms),
      fmtBytes(asset.size_bytes),
    ].filter(Boolean).join(" · ")),
    h("a", { class: "btn ghost small", href: assetUrl(asset.asset_id), target: "_blank", rel: "noopener" }, "Open file"),
  );
  if (asset.kind === "image") {
    return h("div", { class: "source" }, head,
      h("div", { class: "source-media" },
        h("a", { href: assetUrl(asset.asset_id), target: "_blank", rel: "noopener" },
          h("img", { src: assetUrl(asset.asset_id), alt: asset.filename, class: "source-image", loading: "lazy" })),
        prompt));
  }
  return h("div", { class: "source" }, head, prompt,
    h("p", { class: "muted small" }, `Audio: ${audioLabel(asset)}`),
    stickyDetails(`${asset.asset_id}:frames`, `Keyframes the model sees (${asset.frames.length})`,
      h("div", { class: "frame-strip" }, asset.frames.map((f, n) =>
        h("figure", {},
          h("img", { src: assetUrl(asset.asset_id, `frames/${n}`), alt: `frame at ${fmtMs(f.t_ms)}`, loading: "lazy" }),
          h("figcaption", {}, fmtMs(f.t_ms)))))),
    asset.transcript.length > 0 && stickyDetails(`${asset.asset_id}:transcript`, `Transcript (${asset.transcript.length} segments)`,
      h("div", { class: "transcript" }, asset.transcript.map((seg) =>
        h("div", {}, h("span", { class: "mono muted" }, `${fmtMs(seg.start_ms)}–${fmtMs(seg.end_ms)} `), seg.text)))),
  );
}

// A <details> that stays open across re-renders (polling redraws the job header).
function stickyDetails(key, summary, ...body) {
  const d = h("details", { class: "frames", open: state.openDetails.has(key) }, h("summary", {}, summary), ...body);
  d.addEventListener("toggle", () => { d.open ? state.openDetails.add(key) : state.openDetails.delete(key); });
  return d;
}

function audioLabel(a) {
  return {
    no_audio: "no audio track",
    pending: isActive(state.job?.status) ? "not transcribed yet (happens at the ingest step)" : "not analyzed (the offline sample client doesn't transcribe)",
    done: a.transcript_until_ms != null ? `transcribed up to ${fmtMs(a.transcript_until_ms)}` : `transcribed (${a.transcript_model})`,
    failed: `transcription failed: ${a.transcript_error || "unknown error"}`,
  }[a.transcript_status] || "not analyzed";
}

async function jobAction(jobId, action, button) {
  button.disabled = true;
  state.retryError = "";
  try {
    await api(`/v1/drama-jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
    await loadJob(jobId);
  } catch (e) {
    state.retryError = `${action}: ${e.message}`;
    button.disabled = false;
    renderMain();
  }
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
  // No package yet (running, paused, or failed early): the activity view is the page.
  return activityPanel(job.job_id);
}

// ---------------------------------------------------------------------------
// Package tabs

const TABS = [
  ["overview", "Overview"],
  ["script", "Script"],
  ["storyboard", "Storyboard"],
  ["cast", "Cast"],
  ["sets", "Sets"],
  ["props", "Props"],
  ["assets", "Assets"],
  ["qa", "QA"],
  ["revise", "Revise"],
  ["activity", "Activity"],
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
    cast: pkg.cast_sheet?.characters.length,
    sets: pkg.set_sheet?.locations.length,
    props: pkg.prop_sheet?.props.length,
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
    cast: renderCast, sets: renderSets, props: renderProps,
    assets: renderAssets, qa: renderQA, revise: renderRevise,
    activity: () => activityPanel(state.job.job_id),
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
          c.role && h("p", { class: "muted" }, c.role),
          legacyText(c),
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
          l.summary && h("p", {}, l.summary),
          legacyText(l),
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
            p.story_function && h("p", {}, p.story_function),
            legacyText(p),
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
// Visual text from packages made before the design sheets (kept under `legacy`).
const legacyText = (obj) => obj.legacy && Object.values(obj.legacy).map((t) => h("p", { class: "muted" }, t));
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
            sc.intimacy && sc.intimacy.level !== "none" && h("span", { class: "chip warn", title: "See the intimacy plan in the zip (08_intimacy)" }, `intimacy: ${sc.intimacy.level}`),
          ),
          sc.intimacy && sc.intimacy.level !== "none" && h("div", { class: "intimacy-note small" },
            h("div", {}, h("strong", {}, "On screen: "), sc.intimacy.on_screen || "(not described)"),
            sc.intimacy.off_screen && h("div", {}, h("strong", {}, "Implied: "), sc.intimacy.off_screen),
            h("div", { class: "muted" }, [
              `Nudity: ${sc.intimacy.nudity}`,
              sc.intimacy.closed_set ? "closed set" : null,
              sc.intimacy.consent_beats.length ? `consent beats: ${sc.intimacy.consent_beats.join("; ")}` : "no consent beats listed",
            ].filter(Boolean).join(" · ")),
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

// Design sheets (cast / sets / props) --------------------------------------------

// Field label + value rows; lists render as chips, empty values are left out.
function sheetFields(obj, fields) {
  return h("dl", { class: "kv sheet-kv" }, fields.map(([key, label]) => {
    const v = obj[key];
    if (v == null || v === "" || (Array.isArray(v) && !v.length)) return null;
    const value = Array.isArray(v) ? h("div", { class: "chips" }, v.map((x) => h("span", { class: `chip ${key === "must_keep" ? "warn" : ""}` }, x))) : v;
    return kv(label, value);
  }));
}

function sheetHeader(pkg, key, title, blurb) {
  const sheet = pkg[key.replace("-", "_")];
  const q = new URLSearchParams({ revision_id: pkg.revision_id });
  return h("section", { class: "card sheet-head" },
    h("div", {},
      h("h2", {}, title),
      h("p", { class: "muted small" }, blurb),
    ),
    h("div", { class: "job-actions" },
      sheet && h("span", { class: "chip mono" }, `schema ${sheet.schema_version}`),
      sheet && h("a", { class: "btn small", href: `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/${key}?${q}`, download: `${pkg.package_id}-${key}.json` }, "JSON"),
      h("a", { class: "btn ghost small", href: `/v1/schemas/${key}.json`, target: "_blank", rel: "noopener" }, "Schema"),
    ),
  );
}

const noSheet = (what) => h("div", { class: "card placeholder" }, h("p", {}, `This package was made before ${what} sheets existed. Retry or create a new job to get one.`));

function variantCard(id, parentName, obj, fields) {
  return h("div", { class: "card entity variant" },
    h("h3", {}, parentName, h("code", { class: "muted" }, id)),
    sheetFields(obj, fields));
}

const CAST_FIELDS = [
  ["age_range", "Age"], ["gender_presentation", "Gender"], ["build_and_height", "Build"],
  ["face", "Face"], ["hair", "Hair"], ["distinguishing_features", "Distinguishing"],
  ["default_wardrobe", "Wardrobe"], ["wardrobe_palette", "Palette"],
  ["posture_and_mannerisms", "Mannerisms"], ["voice", "Voice"],
  ["must_keep", "Must keep"], ["design_freedom", "Free to design"],
];
const LOOK_FIELDS = [
  ["wardrobe", "Wardrobe"], ["hair_and_makeup", "Hair & makeup"], ["condition", "Condition"],
  ["changes_from_base", "Changes"], ["must_keep", "Must keep"],
];

function renderCast(pkg, L) {
  const sheet = pkg.cast_sheet;
  const head = sheetHeader(pkg, "cast-sheet", "Cast sheet", "How each character looks and sounds, for character design and casting. Story roles and goals are in the Overview.");
  if (!sheet) return h("div", {}, head, noSheet("cast"));
  const looksBy = groupBy(sheet.looks, "character_id");
  return h("div", {}, head,
    sheet.characters.map((c) =>
      h("section", { class: "card entity sheet-entry" },
        h("h3", {}, c.name || L.name[c.character_id] || c.character_id, h("code", { class: "muted" }, c.character_id),
          !c.on_screen && h("span", { class: "chip" }, "not seen on screen")),
        sheetFields(c, CAST_FIELDS),
        (looksBy[c.character_id] || []).length > 0 && h("div", { class: "grid variants-grid" },
          looksBy[c.character_id].map((lk) => variantCard(lk.look_id, "Look", lk, LOOK_FIELDS))),
      )),
  );
}

const SET_FIELDS = [
  ["era_and_region", "Era & region"], ["layout", "Layout"], ["key_furnishings", "Furnishings"],
  ["materials_and_textures", "Materials"], ["color_palette", "Palette"], ["lighting", "Lighting"],
  ["entrances_and_blocking", "Entrances & blocking"], ["ambient_sound", "Ambient sound"],
  ["must_keep", "Must keep"], ["design_freedom", "Free to design"],
];
const SET_STATE_FIELDS = [
  ["time_of_day", "Time"], ["lighting", "Lighting"], ["dressing_changes", "Dressing"],
  ["condition", "Condition"], ["changes_from_base", "Changes"], ["must_keep", "Must keep"],
];

function renderSets(pkg, L) {
  const sheet = pkg.set_sheet;
  const head = sheetHeader(pkg, "set-sheet", "Set sheet", "How each location looks, for production design. Layout and entrances support the script's blocking.");
  if (!sheet) return h("div", {}, head, noSheet("set"));
  const statesBy = groupBy(sheet.states, "location_id");
  return h("div", {}, head,
    sheet.locations.map((loc) =>
      h("section", { class: "card entity sheet-entry" },
        h("h3", {}, loc.name || L.name[loc.location_id] || loc.location_id, h("code", { class: "muted" }, loc.location_id),
          h("span", { class: "chip" }, loc.interior_exterior)),
        sheetFields(loc, SET_FIELDS),
        (statesBy[loc.location_id] || []).length > 0 && h("div", { class: "grid variants-grid" },
          statesBy[loc.location_id].map((st) => variantCard(st.location_state_id, "State", st, SET_STATE_FIELDS))),
      )),
  );
}

const PROP_FIELDS = [
  ["size_and_scale", "Size"], ["materials", "Materials"], ["color_and_finish", "Color & finish"],
  ["condition_and_wear", "Condition"], ["distinguishing_marks", "Distinguishing"],
  ["handling", "Handling"], ["must_keep", "Must keep"], ["design_freedom", "Free to design"],
];
const PROP_STATE_FIELDS = [["condition", "Condition"], ["changes_from_base", "Changes"], ["must_keep", "Must keep"]];

function renderProps(pkg, L) {
  const sheet = pkg.prop_sheet;
  const head = sheetHeader(pkg, "prop-sheet", "Prop sheet", "How each plot-relevant prop looks and is handled, for prop making.");
  if (!sheet) return h("div", {}, head, noSheet("prop"));
  if (!sheet.props.length) return h("div", {}, head, h("div", { class: "card placeholder" }, h("p", {}, "This story registers no key props.")));
  const statesBy = groupBy(sheet.states, "prop_id");
  return h("div", {}, head,
    sheet.props.map((p) =>
      h("section", { class: "card entity sheet-entry" },
        h("h3", {}, p.name || L.name[p.prop_id] || p.prop_id, h("code", { class: "muted" }, p.prop_id)),
        sheetFields(p, PROP_FIELDS),
        (statesBy[p.prop_id] || []).length > 0 && h("div", { class: "grid variants-grid" },
          statesBy[p.prop_id].map((st) => variantCard(st.prop_state_id, "State", st, PROP_STATE_FIELDS))),
      )),
  );
}

// Assets --------------------------------------------------------------------

const ASSET_TYPES = ["character_identity", "character_look", "location", "prop", "shot_reference"];

// Reference images (generated on demand) ----------------------------------------

const imagesKey = (pkg) => `${pkg.package_id}|${pkg.revision_id}`;
const IMAGE_TYPES = new Set(["character_identity", "character_look", "location", "prop"]);

// Loads the package's images; polls while any are waiting or generating, as long as the
// Assets tab of this package stays open.
async function loadImages(pkg) {
  const key = imagesKey(pkg);
  const im = state.images;
  if (im.key !== key) Object.assign(im, { key, view: null, error: "" });
  clearTimeout(im.timer);
  im.timer = null;
  im.loading = true;
  try {
    const q = new URLSearchParams({ revision_id: pkg.revision_id });
    const view = await api(`/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/images?${q}`);
    if (state.images.key !== key) return;
    const changed = JSON.stringify(view) !== JSON.stringify(im.view);
    im.view = view;
    im.error = "";
    if (changed && state.tab === "assets") renderWhenNotSelecting();
  } catch (e) {
    if (state.images.key !== key) return;
    im.error = e.message;
    if (state.tab === "assets") renderWhenNotSelecting();  // otherwise "Loading…" forever
  } finally {
    im.loading = false;
  }
  const busy = im.view && im.view.queued > 0;
  if (busy && state.tab === "assets" && state.pkg && imagesKey(state.pkg) === key) {
    im.timer = setTimeout(() => loadImages(state.pkg), 2000);
  }
}

async function imageAction(url, button, body) {
  if (button) button.disabled = true;
  state.images.error = "";
  try {
    const out = await api(url, { method: "POST", ...(body ? { body: JSON.stringify(body) } : {}) });
    // Develop/Explore notes (e.g. "pick an image for X first") stay until the next run.
    if (out && Array.isArray(out.notes)) state.images.notes = out.notes;
  } catch (e) {
    state.images.error = e.message;
  }
  await loadImages(state.pkg);
  renderMain();
}

// Design board: takes x draws per anchor (faces, first location/prop states), pick one,
// then develop looks and other states from the picks. ------------------------------------

const DRAFT_S = 3, FULL_S = 10, WRITER_S = 60;  // rough seconds per image / per writer call
const design = { takes: 4, draws: 3, draft: true, scope: { cast: true, sets: true, props: true }, devDraws: 3, directions: {}, edits: {} };
const SCOPE_OF = { character_identity: "cast", character_look: "cast", location: "sets", prop: "props" };
const keyOf = (x) => `${x.requirement_id}|${x.deliverable_id}`;
const thumbUrl = (img, v = "thumb") => `/v1/generated-images/${encodeURIComponent(img.image_id)}/${v}`;

function chosenImage(images, key) {
  const done = images.filter((i) => keyOf(i) === key && i.status === "done");
  const sel = done.filter((i) => i.selected);
  const pool = sel.length ? sel : done;
  return pool.sort((a, b) => b.created_at.localeCompare(a.created_at))[0] || null;
}

function numberSelect(value, max, onchange, label) {
  return h("select", { "aria-label": label, onchange: (e) => onchange(Number(e.target.value)) },
    Array.from({ length: max }, (_, i) => i + 1).map((n) => h("option", { value: n, selected: n === value }, String(n))));
}

function renderDesignControls(pkg, v) {
  const base = `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/images`;
  const ids = { revision_id: pkg.revision_id, job_id: state.job.job_id };
  const anchors = v ? v.slots.filter((s) => s.anchor && design.scope[SCOPE_OF[s.asset_type]]) : [];
  const n = anchors.length * design.takes * design.draws;
  const mins = Math.max(1, Math.round((n * (design.draft ? DRAFT_S : FULL_S) + WRITER_S) / 60));
  const selectedAnchors = v ? v.slots.filter((s) => s.anchor && v.images.some((i) => keyOf(i) === keyOf(s) && i.selected)).length : 0;
  const busy = v && v.queued > 0;
  const check = (name, label) => h("label", { class: "check small" },
    h("input", { type: "checkbox", checked: design.scope[name], onchange: (e) => { design.scope[name] = e.target.checked; renderMain(); } }), ` ${label}`);
  return h("section", { class: "card design-controls" },
    h("h3", {}, "Casting & design"),
    h("p", { class: "muted small" }, "1. Explore several takes of every face, set and prop, with a few draws each. 2. Pick one image per item (a draft pick is re-rendered at full size). 3. Develop looks, other states and plan views from your picks."),
    h("div", { class: "design-row" },
      h("strong", { class: "small" }, "Explore"),
      h("span", { class: "small muted" }, "takes"), numberSelect(design.takes, 6, (x) => { design.takes = x; renderMain(); }, "Takes per item"),
      h("span", { class: "small muted" }, "draws"), numberSelect(design.draws, 6, (x) => { design.draws = x; renderMain(); }, "Draws per take"),
      h("select", { "aria-label": "Size", onchange: (e) => { design.draft = e.target.value === "draft"; renderMain(); } },
        h("option", { value: "draft", selected: design.draft }, "Draft (fast)"), h("option", { value: "full", selected: !design.draft }, "Full size")),
      check("cast", "Cast"), check("sets", "Sets"), check("props", "Props"),
      h("span", { class: "small muted" }, n ? `${n} images, about ${mins} min` : "nothing in scope"),
      h("button", { type: "button", class: "btn small primary-outline", disabled: !n,
        onclick: (e) => imageAction(`${base}/explore`, e.currentTarget, { ...ids, takes: design.takes, draws: design.draws, draft: design.draft, scope: Object.keys(design.scope).filter((k) => design.scope[k]) }) }, "Explore"),
    ),
    h("div", { class: "design-row" },
      h("strong", { class: "small" }, "Develop"),
      h("span", { class: "small muted" }, "draws"), numberSelect(design.devDraws, 6, (x) => { design.devDraws = x; renderMain(); }, "Draws per look or state"),
      h("span", { class: "small muted" }, `${selectedAnchors} of ${v ? v.slots.filter((s) => s.anchor).length : 0} anchors picked`),
      h("button", { type: "button", class: "btn small", disabled: !selectedAnchors,
        onclick: (e) => imageAction(`${base}/develop`, e.currentTarget, { ...ids, draws: design.devDraws, draft: design.draft }) }, "Develop looks & states"),
    ),
    h("p", { class: "small" }, !v ? (state.images.error ? "Couldn't load images." : "Loading…") : busy ? `${v.queued} images in the queue…` : "Idle."),
    (state.images.notes || []).length > 0 && h("ul", { class: "small" }, state.images.notes.map((t) => h("li", {}, t))),
    state.images.error && h("p", { class: "form-error" }, state.images.error),
    !v && state.images.error && h("button", { type: "button", class: "btn small", onclick: () => { state.images.error = ""; loadImages(pkg); renderMain(); } }, "Retry"),
  );
}

function renderDraw(img) {
  const status = { pending: "waiting", running: img.prompt_source === "pending" ? "writing…" : "generating…", failed: "failed" }[img.status];
  const selected = img.selected;  // only an explicit pick gets the check mark
  const label = `Draw ${img.draw}${img.finalizes ? " · full size" : ""}`;
  return h("figure", { class: `draw ${img.status}${selected ? " selected" : ""}` },
    img.status === "done"
      ? h("button", { type: "button", class: "draw-pick", title: selected ? "Selected" : "Select this image",
          onclick: (e) => imageAction(`/v1/generated-images/${encodeURIComponent(img.image_id)}/select`, e.currentTarget) },
          h("img", { src: thumbUrl(img), alt: label, loading: "lazy" }))
      : h("div", { class: "draw-placeholder", title: img.error || "" }, status,
          img.status === "failed" && h("button", { type: "button", class: "btn ghost small", title: img.error || "Try this draw again (same prompt and seed)",
            onclick: (e) => imageAction(`/v1/generated-images/${encodeURIComponent(img.image_id)}/retry`, e.currentTarget) }, "Retry")),
    h("figcaption", {}, label, selected ? " ✓" : "",
      img.status === "done" && h("a", { href: thumbUrl(img, "content"), target: "_blank", rel: "noopener", class: "muted" }, " open")),
  );
}

function renderTakeRows(v, slot) {
  const key = keyOf(slot);
  const opts = v.options.filter((o) => keyOf(o) === key).sort((a, b) => a.index - b.index);
  if (!opts.length) return [h("p", { class: "muted small" }, slot.anchor ? "No takes yet: use Explore." : "Not developed yet.")];
  return opts.map((o) => {
    const draws = v.images.filter((i) => i.option_id === o.option_id).sort((a, b) => a.draw - b.draw || (a.finalizes ? 1 : -1));
    return h("div", { class: "take" },
      h("div", { class: "take-head" },
        h("strong", {}, o.status === "pending" ? `Take ${o.index}: being written…`
          : o.label && o.label !== `Take ${o.index}` ? `Take ${o.index}: ${o.label}` : `Take ${o.index}`),
        o.direction && h("span", { class: "chip" }, `“${o.direction}”`),
        o.status === "edited" && h("span", { class: "chip" }, "your prompt"),
        h("button", { type: "button", class: "btn ghost small",
          onclick: (e) => imageAction(`/v1/image-options/${encodeURIComponent(o.option_id)}/draws`, e.currentTarget, { draws: design.draws }) }, "+ draws"),
      ),
      o.profile && h("p", { class: "small muted take-profile" }, o.profile),
      h("div", { class: "draws" }, draws.map((img) => renderDraw(img))),
      o.status !== "pending" && renderPromptEditor(v, o, draws),
    );
  });
}

const PROMPT_SOURCE = (o) => ({ written: `written by ${o.prompt_model || "the text model"}`, edited: "edited by you" }[o.status] || "built-in template");

// A take's prompt, editable: "Draw edited take" makes a new take with this wording, drawn
// with the same seeds as this take's draws, so the images differ only by the change.
function renderPromptEditor(v, o, draws) {
  const id = o.option_id;
  const box = h("textarea", { class: "prompt-edit", rows: 8, spellcheck: "false",
    oninput: (e) => { design.edits[id] = e.target.value; sync(); } });
  box.value = design.edits[id] ?? o.prompt;
  const n = draws.filter((i) => !i.finalizes).length || design.draws;
  const drawBtn = h("button", { type: "button", class: "btn small", onclick: (e) => {
    const prompt = box.value;
    imageAction(`/v1/image-options/${encodeURIComponent(id)}/edit`, e.currentTarget, { prompt, draws: Math.min(6, n) })
      .then(() => { if (!state.images.error) delete design.edits[id]; renderMain(); });
  } }, `Draw edited take (${Math.min(6, n)})`);
  const resetBtn = h("button", { type: "button", class: "btn ghost small",
    onclick: () => { delete design.edits[id]; box.value = o.prompt; sync(); } }, "Reset");
  const counter = h("span", { class: "muted small" });
  function sync() {
    const changed = box.value.trim() !== o.prompt.trim();
    drawBtn.disabled = !changed || !box.value.trim();
    resetBtn.disabled = !changed;
    counter.textContent = `${box.value.length.toLocaleString()} characters${changed ? " · edited" : ""}`;
  }
  sync();
  const source = o.edited_from && v.options.find((x) => x.option_id === o.edited_from);
  return stickyDetails(`prompt:${id}`, `Prompt (${PROMPT_SOURCE(o)}${source ? `, from take ${source.index}` : ""})`,
    o.warnings && o.warnings.length > 0 && h("p", { class: "small warn-text" }, `Doesn't follow the H3 rules: ${o.warnings.join("; ")}.`),
    box,
    h("div", { class: "prompt-edit-actions" }, drawBtn, resetBtn, counter),
    h("p", { class: "muted small" }, "Drawing an edit adds a new take with the same seeds as this one, so you can compare the images draw for draw."));
}

function renderNewTake(pkg, slot) {
  const key = keyOf(slot);
  const input = h("input", { type: "text", placeholder: slot.asset_type === "character_identity" ? "Direction for a new take, e.g. older, kinder eyes" : "Direction for a new take (optional)",
    oninput: (e) => { design.directions[key] = e.target.value; } });
  input.value = design.directions[key] || "";
  const base = `/v1/drama-packages/${encodeURIComponent(pkg.package_id)}/images/takes`;
  return h("div", { class: "new-take" }, input,
    h("button", { type: "button", class: "btn small", onclick: (e) => {
      const direction = (design.directions[key] || "").trim();
      imageAction(base, e.currentTarget, { requirement_id: slot.requirement_id, deliverable_id: slot.deliverable_id, direction: direction || null, draws: design.draws, draft: design.draft, revision_id: pkg.revision_id, job_id: state.job.job_id })
        .then(() => { if (!state.images.error) { design.directions[key] = ""; renderMain(); } });  // keep it if the request failed
    } }, "New take"));
}

function renderDesignBoard(pkg) {
  const im = state.images;
  if (im.key !== imagesKey(pkg)) loadImages(pkg);
  const v = im.key === imagesKey(pkg) ? im.view : null;
  const stale = v && v.queued && !im.timer && !im.loading;
  if (stale) loadImages(pkg);  // resume polling after coming back to the tab
  const pickedKeys = new Set(!v ? [] : v.images.filter((i) => i.selected && i.status === "done").map(keyOf));
  const titleOf = (k) => { const x = v.slots.find((s) => keyOf(s) === k); return x ? `${x.entity}${x.variant ? ` (${x.variant})` : ""}, ${x.view}` : k; };
  const renderDependent = (d) => {
    const parent = d.parent_key && `${d.parent_key[0]}|${d.parent_key[1]}`;
    const blocked = parent && !pickedKeys.has(parent);
    return h("div", { class: "dependent" },
      h("div", { class: "small" }, h("strong", {}, d.variant || d.view), h("span", { class: "muted" }, ` · ${d.view}`)),
      renderTakeRows(v, d),
      blocked ? h("p", { class: "muted small" }, `Drawn from ${titleOf(parent)}: pick an image for it first.`) : renderNewTake(pkg, d));
  };
  // Items with no anchor to be drawn from (e.g. a look of a character with no face image).
  const others = !v ? [] : v.slots.filter((s) => !s.anchor && !s.anchor_key);
  const otherCard = others.length > 0 && h("section", { class: "card design-card" },
    h("div", { class: "design-card-head" }, h("h3", {}, "Other items")),
    others.map((d) => h("div", { class: "dependent" },
      h("div", { class: "small" }, h("strong", {}, d.entity), h("span", { class: "muted" }, ` · ${d.variant ? `${d.variant}, ` : ""}${d.view}`)),
      renderTakeRows(v, d), renderNewTake(pkg, d))));
  const cards = !v ? [] : v.slots.filter((s) => s.anchor).map((anchor) => {
    const chosen = chosenImage(v.images, keyOf(anchor));
    const opt = chosen && v.options.find((o) => o.option_id === chosen.option_id);
    const deps = v.slots.filter((s) => s.anchor_key && `${s.anchor_key[0]}|${s.anchor_key[1]}` === keyOf(anchor));
    return h("section", { class: "card design-card" },
      h("div", { class: "design-card-head" },
        h("h3", {}, anchor.entity, h("span", { class: "muted small" }, ` · ${anchor.variant ? `${anchor.variant}, ` : ""}${anchor.view}`)),
        chosen && chosen.selected
          ? h("span", { class: "chip ok" }, opt ? `Selected: take ${opt.index}, draw ${chosen.draw}` : "Selected")
          : h("span", { class: "chip warn" }, "Pick one"),
      ),
      renderTakeRows(v, anchor),
      renderNewTake(pkg, anchor),
      deps.length > 0 && h("div", { class: "dependents" },
        h("h4", {}, "Drawn from the pick"),
        deps.map(renderDependent)),
    );
  });
  return h("div", {}, renderDesignControls(pkg, v), cards, otherCard);
}

function renderReqImages(r) {
  // On a requirement card: the chosen image of each deliverable (selected, else newest).
  const v = state.images.view;
  if (!v || !IMAGE_TYPES.has(r.asset_type)) return null;
  const shown = r.deliverables.map((d) => [d, chosenImage(v.images, `${r.requirement_id}|${d.deliverable_id}`)]).filter(([, img]) => img);
  if (!shown.length) return null;
  return h("div", { class: "req-images" }, shown.map(([d, img]) =>
    h("figure", { class: "req-image done" },
      h("a", { href: thumbUrl(img, "content"), target: "_blank", rel: "noopener", title: "Open full size" },
        h("img", { src: thumbUrl(img), alt: `${r.requirement_id} ${d.view}`, loading: "lazy" })),
      h("figcaption", {}, h("span", {}, d.view, img.selected ? " · selected" : "")))));
}

function renderAssets(pkg) {
  const ar = pkg.asset_requirements;
  const items = state.assetFilter === "all" ? ar.items : ar.items.filter((r) => r.asset_type === state.assetFilter);
  const count = (t) => ar.items.filter((r) => r.asset_type === t).length;
  const sb = ar.style_brief;
  return h("div", {},
    renderDesignBoard(pkg),
    h("section", { class: "card", style: "margin-top:12px" },
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
          renderReqImages(r),
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

  const instruction = h("textarea", { rows: 3, required: true, placeholder: "Make the father's motive in episode 2 more believable, without revealing who he is too early", oninput: (e) => { r.instruction = e.target.value; } });
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

// ---------------------------------------------------------------------------
// Activity: live agent steps and model calls (also the debugging record later).
//
// Built once per job and updated in place: streaming updates arrive every second, and
// rebuilding would wipe any text the user has selected.

const ACTIVITY_POLL_MS = 1000;
const EVENT_TONE = {
  stage_passed: "ok", job_completed: "ok", resumed: "info", job_resumed: "info",
  gate_issues: "warn", schema_repair: "warn", model_retry: "warn", retry_scheduled: "warn",
  normalized: "", paused: "info", pause_requested: "info", requeued: "warn",
  stage_failed: "bad", job_failed: "bad", gave_up: "bad", lease_lost: "warn",
};

function stopActivity() {
  if (state.activity) clearTimeout(state.activity.timer);
  state.activity = null;
}

function activityPanel(jobId) {
  if (state.activity?.jobId !== jobId) {
    stopActivity();
    const a = { jobId, seq: 0, calls: new Map(), timer: null, polling: false };
    a.status = h("span", { class: "chip" }, "loading");
    a.timeline = h("ol", { class: "timeline-list" });
    a.callList = h("div", { class: "call-list" });
    a.empty = h("p", { class: "muted small" }, "No model calls yet.");
    a.el = h("section", { class: "card activity" },
      h("div", { class: "section-head" }, h("h3", {}, "Activity"), a.status),
      h("div", { class: "activity-grid" },
        h("div", {}, h("h4", { class: "subhead" }, "Agent steps"), a.timeline),
        h("div", {}, h("h4", { class: "subhead" }, "Model calls"), a.empty, a.callList),
      ),
    );
    state.activity = a;
    pollActivity(a);
  }
  return state.activity.el;
}

async function pollActivity(a) {
  if (a.polling || state.activity !== a) return;
  a.polling = true;
  try {
    const t = await api(`/v1/drama-jobs/${encodeURIComponent(a.jobId)}/trace?after_seq=${a.seq}`);
    if (state.activity !== a) return;  // the user moved to another job
    for (const ev of t.events) a.timeline.append(eventRow(ev));
    a.seq = t.next_seq;
    for (const view of t.calls) upsertCall(a, view);
    a.empty.hidden = t.calls.length > 0;
    const live = isActive(state.job?.status) || t.calls.some((v) => v.call.status === "running");
    a.status.className = `chip ${live ? "accent" : ""}`;
    a.status.textContent = live ? "● live" : state.job?.status === "paused" ? "paused" : "finished";
    if (live) a.timer = setTimeout(() => pollActivity(a), ACTIVITY_POLL_MS);
  } catch (e) {
    a.status.textContent = `trace unavailable: ${e.message}`;
    a.timer = setTimeout(() => pollActivity(a), ACTIVITY_POLL_MS * 5);
  } finally {
    a.polling = false;
  }
}

// When a paused/finished job starts running again, the activity resumes polling.
function kickActivity() {
  const a = state.activity;
  if (a && !a.polling && isActive(state.job?.status)) {
    clearTimeout(a.timer);
    pollActivity(a);
  }
}

const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

function eventRow(ev) {
  const extra = [];
  if (ev.data?.issues?.length) {
    extra.push(h("ul", { class: "plain small" }, ev.data.issues.slice(0, 5).map((i) => h("li", { class: "mono" }, i))));
  }
  if (ev.data?.errors?.length) {
    extra.push(h("div", { class: "small muted mono" }, `fields: ${ev.data.errors.join(", ")}`));
  }
  if (ev.data?.tokens && Object.keys(ev.data.tokens).length) {
    const t = ev.data.tokens;
    extra.push(h("div", { class: "small muted" },
      `${(t.prompt_tokens || 0).toLocaleString()} in · ${(t.completion_tokens || 0).toLocaleString()} out` +
      (t.reasoning_tokens ? ` · ${t.reasoning_tokens.toLocaleString()} reasoning` : "")));
  }
  return h("li", { class: `event ${EVENT_TONE[ev.kind] || ""}` },
    h("span", { class: "event-time mono" }, clock(ev.ts)),
    h("div", {},
      ev.stage && h("span", { class: "chip" }, ev.stage), " ",
      h("span", {}, ev.message),
      extra),
  );
}

function callTitle(c) {
  const parts = [c.stage, `attempt ${c.attempt + 1}`];
  if (c.turn) parts.push(`repair ${c.turn}`);
  return parts.join(" · ");
}

function callMeta(c) {
  const end = c.finished_at ? new Date(c.finished_at) : new Date();
  const secs = Math.max(0, Math.round((end - new Date(c.started_at)) / 1000));
  const u = c.usage || {};
  const bits = [c.model, `${secs}s`];
  if (c.status !== "running" && (u.prompt_tokens || u.completion_tokens)) {
    bits.push(`${(u.prompt_tokens || 0).toLocaleString()} in / ${(u.completion_tokens || 0).toLocaleString()} out`);
    if (u.reasoning_tokens) bits.push(`${u.reasoning_tokens.toLocaleString()} reasoning`);
  }
  bits.push(`${(c.output_chars || 0).toLocaleString()} chars`);
  return bits.join(" · ");
}

// Replace text only when it changed and the user isn't selecting inside it.
function setText(node, text) {
  if (node.textContent !== text && !selectingIn(node)) node.textContent = text;
}

function lazyDetails(label, load) {
  const body = h("pre", { class: "call-body" }, "Loading…");
  const d = h("details", { class: "call-details" }, h("summary", {}, label), body);
  let loaded = false;
  d.addEventListener("toggle", async () => {
    if (!d.open || loaded) return;
    loaded = true;
    try {
      body.textContent = await load();
    } catch (e) {
      body.textContent = `Couldn't load: ${e.message}`;
      loaded = false;
    }
  });
  return d;
}

// Message content is a string, or (with images) a list of text and image parts.
function messageText(content) {
  if (!Array.isArray(content)) return String(content ?? "");
  return content.map((p) => p.text ?? (p.file ? `[image: ${p.file}]` : JSON.stringify(p))).join("\n");
}

const fetchCall = (id) => api(`/v1/llm-calls/${encodeURIComponent(id)}`);

function upsertCall(a, view) {
  const c = view.call;
  let ref = a.calls.get(c.call_id);
  if (!ref) {
    ref = {};
    ref.status = h("span", { class: "chip" });
    ref.meta = h("div", { class: "small muted mono" });
    ref.notesWrap = h("div", { class: "call-notes", hidden: true },
      h("div", { class: "subhead" }, "Model notes"), (ref.notes = h("p", {})));
    ref.reasonWrap = h("div", { class: "call-notes", hidden: true },
      h("div", { class: "subhead" }, "Reasoning summary"), (ref.reason = h("p", {})));
    ref.live = h("pre", { class: "call-body live", hidden: true });
    ref.error = h("p", { class: "form-error" });
    ref.done = h("div", { class: "call-done", hidden: true },
      lazyDetails("Output", async () => {
        const full = await fetchCall(c.call_id);
        try { return JSON.stringify(JSON.parse(full.output), null, 2); } catch { return full.output || "(empty)"; }
      }),
      lazyDetails("Prompt sent to the model", async () => {
        const full = await fetchCall(c.call_id);
        return (full.request.messages || []).map((m) => `── ${m.role} ──\n${messageText(m.content)}`).join("\n\n") || JSON.stringify(full.request, null, 2);
      }),
    );
    ref.el = h("article", { class: "call" },
      h("div", { class: "call-head" }, h("strong", {}, callTitle(c)), ref.status),
      ref.meta, ref.notesWrap, ref.reasonWrap, ref.live, ref.error, ref.done);
    a.calls.set(c.call_id, ref);
    a.callList.prepend(ref.el);  // newest first
  }
  const running = c.status === "running";
  ref.status.className = `chip ${running ? "accent" : c.status === "failed" ? "bad" : "ok"}`;
  setText(ref.status, running ? "streaming" : c.status);
  setText(ref.meta, callMeta(c));
  ref.notesWrap.hidden = !c.commentary;
  setText(ref.notes, c.commentary || "");
  ref.reasonWrap.hidden = !c.reasoning_summary;
  setText(ref.reason, c.reasoning_summary || "");
  setText(ref.error, c.error || "");
  ref.live.hidden = !running;
  ref.done.hidden = running;
  if (running) {
    const pre = ref.live;
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    setText(pre, view.output_tail || "Waiting for the first tokens…");
    if (atBottom && !selectingIn(pre)) pre.scrollTop = pre.scrollHeight;
  }
}
