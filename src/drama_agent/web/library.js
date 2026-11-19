"use strict";

/*
 * Characters and Sets: the asset library (docs/entity-reorganization.md, phase 2).
 * Every character or set, and one with its sheet, its looks or states, its items, current
 * pictures (click to enlarge; out-of-date ones marked) and the stories and shots that use it.
 * Each part is edited in place: the edit is saved to the story it came from (PATCH
 * …/entries/{id}, the same as the Cast and Sets tabs) and the library follows. "Redraw
 * out-of-date" redraws this asset's out-of-date pictures (and what is drawn from them).
 */

const lib = { kind: null, id: null, list: null, view: null, error: "", filter: "", story: "", timer: null, redrawing: "",
  designs: {}, designTimer: null };  // design board data per story revision on the page
// Open editors by asset id; typed text is kept in `drafts` until saved or cancelled.
const libEdit = { open: new Set(), drafts: {}, errors: {}, notes: {}, saving: new Set() };

const LIB_KINDS = {
  character: { mode: "chars", path: "characters", plural: "Characters", child: "looks", childLabel: "Looks", one: "look", many: "looks", storyTab: "casting" },
  set: { mode: "sets", path: "sets", plural: "Sets", child: "states", childLabel: "States", one: "state", many: "states", storyTab: "casting" },
};
// Sheet fields shown as lists of chips, and fields never shown (ids, names, versions).
const LIB_LISTS = new Set(["must_keep", "design_freedom"]);
const LIB_HIDDEN = new Set(["name", "schema_version", "legacy"]);
const libLabel = (k) => k.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
// "1 look", "2 looks"; "1 story", "2 stories".
const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const kindOfId = (id) => (id.startsWith("chr_") ? "character" : id.startsWith("set_") ? "set" : null);

// "#lib=characters", "#lib=sets", or "#lib=<asset id>".
async function libRoute(param) {
  const kind = param === "sets" ? "set" : param === "characters" || !param ? "character" : kindOfId(param);
  if (!kind) {
    $("#main").replaceChildren(h("section", { class: "card" }, h("p", {}, `Nothing in the library is called ${param}.`)));
    return;
  }
  const meta = LIB_KINDS[kind];
  setMode(meta.mode);
  const id = kindOfId(param || "") ? param : null;
  if (lib.kind !== kind) Object.assign(lib, { kind, list: null, filter: "" });
  if (lib.id !== id) {
    Object.assign(libEdit, { open: new Set(), drafts: {}, errors: {}, notes: {} });
    lib.designs = {};
  }
  clearTimeout(lib.timer);
  clearTimeout(lib.designTimer);
  lib.id = id;
  lib.view = null;
  lib.error = "";
  lib.redrawing = "";
  renderLibrary();
  if (!state.jobs.length) await loadJobs();
  try {
    lib.list = await api(`/v1/library/${meta.path}`);
    if (id) {
      lib.view = await api(`/v1/library/${meta.path}/${encodeURIComponent(id)}`);
      libLoadDesign();
    }
  } catch (e) {
    lib.error = e.message;
  }
  if (lib.kind === kind && lib.id === id) renderLibrary();
}

function renderLibrary() {
  renderLibSide();
  const main = $("#main");
  if (lib.error) return main.replaceChildren(h("section", { class: "card" }, h("p", { class: "form-error" }, lib.error)));
  if (lib.id) return main.replaceChildren(lib.view ? renderLibAsset(lib.view) : h("p", { class: "muted" }, "Loading…"));
  main.replaceChildren(renderLibGrid());
}

// The sidebar: every character (or set), filterable, each with its cover.
function renderLibSide() {
  const meta = LIB_KINDS[lib.kind];
  $("#lib-title").textContent = meta.plural;
  const list = $("#lib-list");
  if (!lib.list) return list.replaceChildren(h("li", { class: "muted small" }, "Loading…"));
  renderLibStories();
  const f = lib.filter.trim().toLowerCase();
  const shown = libShown().filter((a) => !f || a.name.toLowerCase().includes(f));
  list.replaceChildren(...shown.map((a) =>
    h("li", {}, h("a", { href: `#lib=${a.asset_id}`, class: `lib-side-item${a.asset_id === lib.id ? " active" : ""}` },
      a.cover ? h("img", { src: a.cover.thumb, alt: "", loading: "lazy" }) : h("span", { class: "lib-noimg" }),
      h("span", {}, a.name, h("span", { class: "muted small" }, ` · ${count(a.children, meta.one, meta.many)}`))))));
  if (!shown.length) list.replaceChildren(h("li", { class: "muted small" }, f || lib.story ? "No match." : "Nothing yet."));
}

// The characters (or sets) of the story chosen in the story filter (all when none is).
const libShown = () => (lib.list || []).filter((a) => !lib.story || (a.story_list || []).some((s) => s.package_id === lib.story));

// The story filter's choices: every story the list's assets are cast in, by title.
function renderLibStories() {
  const sel = $("#lib-story");
  if (!sel) return;
  const stories = new Map();
  for (const a of lib.list || []) for (const s of a.story_list || []) stories.set(s.package_id, s.title);
  if (lib.story && !stories.has(lib.story)) lib.story = "";
  const opts = [...stories].sort((x, y) => x[1].localeCompare(y[1]));
  sel.replaceChildren(h("option", { value: "" }, "All stories"),
    ...opts.map(([pid, title]) => h("option", { value: pid }, title || pid)));
  sel.value = lib.story;
}

// Every character (or set) as a card with its cover.
function renderLibGrid() {
  const meta = LIB_KINDS[lib.kind];
  if (!lib.list) return h("p", { class: "muted" }, "Loading…");
  const shown = libShown();
  const story = lib.story && shown[0]?.story_list.find((s) => s.package_id === lib.story)?.title;
  return h("section", {},
    h("div", { class: "section-head" }, h("h2", {}, meta.plural),
      h("span", { class: "muted small" }, lib.story
        ? `${shown.length} in ${story || "this story"}.`
        : `${lib.list.length} in the library, from every story. Edit them in their story for now.`)),
    h("div", { class: "lib-grid" }, shown.map((a) =>
      h("a", { href: `#lib=${a.asset_id}`, class: "card lib-card" },
        a.cover ? h("img", { src: a.cover.thumb, alt: a.name, loading: "lazy" }) : h("div", { class: "lib-noimg big" }, "no picture yet"),
        h("strong", {}, a.name),
        h("span", { class: "muted small" }, `${count(a.children, meta.one, meta.many)} · ${count(a.items, "item", "items")} · ${count(a.stories, "story", "stories")}`)))));
}

// The job that made a story, for links into it.
const jobOf = (packageId) => (state.jobs || []).find((j) => j.package_id === packageId);
const storyLink = (packageId, tab, label) => {
  const job = jobOf(packageId);
  return job ? h("a", { href: `#job=${encodeURIComponent(job.job_id)}&tab=${tab}` }, label) : h("span", {}, label);
};

// Pictures: click to enlarge (each list steps through its own); out-of-date ones say why.
function libPictures(pictures, title) {
  if (!pictures.length) return h("p", { class: "muted small" }, "No picture yet.");
  const items = pictures.map((p) => ({ src: p.content, caption: `${title} · ${p.view}`, title: p.imported_from ? `imported from ${p.imported_from}` : `${p.size}` }));
  return h("div", { class: "lib-pics" }, pictures.map((p, i) =>
    h("figure", { class: "lib-pic" },
      h("button", { type: "button", class: "ref-open", "aria-label": `Enlarge ${title}, ${p.view}`, onclick: () => enlarge(items, i) },
        h("img", { src: p.thumb, alt: `${title}, ${p.view}`, loading: "lazy" })),
      h("figcaption", { class: "small" }, p.view,
        p.likeness && h("span", { class: "chip warn", title: "Shows a real person's likeness (content rule 3)" }, "likeness"),
        p.outdated.length > 0 && h("span", { class: "chip warn", title: p.outdated.join("; ") }, "out of date")))));
}

// A sheet as labelled fields; must-keep and design freedom as chips.
function libSheet(sheet) {
  const rows = Object.entries(sheet || {}).filter(([k, v]) => !LIB_HIDDEN.has(k) && !k.endsWith("_id") && v !== "" && v != null && !(Array.isArray(v) && !v.length));
  if (!rows.length) return h("p", { class: "muted small" }, "No sheet entry.");
  return h("dl", { class: "lib-sheet" }, rows.flatMap(([k, v]) => [
    h("dt", {}, libLabel(k)),
    h("dd", {}, LIB_LISTS.has(k) && Array.isArray(v) ? h("div", { class: "chips" }, v.map((x) => h("span", { class: "chip" }, x)))
      : typeof v === "object" ? JSON.stringify(v) : String(v)),
  ]));
}

function libUsedIn(usedIn, tab) {
  if (!usedIn || !usedIn.length) return h("p", { class: "muted small" }, "Not used in any story yet.");
  return h("ul", { class: "plain small" }, usedIn.map((u) =>
    h("li", {}, storyLink(u.package_id, tab, u.title || u.package_id),
      h("span", { class: "muted" }, ` · ${count(u.shots.length, "shot", "shots")}`),
      u.shots.length > 0 && h("span", { class: "muted mono" }, `: ${u.shots.slice(0, 8).join(", ")}${u.shots.length > 8 ? ", …" : ""}`))));
}

// "lk_lila_evening_torn" of "ch_lila" -> "Evening torn"; "prop_business_card" -> "Business card".
function libVariant(id, ownerId) {
  const key = (ownerId || "").replace(/^(ch|loc|prop)_/, "");
  const words = id.replace(/^(lk|ls|ps|prop)_/, "").replace(key ? new RegExp(`^${key}_`) : /^$/, "").replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// A look, a state or an item: its pictures, line, sheet and use.
function libPart(part, kindLabel, ownerId) {
  return h("section", { class: "card lib-part" },
    h("div", { class: "section-head" },
      h("h3", {}, libVariant(part.origin_entity_id, kindLabel === "item" ? "" : ownerId), " ", h("span", { class: "muted small mono" }, part.origin_entity_id)),
      h("span", { class: "chip" }, kindLabel)),
    libEdit.open.has(part.asset_id) ? libEditor(part) : [
      part.description && h("p", {}, part.description),
      h("details", {}, h("summary", { class: "small" }, "Sheet"), libSheet(part.sheet)),
      libEditButton(part)],
    libEdit.notes[part.asset_id] && h("p", { class: "small muted" }, libEdit.notes[part.asset_id]),
    libPictures(part.pictures, part.name),
    libDesign(part),
    part.states && part.states.length > 0 && h("div", { class: "small" }, h("strong", {}, "States: "),
      h("ul", { class: "plain" }, part.states.map((s) => h("li", {}, h("span", { class: "mono" }, s.state_id), ` · ${s.description}`)))),
    h("div", { class: "small" }, h("strong", {}, "Used in"), libUsedIn(part.used_in, "keyframes")));
}

function renderLibAsset(a) {
  const meta = LIB_KINDS[a.kind];
  const origin = jobOf(a.origin_package_id);
  return h("div", { class: "lib-asset" },
    h("section", { class: "card" },
      h("div", { class: "section-head" },
        h("h2", {}, a.name),
        h("div", { class: "chips" },
          h("span", { class: "chip" }, a.kind === "character" ? "character" : "set"),
          origin && h("a", { class: "btn ghost small", href: `#job=${encodeURIComponent(origin.job_id)}&tab=${meta.storyTab}`,
            title: "Open the story this came from" }, "Open its story"))),
      a.description && h("p", { class: "muted" }, a.description),
      libRedrawRow(a),
      libDesignControls(),
      a.kind === "character" && libPictures(a.pictures, a.name),
      a.kind === "character" && libDesign(a),
      libEdit.open.has(a.asset_id) ? libEditor(a) : [libSheet(a.sheet), libEditButton(a)],
      libEdit.notes[a.asset_id] && h("p", { class: "small muted" }, libEdit.notes[a.asset_id]),
      h("div", { class: "small" }, h("strong", {}, "Used in"), libUsedIn(a.used_in, "storyboard"))),
    (a.also || []).length > 0 && h("section", { class: "card" },
      h("h3", {}, "Also drawn in"),
      h("p", { class: "muted small" }, `Other stories linked their ${a.kind === "character" ? "character" : "set"} to this one; these are their own pictures.`),
      a.also.map((x) => h("div", {}, h("strong", { class: "small" }, storyLink(x.package_id, "casting", x.title || x.package_id)),
        libPictures(x.pictures, `${a.name} (${x.title})`)))),
    h("h3", {}, `${meta.childLabel} (${a[meta.child].length})`),
    a[meta.child].length ? a[meta.child].map((x) => libPart(x, a.kind === "character" ? "look" : "state", a.origin_entity_id)) : h("p", { class: "muted" }, "None yet."),
    h("h3", {}, a.kind === "character" ? `Carried items (${a.items.length})` : `Dressing (${a.items.length})`),
    a.items.length ? a.items.map((x) => libPart(x, "item", a.origin_entity_id)) : h("p", { class: "muted small" }, a.kind === "character" ? "Nothing this character carries is registered." : "No registered props dress this set."));
}

document.addEventListener("DOMContentLoaded", () => {
  const input = $("#lib-filter");
  if (input) input.addEventListener("input", (e) => { lib.filter = e.target.value; renderLibSide(); });
  // The story filter narrows the sidebar and, on the list page, the grid.
  const story = $("#lib-story");
  if (story) story.addEventListener("change", (e) => { lib.story = e.target.value; renderLibrary(); });
});

// -- Editing (saved to the story the asset came from) --------------------------------

// Fields a hand edit may change: every sheet field but ids and the name; for a look or a
// state also its one-line story description (the story bible's).
const libEditable = (part) => Object.keys(part.sheet || {}).filter((k) => !LIB_HIDDEN.has(k) && !k.endsWith("_id"));
const libHasLine = (part) => part.kind === "look" || part.kind === "set_state";

function libEditButton(part) {
  if (!libEditable(part).length && !libHasLine(part)) return null;
  return h("button", { type: "button", class: "btn ghost small", onclick: () => {
    libEdit.open.add(part.asset_id);
    delete libEdit.errors[part.asset_id];
    delete libEdit.notes[part.asset_id];
    renderLibrary();
  } }, "Edit");
}

function libEditor(part) {
  const id = part.asset_id;
  const draft = (libEdit.drafts[id] ??= {});
  const field = (key, label, value, list) => {
    const shown = draft[key] ?? (list ? (value || []).join("\n") : value ?? "");
    const box = h("textarea", { rows: list ? Math.max(2, (value || []).length + 1) : Math.min(8, Math.max(2, Math.ceil(String(shown).length / 90))),
      "aria-label": label, oninput: (e) => { draft[key] = e.target.value; } });
    box.value = shown;
    return h("label", { class: "field" }, h("span", {}, label, list && h("span", { class: "muted small" }, " (one per line)")), box);
  };
  return h("div", { class: "lib-editor" },
    libHasLine(part) && field("__line", "Story line", part.description, false),
    libEditable(part).map((k) => field(k, libLabel(k), part.sheet[k], Array.isArray(part.sheet[k]))),
    h("div", { class: "prompt-edit-actions" },
      h("button", { type: "button", class: "btn small primary-outline", disabled: libEdit.saving.has(id),
        onclick: () => libSave(part) }, libEdit.saving.has(id) ? "Saving…" : "Save"),
      h("button", { type: "button", class: "btn ghost small", onclick: () => {
        libEdit.open.delete(id); delete libEdit.drafts[id]; delete libEdit.errors[id]; renderLibrary();
      } }, "Cancel"),
      h("span", { class: "muted small" }, "Saved to the story it came from; the library follows.")),
    libEdit.errors[id] && h("p", { class: "small warn-text", role: "alert" }, `Not saved: ${libEdit.errors[id]}`));
}

async function libSave(part) {
  const id = part.asset_id;
  const draft = libEdit.drafts[id] || {};
  const sheet = {};
  let line = null;
  for (const [k, v] of Object.entries(draft)) {
    if (k === "__line") { if (v.trim() !== (part.description || "").trim()) line = v; continue; }
    const before = part.sheet[k];
    const value = Array.isArray(before) ? v.split("\n").map((x) => x.trim()).filter(Boolean) : v;
    if (JSON.stringify(value) !== JSON.stringify(Array.isArray(before) ? before : (before ?? ""))) sheet[k] = value;
  }
  if (!Object.keys(sheet).length && line == null) {
    libEdit.open.delete(id); delete libEdit.drafts[id]; libEdit.notes[id] = "No changes."; renderLibrary(); return;
  }
  libEdit.saving.add(id);
  delete libEdit.errors[id];
  renderLibrary();
  try {
    const r = await api(`/v1/drama-packages/${encodeURIComponent(part.origin_package_id)}/entries/${encodeURIComponent(part.origin_entity_id)}`, {
      method: "PATCH",
      body: JSON.stringify({ revision_id: part.origin_revision_id || lib.view.origin_revision_id, sheet, bible: line == null ? {} : { description: line } }),
    });
    libEdit.open.delete(id);
    delete libEdit.drafts[id];
    libEdit.notes[id] = r.drawn_before.length
      ? `Saved. ${r.drawn_before.length} picture${r.drawn_before.length > 1 ? "s were" : " was"} drawn from the earlier text: redraw them with "Redraw out-of-date".`
      : "Saved.";
    await libReload();
  } catch (e) {
    libEdit.errors[id] = e.message;
  } finally {
    libEdit.saving.delete(id);
    renderLibrary();
  }
}

async function libReload() {
  const meta = LIB_KINDS[lib.kind];
  const id = lib.id;
  try {
    const view = await api(`/v1/library/${meta.path}/${encodeURIComponent(id)}`);
    if (lib.id === id) {
      lib.view = view;
      libLoadDesign();
    }
  } catch (e) {
    lib.error = e.message;
  }
}

// -- Redrawing out-of-date pictures ---------------------------------------------------

// Every out-of-date picture on the page (the asset's, its looks' or states', its items').
function libOutdated(a) {
  const meta = LIB_KINDS[a.kind];
  return [a, ...(a[meta.child] || []), ...(a.items || [])].flatMap((x) => x.pictures.filter((p) => p.outdated.length));
}

function libRedrawRow(a) {
  const stale = libOutdated(a);
  if (!stale.length && !lib.redrawing) return null;
  return h("div", { class: "banner warn compact" },
    stale.length ? `${count(stale.length, "picture is", "pictures are")} out of date: drawn before a change to its design or to a picture it is drawn from. ` : "",
    lib.redrawing && h("span", {}, lib.redrawing, " "),
    stale.length > 0 && h("button", { type: "button", class: "btn small", disabled: Boolean(lib.redrawing), onclick: () => libRedraw(a) },
      "Redraw out-of-date"));
}

async function libRedraw(a) {
  const stale = libOutdated(a);
  const byStory = {};
  for (const p of stale) (byStory[`${p.package_id}|${p.revision_id}`] ??= new Set()).add(p.requirement_id);
  lib.redrawing = "Redrawing…";
  renderLibrary();
  try {
    for (const [key, reqs] of Object.entries(byStory)) {
      const [pid, rid] = key.split("|");
      const q = new URLSearchParams({ revision_id: rid });
      for (const r of reqs) q.append("requirement_ids", r);
      const out = await api(`/v1/drama-packages/${encodeURIComponent(pid)}/images/refresh?${q}`, { method: "POST" });
      lib.redrawing = (out.notes || []).join(" ") || "Redrawing…";
    }
  } catch (e) {
    lib.redrawing = "";
    lib.error = e.message;
    return renderLibrary();
  }
  renderLibrary();
  libPoll(a.asset_id, 0);
}

// While redraws run, reload the page every few seconds (until nothing is out of date, or a
// few minutes have passed).
function libPoll(id, n) {
  clearTimeout(lib.timer);
  lib.timer = setTimeout(async () => {
    if (lib.id !== id) return;
    if (typingIn($("#main"))) return libPoll(id, n);  // never redraw under someone typing
    await libReload();
    const left = lib.view ? libOutdated(lib.view).length : 0;
    lib.redrawing = left && n < 60 ? `Redrawing… ${count(left, "picture", "pictures")} still to go.` : "";
    renderLibrary();
    if (left && n < 60) libPoll(id, n + 1);
  }, 4000);
}

// -- Takes and draws (the design board, per asset) -------------------------------------

// Each story revision whose design pictures are on this page (the asset's own, and those of
// looks or states from linked stories), as "<package>|<revision>".
function libStoryRefs() {
  const v = lib.view;
  if (!v) return [];
  const meta = LIB_KINDS[v.kind];
  const parts = [v, ...(v[meta.child] || []), ...(v.items || [])];
  const refs = {};
  for (const p of parts) {
    if (p.origin_revision_id) refs[`${p.origin_package_id}|${p.origin_revision_id}`] = { package_id: p.origin_package_id, revision_id: p.origin_revision_id };
  }
  return Object.values(refs);
}
const libDesignKey = () => null;  // the page keeps its own design data (app.js designOpen)

async function libFetchDesigns() {
  const refs = libStoryRefs();
  const got = await Promise.all(refs.map((r) =>
    api(`/v1/drama-packages/${encodeURIComponent(r.package_id)}/images?${new URLSearchParams({ revision_id: r.revision_id })}`).catch(() => null)));
  const designs = {};
  refs.forEach((r, i) => { if (got[i]) designs[imagesKey(r)] = got[i]; });
  lib.designs = designs;
  // The chats tied to the page's pictures (docs/workbench.md), for each slot's list.
  const chats = await Promise.all(refs.map((r) =>
    api(`/v1/experiments?${new URLSearchParams({ package_id: r.package_id })}`).catch(() => [])));
  lib.chats = chats.flat();
  // While anything on the page is drawing, look again in a moment.
  clearTimeout(lib.designTimer);
  if (Object.values(designs).some((v) => v.queued > 0)) {
    const id = lib.id;
    lib.designTimer = setTimeout(() => { if (lib.id === id) libAfterDesign(); }, 2500);
  }
}

function libLoadDesign() {
  libFetchDesigns().then(() => { if (!typingIn($("#main"))) renderLibrary(); });
}

// The design slots (deliverables) that show this part: a character's face, a look, a set
// state's views, an item's states.
function libSlots(part, v) {
  if (!v) return [];
  const id = part.origin_entity_id;
  return v.slots.filter((s) =>
    (part.kind === "character" && s.asset_type === "character_identity" && s.entity_id === id)
    || (part.kind === "look" && s.asset_type === "character_look" && s.variant_id === id)
    || (part.kind === "set_state" && s.asset_type === "location" && s.variant_id === id)
    || (part.kind === "item" && s.asset_type === "prop" && s.entity_id === id));
}

function libDesign(part) {
  const ref = part.origin_revision_id ? { package_id: part.origin_package_id, revision_id: part.origin_revision_id } : null;
  const v = ref && lib.designs[imagesKey(ref)];
  if (!v) return h("p", { class: "muted small" }, "Loading takes…");
  const slots = libSlots(part, v);
  if (!slots.length) return null;
  const job = jobOf(part.origin_package_id);
  const picked = new Set(v.images.filter((i) => i.selected && i.status === "done").map(keyOf));
  const foreign = lib.view && part.origin_package_id !== lib.view.origin_package_id;
  return slots.map((slot) => {
    const parent = slot.parent_key && `${slot.parent_key[0]}|${slot.parent_key[1]}`;
    const from = parent && v.slots.find((x) => keyOf(x) === parent);
    const blocked = parent && !picked.has(parent);
    const takes = v.options.filter((o) => keyOf(o) === keyOf(slot)).length;
    return [libWorkbench(part, slot, slots.length > 1), stickyDetails(`lib:${keyOf(slot)}`, `Takes and draws · ${slot.view} · ${count(takes, "take", "takes")}${foreign ? " · in its own story" : ""}`,
      renderTakeRows(v, slot),
      blocked
        ? h("p", { class: "muted small" }, `Drawn from ${from ? `${from.entity}${from.variant ? ` (${from.variant})` : ""}, ${from.view}` : "another picture"}: pick an image for it first.`)
        : renderNewTake(ref, slot, { jobId: job && job.job_id }))];
  }).flat();
}

// The workbench (docs/workbench.md): develop a picture in a chat that starts from its pick;
// a result's "Use as pick" makes it this picture's pick. The slot's chats are listed.
function libWorkbench(part, slot, sayView) {
  const mine = (lib.chats || []).filter((s) => s.target && s.target.package_id === part.origin_package_id
    && s.target.requirement_id === slot.requirement_id && s.target.deliverable_id === slot.deliverable_id);
  const label = `${slot.entity}${slot.variant ? ` (${slot.variant})` : ""}, ${slot.view}`;
  const develop = async (button) => {
    button.disabled = true;
    try {
      const s = await api("/v1/experiments", { method: "POST", body: JSON.stringify({
        target: { package_id: part.origin_package_id, requirement_id: slot.requirement_id, deliverable_id: slot.deliverable_id, label } }) });
      goExp(s.session_id);
    } catch (e) {
      button.disabled = false;
      lib.error = e.message;
      renderLibrary();
    }
  };
  return h("div", { class: "design-row small" },
    h("button", { type: "button", class: "btn small", title: "Change this picture in a chat with the image model, starting from its pick", onclick: (e) => develop(e.currentTarget) }, sayView ? `Develop the ${slot.view} in a chat` : "Develop in a chat"),
    mine.length > 0 && h("span", { class: "muted" }, "Chats:"),
    mine.slice(0, 6).map((s) => h("a", { href: `#${s.kind === "face" ? "face" : "exp"}=${encodeURIComponent(s.session_id)}`, title: `${s.kind === "face" ? "Face Lab study" : "Chat"}, updated ${timeAgo(s.updated_at)}` }, s.title)),
    mine.length > 6 && h("span", { class: "muted" }, `and ${mine.length - 6} more`));
}

// Draws per new take, and draft or full size (shared with the Casting tab's controls).
function libDesignControls() {
  const queued = Object.values(lib.designs).reduce((n, v) => n + v.queued, 0);
  return h("div", { class: "design-row small" },
    h("span", { class: "muted" }, "New takes:"),
    h("span", { class: "muted" }, "draws"), numberSelect(design.draws, 6, (x) => { design.draws = x; renderLibrary(); }, "Draws per take"),
    h("select", { "aria-label": "Size", onchange: (e) => { design.draft = e.target.value === "draft"; renderLibrary(); } },
      h("option", { value: "draft", selected: design.draft }, "Draft (fast)"), h("option", { value: "full", selected: !design.draft }, "Full size")),
    queued > 0 && h("span", { class: "chip info" }, `${queued} drawing…`),
    state.images.error && h("span", { class: "warn-text" }, state.images.error),
    (state.images.notes || []).length > 0 && h("span", { class: "muted" }, state.images.notes.join(" ")));
}
