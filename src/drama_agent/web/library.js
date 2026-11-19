"use strict";

/*
 * Characters and Sets: the asset library (docs/entity-reorganization.md, phase 2), in three
 * layers (docs/library-layers.md): a character or set with its sheet and its looks or states
 * and items as cards (and "+ New look"); one look, state or item (or the face) with its line,
 * sheet, pictures and the shots that use it; one picture with its takes, the pictures
 * brought in (keyframes, chats), a new take and the workbench.
 * Each part is edited in place: the edit is saved to the story it came from (PATCH
 * …/entries/{id}, the same as the Cast and Sets tabs) and the library follows. "Redraw
 * out-of-date" redraws this asset's out-of-date pictures (and what is drawn from them).
 */

const lib = { kind: null, id: null, list: null, view: null, error: "", filter: "", story: "", timer: null, redrawing: "", scratch: [],
  designs: {}, designTimer: null,  // design board data per story revision on the page
  part: null, slot: null,  // the layer shown: a look, state or item (or the face), one of its pictures
  shot: null,  // ... or one of the shots it is in, with their keyframes
  adding: false, newDraft: {}, newError: "", stale: false,  // "+ New look"
  addingItem: false, itemDraft: {}, itemError: "", itemPhoto: null, pendingPhoto: null };  // "+ New item"
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

// "#lib=characters", "#lib=sets", or "#lib=<asset id>" (with "&part=" and "&slot=" for its
// layers).
async function libRoute(param) {
  const kind = param === "sets" ? "set" : param === "characters" || !param ? "character" : kindOfId(param);
  if (!kind) {
    $("#main").replaceChildren(h("section", { class: "card" }, h("p", {}, `Nothing in the library is called ${param}.`)));
    return;
  }
  const meta = LIB_KINDS[kind];
  setMode(meta.mode);
  const id = kindOfId(param || "") ? param : null;
  const hash = new URLSearchParams(location.hash.slice(1));
  const [part, slot] = id ? [hash.get("part"), hash.get("part") && hash.get("slot")] : [null, null];
  const shot = part && !slot ? hash.get("shot") : null;  // a shot's keyframes, from a look or state
  // Another layer of the asset on the page: shown from what is loaded, then refreshed.
  if (id && lib.id === id && lib.view && !lib.stale) {
    Object.assign(lib, { part, slot, shot, adding: false, addingItem: false });
    renderLibrary();
    window.scrollTo(0, 0);
    libReload();
    return;
  }
  if (lib.kind !== kind) Object.assign(lib, { kind, list: null, filter: "" });
  if (lib.id !== id) {
    Object.assign(libEdit, { open: new Set(), drafts: {}, errors: {}, notes: {} });
    lib.designs = {};
  }
  Object.assign(lib, { part, slot, shot, adding: false, addingItem: false, stale: false });
  clearTimeout(lib.timer);
  clearTimeout(lib.designTimer);
  lib.id = id;
  lib.view = null;
  lib.error = "";
  lib.redrawing = "";
  renderLibrary();
  // Scratch feeds the sidebar and each picture's "Link a scratch chat": in before the page.
  const scratch = libLoadScratch();
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
  await scratch;
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
      a.cover ? h("img", { src: a.cover.thumb, alt: "", loading: "lazy", class: a.cover.likeness ? "lookalike-thumb" : null, title: a.cover.likeness ? LOOKALIKE_TIP : null }) : h("span", { class: "lib-noimg" }),
      h("span", {}, a.name, h("span", { class: "muted small" }, ` · ${count(a.children, meta.one, meta.many)}`))))));
  if (!shown.length) list.replaceChildren(h("li", { class: "muted small" }, f || lib.story ? "No match." : "Nothing yet."));
}

// Scratch (docs/workbench.md): image chats and face studies tied to no picture, newest
// first. They open in the workbench panel.
async function libLoadScratch() {
  try {
    lib.scratch = (await api("/v1/experiments")).filter((s) => !s.target);
  } catch {
    lib.scratch = [];
  }
  renderLibScratch();
}

function renderLibScratch() {
  const list = $("#lib-scratch");
  if (!list) return;
  const items = lib.scratch || [];
  if (!items.length) return list.replaceChildren(h("li", { class: "muted small" }, "Nothing here."));
  list.replaceChildren(...items.map((s) => h("li", {},
    h("a", {
      href: `#${s.kind === "face" ? "face" : "exp"}=${encodeURIComponent(s.session_id)}`,
      class: `lib-side-item${bench.sid === s.session_id ? " active" : ""}`,
      onclick: (e) => { if (e.button || e.metaKey || e.ctrlKey) return; e.preventDefault(); openBench(s.kind === "face" ? "face" : "chat", s.session_id, s.title); },
    }, h("span", {}, s.title, h("span", { class: "muted small" }, ` · ${s.kind === "face" ? "study" : "chat"} · ${timeAgo(s.updated_at)}`))))));
}

// Link a scratch chat or study to this picture; it leaves Scratch.
function libLinkScratch(target, isFace) {
  const free = (lib.scratch || []).filter((s) => s.kind === "chat" || isFace);
  if (!free.length) return null;
  return h("select", {
    "aria-label": "Link a scratch chat to this picture",
    onchange: async (e) => {
      const sid = e.target.value;
      if (!sid) return;
      e.target.disabled = true;
      try {
        await api(`/v1/experiments/${encodeURIComponent(sid)}`, { method: "PATCH", body: JSON.stringify({ target }) });
        await libLoadScratch();
        libLoadDesign();
      } catch (err) {
        lib.error = err.message;
        renderLibrary();
      }
    },
  }, h("option", { value: "" }, "Link a scratch chat…"),
    free.map((s) => h("option", { value: s.session_id }, `${s.title} (${s.kind === "face" ? "study" : "chat"})`)));
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
        lookalikeTag(a.cover?.likeness),
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
  const items = pictures.map((p) => ({ src: p.content, caption: `${title} · ${p.view}`, likeness: p.likeness, title: p.imported_from ? `imported from ${p.imported_from}` : `${p.size}` }));
  return h("div", { class: "lib-pics" }, pictures.map((p, i) =>
    h("figure", { class: "lib-pic" },
      h("button", { type: "button", class: "ref-open", "aria-label": `Enlarge ${title}, ${p.view}`, onclick: () => enlarge(items, i) },
        h("img", { src: p.thumb, alt: `${title}, ${p.view}`, loading: "lazy" }), lookalikeTag(p.likeness), idTag(`design:${p.image_id}`)),
      h("figcaption", { class: "small" }, p.view,
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

// -- Layers: the character (or set), one look (state, item or the face), one picture --------
// "#lib=<asset>" is the character or set; "&part=<asset>" one of its looks, states or items
// (the character itself for its face); "&slot=<deliverable>" one picture of it, with its
// takes. Each layer links to the next; a breadcrumb leads back.

const libHref = (id, part, slot, shot) => `#lib=${encodeURIComponent(id)}${part ? `&part=${encodeURIComponent(part)}` : ""}${slot ? `&slot=${encodeURIComponent(slot)}` : ""}${shot ? `&shot=${encodeURIComponent(shot)}` : ""}`;

// Everything on the page that has pictures: the character itself (its face), its looks or
// states, its items.
function libParts(a) {
  const meta = LIB_KINDS[a.kind];
  return [...(a.kind === "character" ? [a] : []), ...(a[meta.child] || []), ...(a.items || [])];
}

// "Face", "Look: Evening torn", "State: Night", "Item: Business card".
function libPartTitle(a, part) {
  if (part.asset_id === a.asset_id) return "Face";
  const kind = { look: "Look", set_state: "State", item: "Item" }[part.kind] || "Part";
  return `${kind}: ${libVariant(part.origin_entity_id, part.kind === "item" ? "" : a.origin_entity_id)}`;
}

// A picture by its view, and for an item (one picture per state) its state too:
// "Ryder · detail".
const libSlotName = (slot) => (slot.asset_type === "prop" && slot.variant_id
  ? `${libVariant(slot.variant_id, slot.entity_id)} · ${slot.view}` : slot.view);

function libCrumbs(a, part, slot, shotId) {
  const meta = LIB_KINDS[a.kind];
  const trail = [[`#lib=${meta.path}`, meta.plural], [libHref(a.asset_id), a.name]];
  if (part) trail.push([libHref(a.asset_id, part.asset_id), libPartTitle(a, part)]);
  if (slot) trail.push([null, libSlotName(slot)]);
  if (shotId) trail.push([null, `Shot ${shotId} · keyframes`]);
  return h("nav", { class: "crumbs small", "aria-label": "Where you are" }, trail.map(([href, label], i) => [
    i > 0 && h("span", { class: "muted", "aria-hidden": "true" }, " › "),
    href && i < trail.length - 1 ? h("a", { href }, label) : h("strong", { "aria-current": "page" }, label)]));
}

// The design data of a part: its story revision's board and the slots that show it.
function libSlotData(part) {
  const ref = part.origin_revision_id ? { package_id: part.origin_package_id, revision_id: part.origin_revision_id } : null;
  const v = ref && lib.designs[imagesKey(ref)];
  return { ref, v, slots: libSlots(part, v) };
}

// What came in from elsewhere (a keyframe, a chat) or was drawn before takes: a slot's
// finished pictures that belong to no take.
const libLoose = (v, slot) => v.images.filter((i) => keyOf(i) === keyOf(slot) && !i.option_id && i.status === "done" && !i.finalizes);
const libTakeCount = (v, slot) => v.options.filter((o) => keyOf(o) === keyOf(slot)).length + (libLoose(v, slot).length ? 1 : 0);

// A card that opens a part (a look, a state, an item, the face): its pick, name and line.
function libPartCard(a, part) {
  const { v, slots } = libSlotData(part);
  const pic = part.pictures[0];
  const takes = v ? slots.reduce((n, s) => n + libTakeCount(v, s), 0) : null;
  const shots = (part.used_in || []).reduce((n, u) => n + u.shots.length, 0);
  const stale = part.pictures.some((p) => p.outdated.length);
  return h("a", { href: libHref(a.asset_id, part.asset_id), class: "card lib-card", title: `Open ${libPartTitle(a, part)}: its pictures and takes` },
    pic ? h("img", { src: pic.thumb, alt: libPartTitle(a, part), loading: "lazy" }) : h("div", { class: "lib-noimg big" }, "no picture yet"),
    pic && lookalikeTag(pic.likeness),
    h("strong", {}, libPartTitle(a, part)),
    part.asset_id !== a.asset_id && part.description && h("span", { class: "small lib-line" }, part.description),
    h("span", { class: "muted small" }, [
      takes != null && count(takes, "take", "takes"),
      part.asset_id !== a.asset_id && (shots ? count(shots, "shot", "shots") : "in no shot yet"),
    ].filter(Boolean).join(" · ")),
    stale && h("span", { class: "chip warn" }, "out of date"));
}

// Level 1: the character (or set): its sheet, its face, its looks or states, its items.
function renderLibTop(a) {
  const meta = LIB_KINDS[a.kind];
  const origin = jobOf(a.origin_package_id);
  const children = a[meta.child];
  return h("div", { class: "lib-asset" },
    libCrumbs(a),
    h("section", { class: "card" },
      h("div", { class: "section-head" },
        h("h2", {}, a.name),
        h("div", { class: "chips" },
          h("span", { class: "chip" }, a.kind === "character" ? "character" : "set"),
          origin && h("a", { class: "btn ghost small", href: `#job=${encodeURIComponent(origin.job_id)}&tab=${meta.storyTab}`,
            title: "Open the story this came from" }, "Open its story"))),
      a.description && h("p", { class: "muted" }, a.description),
      libRedrawRow(a),
      libEdit.open.has(a.asset_id) ? libEditor(a) : [stickyDetails(`libsheet:${a.asset_id}`, "Sheet", libSheet(a.sheet)), libEditButton(a)],
      libEdit.notes[a.asset_id] && h("p", { class: "small muted" }, libEdit.notes[a.asset_id]),
      h("div", { class: "small" }, h("strong", {}, "Used in"), libUsedIn(a.used_in, "storyboard"))),
    h("div", { class: "section-head" }, h("h3", {}, `${meta.childLabel} (${children.length})`),
      !lib.adding && h("button", { type: "button", class: "btn small", onclick: () => { lib.adding = true; renderLibrary(); } }, `+ New ${meta.one}`)),
    lib.adding && libNewVariant(a),
    h("div", { class: "lib-grid" },
      a.kind === "character" && libPartCard(a, a),
      children.map((x) => libPartCard(a, x))),
    !children.length && h("p", { class: "muted small" }, `No ${meta.many} yet.`),
    h("div", { class: "section-head" }, h("h3", {}, a.kind === "character" ? `Carried items (${a.items.length})` : `Dressing (${a.items.length})`),
      a.kind === "character" && !lib.addingItem && h("button", { type: "button", class: "btn small", onclick: () => { lib.addingItem = true; renderLibrary(); } }, "+ New item")),
    lib.addingItem && a.kind === "character" && libNewItem(a),
    a.items.length ? h("div", { class: "lib-grid" }, a.items.map((x) => libPartCard(a, x)))
      : h("p", { class: "muted small" }, a.kind === "character" ? "Nothing this character carries is registered." : "No registered props dress this set."),
    (a.also || []).length > 0 && h("section", { class: "card" },
      h("h3", {}, "Also drawn in"),
      h("p", { class: "muted small" }, `Other stories linked their ${a.kind === "character" ? "character" : "set"} to this one; these are their own pictures.`),
      a.also.map((x) => h("div", {}, h("strong", { class: "small" }, storyLink(x.package_id, "casting", x.title || x.package_id)),
        libPictures(x.pictures, `${a.name} (${x.title})`)))));
}

// Level 2: one look, state, item or the face: its line and sheet, its pictures (each opens
// its takes), where it is used.
function renderLibPart(a, part) {
  const self = part.asset_id === a.asset_id;
  const { v, slots } = libSlotData(part);
  return h("div", { class: "lib-asset" },
    libCrumbs(a, part),
    h("section", { class: "card" },
      h("div", { class: "section-head" }, h("h2", {}, libPartTitle(a, part)),
        h("span", { class: "chip" }, self ? "face" : { look: "look", set_state: "state", item: "item" }[part.kind])),
      !self && (libEdit.open.has(part.asset_id) ? libEditor(part) : [
        part.description && h("p", {}, part.description),
        h("details", {}, h("summary", { class: "small" }, "Sheet"), libSheet(part.sheet)),
        libEditButton(part)]),
      self && h("p", { class: "muted small" }, "The face every look is drawn from. Its sheet is on the character's page."),
      libEdit.notes[part.asset_id] && h("p", { class: "small muted" }, libEdit.notes[part.asset_id]),
      part.kind === "item" && libPropLink(part),
      part.states && part.states.length > 0 && h("div", { class: "small" }, h("strong", {}, "States: "),
        h("ul", { class: "plain" }, part.states.map((s) => h("li", {}, h("span", { class: "mono" }, s.state_id), ` · ${s.description}`)))),
      !self && h("div", { class: "small" }, h("strong", {}, "Used in"), libUsedIn(part.used_in, "keyframes"))),
    h("h3", {}, "Pictures"),
    !v ? h("p", { class: "muted small" }, "Loading takes…")
      : !slots.length ? h("p", { class: "muted small" }, "Nothing to draw for it in its story.")
      : h("div", { class: "lib-grid" }, slots.map((slot) => {
          const pick = part.pictures.find((p) => p.deliverable_id === slot.deliverable_id);
          const n = libTakeCount(v, slot);
          return h("a", { href: libHref(a.asset_id, part.asset_id, slot.deliverable_id), class: "card lib-card", title: `Open the ${libSlotName(slot)}: its takes and draws` },
            pick ? h("img", { src: pick.thumb, alt: libSlotName(slot), loading: "lazy" }) : h("div", { class: "lib-noimg big" }, "no picture yet"),
            pick && lookalikeTag(pick.likeness),
            h("strong", {}, libSlotName(slot)),
            h("span", { class: "muted small" }, `${count(n, "take", "takes")}${pick?.picked ? " · picked" : ""}`),
            pick && pick.outdated.length > 0 && h("span", { class: "chip warn", title: pick.outdated.join("; ") }, "out of date"));
        })),
    !self && libKeyframes(a, part));
}

// -- Keyframes of the shots a look (or state, or item) is in ----------------------------

// The shots it is in, in its own story (a linked story's shots are on that story's page).
function libShotIds(part) {
  const own = (part.used_in || []).find((u) => u.package_id === part.origin_package_id);
  return own ? own.shots : [];
}

// The story's keyframe board (shots.js keeps one story's at a time); null while loading.
function libFrames(part) {
  if (!part.origin_revision_id) return null;
  const pkg = { package_id: part.origin_package_id, revision_id: part.origin_revision_id };
  if (frames.key !== framesKey(pkg)) loadFrames(pkg);
  else if (frames.view && frames.view.busy && !frames.timer && !frames.loading) loadFrames(pkg);  // resume polling
  return frames.key === framesKey(pkg) ? frames.view : null;
}

// One row per shot: each cut's approved keyframe (or its newest draw, dashed), how many
// are approved, and a link to the shot's keyframes, where they are drawn and developed.
function libKeyframes(a, part) {
  const ids = libShotIds(part);
  if (!ids.length) return [h("h3", {}, "Keyframes"), h("p", { class: "muted small" }, "In no shot yet.")];
  const v = libFrames(part);
  const shots = v ? ids.map((id) => v.shots.find((s) => s.shot_id === id)).filter(Boolean) : [];
  const approved = shots.reduce((n, s) => n + s.sub_shots.filter((x) => x.approved).length, 0);
  const total = shots.reduce((n, s) => n + s.sub_shots.length, 0);
  return [
    h("div", { class: "section-head" }, h("h3", {}, `Keyframes (${count(ids.length, "shot", "shots")})`),
      v && h("span", { class: `chip ${approved === total ? "ok" : "warn"}` }, `${approved}/${total} approved`)),
    !v ? h("p", { class: "muted small" }, frames.error || "Loading keyframes…")
      : h("div", { class: "kf-rows" }, shots.map((s) => h("a", { href: libHref(a.asset_id, part.asset_id, null, s.shot_id), class: "card kf-row", title: `Open ${s.shot_id}: review and develop its keyframes` },
          h("div", { class: "kf-row-head" }, h("strong", {}, s.shot_id), h("span", { class: "muted small" }, ` · ${s.place}`),
            s.sub_shots.some((x) => (x.outdated || []).length) && h("span", { class: "chip warn" }, "out of date"),
            s.sub_shots.some((x) => x.takes.some((t) => !["done", "failed", "paused"].includes(t.status))) && h("span", { class: "chip info" }, "drawing…")),
          h("p", { class: "small muted kf-row-action" }, s.action),
          h("div", { class: "kf-thumbs" }, s.sub_shots.map((x) => libFrameThumb(x, s.sub_shots.length)))))),
  ];
}

function libFrameThumb(sub, total) {
  const [aid, an] = sub.approved || [];
  const ap = aid && sub.takes.find((t) => t.take_id === aid);
  const done = sub.takes.flatMap((t) => t.results.filter((d) => d.status === "done").map((d) => [t, d]));
  const newest = done.reduce((m, x) => (!m || x[0].created_at > m[0].created_at ? x : m), null);
  const [take, n] = ap ? [ap, an] : newest ? [newest[0], newest[1].n] : [];
  const label = total > 1 ? `Cut ${sub.index}` : "Keyframe";
  return h("figure", { class: `kf-thumb${ap ? " approved" : ""}` },
    take ? h("img", { src: drawUrl(take, n), alt: label, loading: "lazy" }) : h("div", { class: "lib-noimg" }, "none yet"),
    h("figcaption", { class: "small" }, label, " · ", ap ? "✓ approved" : take ? "not approved" : sub.missing.length ? "waiting" : "not drawn",
      ` · ${count(sub.takes.length, "round", "rounds")}`));
}

// A shot's keyframes, from a look's (or state's) page: the Keyframes tab's shot card (plan,
// scene reading, each cut with its approved keyframe, chats, rounds and a new take).
function renderLibShot(a, part, shotId) {
  const v = libFrames(part);
  const shot = v && v.shots.find((s) => s.shot_id === shotId);
  const pkg = frames.pkg;
  const job = jobOf(part.origin_package_id);
  return h("div", { class: "lib-asset" },
    libCrumbs(a, part, null, shotId),
    h("section", { class: "card" },
      h("div", { class: "section-head" }, h("h2", {}, `Shot ${shotId}`),
        job && h("a", { class: "btn ghost small", href: `#job=${encodeURIComponent(job.job_id)}&tab=keyframes`, title: "Every shot's keyframes, in the story" }, "All keyframes of the story")),
      h("p", { class: "muted small" }, "Review each cut's keyframe, approve a draw, draw another round, or develop a draw in a chat: a chat result's Use as keyframe brings it back here, approved."),
      v && libFrameControls(v),
      frames.error && h("p", { class: "small warn-text" }, frames.error)),
    !v ? h("p", { class: "muted" }, "Loading keyframes…")
      : !shot ? h("p", {}, `No shot ${shotId} in this story any more.`)
      : renderShotCard(pkg, shot));
}

// Draws and rounds for new keyframe takes, and the image provider (as on the Keyframes tab).
function libFrameControls(v) {
  return h("div", { class: "design-row small" },
    h("label", {}, "Draws ", numberSelect(frames.draws, 4, (n) => { frames.draws = n; }, "Draws per round")),
    h("label", {}, " Rounds ", numberSelect(frames.rounds, 3, (n) => { frames.rounds = n; }, "Automatic rounds")),
    Object.keys(v.providers || {}).length > 1 && h("label", {}, " Drawn with ",
      h("select", { "aria-label": "Image provider", onchange: (e) => { frames.provider = e.target.value; } },
        Object.entries(v.providers).map(([name, model]) =>
          h("option", { value: name, title: model, selected: (frames.provider || v.default_provider) === name }, providerName(name))))));
}

// Level 3: one picture: its takes and draws, pictures brought in, a new take, the workbench.
function renderLibSlot(a, part, slot) {
  const { ref, v } = libSlotData(part);
  // A new item's reference photo: its chat starts once the picture's page is up.
  if (lib.pendingPhoto && lib.pendingPhoto.target.deliverable_id === slot.deliverable_id) {
    const { file, target } = lib.pendingPhoto;
    lib.pendingPhoto = null;
    setTimeout(() => { const b = [...document.querySelectorAll(".design-row button")].find((x) => x.textContent === "From a reference photo"); if (b) benchChatFromPhoto(file, target, b); });
  }
  const job = jobOf(part.origin_package_id);
  const picked = new Set(v.images.filter((i) => i.selected && i.status === "done").map(keyOf));
  const parent = slot.parent_key && `${slot.parent_key[0]}|${slot.parent_key[1]}`;
  const from = parent && v.slots.find((x) => keyOf(x) === parent);
  const foreign = part.origin_package_id !== a.origin_package_id;
  const loose = libLoose(v, slot);
  return h("div", { class: "lib-asset" },
    libCrumbs(a, part, slot),
    h("section", { class: "card" },
      h("div", { class: "section-head" }, h("h2", {}, `${libPartTitle(a, part)} · ${libSlotName(slot)}`),
        foreign && h("span", { class: "chip", title: "This look comes from a story linked to this character" }, "in its own story")),
      libPickHero(v, slot, `${libPartTitle(a, part)} · ${libSlotName(slot)}`),
      libDesignControls(),
      libWorkbench(part, slot, false)),
    h("h3", {}, "Takes"),
    renderTakeRows(v, slot),
    loose.length > 0 && h("div", { class: "take" },
      h("div", { class: "take-head" }, h("strong", {}, "Brought in"),
        h("span", { class: "muted small" }, "From keyframes and chats, or drawn before takes. Click one to pick it.")),
      h("div", { class: "draws" }, loose.map(libLooseDraw))),
    h("h3", {}, "New take"),
    parent && !picked.has(parent)
      ? h("p", { class: "muted small" }, `Drawn from ${from ? `${from.entity}${from.variant ? ` (${from.variant})` : ""}, ${from.view}` : "another picture"}: pick an image for it first.`)
      : renderNewTake(ref, slot, { jobId: job && job.job_id }));
}

// The picture's current pick, large, at the top of its page: which take and draw it is (or
// where it was brought in from), and whether it is out of date. With no pick, the newest
// finished draw stands in (as everywhere else) and says so.
function libPickHero(v, slot, title) {
  const done = v.images.filter((i) => keyOf(i) === keyOf(slot) && i.status === "done");
  const chosen = done.filter((i) => i.selected);
  const newest = (xs) => xs.reduce((m, i) => (!m || i.created_at > m.created_at ? i : m), null);
  const img = newest(chosen) || newest(done);
  if (!img) return h("div", { class: "pick-hero empty" }, h("span", { class: "muted" }, "No picture yet: start with a new take below."));
  const picked = chosen.length > 0;
  // A full-size render stands for the draft it finishes.
  const base = img.finalizes ? v.images.find((i) => i.image_id === img.finalizes) || img : img;
  const take = base.option_id && v.options.find((o) => o.option_id === base.option_id);
  const source = take
    ? `Take ${take.index}${take.label && take.label !== `Take ${take.index}` ? `: ${take.label}` : ""}, draw ${base.draw}${img.finalizes ? " (full size)" : ""}`
    : libSource(img);
  const stale = (v.outdated || {})[img.image_id] || [];
  return h("div", { class: `pick-hero${picked ? "" : " unpicked"}` },
    h("button", { type: "button", class: "ref-open", "aria-label": `Enlarge the pick of ${title}`,
      onclick: () => enlarge([{ src: thumbUrl(img, "content"), caption: `${title} · ${picked ? "pick" : "newest draw"}`, likeness: img.likeness, title: img.size }]) },
      h("img", { src: thumbUrl(img), alt: `${title}, current pick` }), lookalikeTag(img.likeness), idTag(`design:${img.image_id}`)),
    h("div", { class: "pick-hero-text" },
      h("span", { class: `chip ${picked ? "ok" : "warn"}` }, picked ? "✓ Current pick" : "No pick yet"),
      h("strong", {}, source),
      !picked && h("p", { class: "small muted" }, "Showing the newest draw; looks, states and keyframes are drawn from it until you pick one below."),
      picked && h("p", { class: "small muted" }, "What is drawn from this picture uses it. Click another draw below to change the pick."),
      stale.length > 0 && h("p", { class: "small warn-text" }, `Out of date: ${stale.join("; ")}`),
      h("span", { class: "small muted" }, [img.size, img.model, timeAgo(img.finished_at || img.created_at)].filter(Boolean).join(" · ")),
      h("a", { href: thumbUrl(img, "content"), target: "_blank", rel: "noopener", class: "small" }, "Open full size")));
}

// "keyframe take_1:0 (shot sh_01_02_01)" or "experiment exp_1 #3": where a picture came from.
function libSource(img) {
  const src = img.imported_from || "";
  let m = src.match(/^keyframe (\S+):(\d+) \(shot (\S+)\)$/);
  if (m) {
    const job = jobOf(img.package_id);
    const label = `Keyframe of shot ${m[3]}, draw ${Number(m[2]) + 1}`;
    return job ? h("a", { href: `#job=${encodeURIComponent(job.job_id)}&tab=keyframes`, title: "Open the story's keyframes" }, label) : label;
  }
  m = src.match(/^experiment (\S+) #(\d+)$/);
  if (m) {
    const study = (lib.chats || []).find((c) => c.session_id === m[1])?.kind === "face";
    const kind = study ? "face" : "chat";
    return h("a", { href: `#${study ? "face" : "exp"}=${encodeURIComponent(m[1])}`, title: `Open the ${study ? "study" : "chat"} it came from`,
      onclick: (e) => { if (e.button || e.metaKey || e.ctrlKey) return; e.preventDefault(); openBench(kind, m[1]); } },
      `${study ? "Study" : "Chat"} image #${m[2]}`);
  }
  return src ? `From ${src}` : "Drawn before takes";
}

function libLooseDraw(img) {
  return h("figure", { class: `draw done${img.selected ? " selected" : ""}` },
    h("button", { type: "button", class: "draw-pick", title: img.selected ? "Selected" : "Select this image",
      onclick: (e) => imageAction(`/v1/generated-images/${encodeURIComponent(img.image_id)}/select`, e.currentTarget) },
      h("img", { src: thumbUrl(img), alt: img.imported_from || "earlier draw", loading: "lazy" }), lookalikeTag(img.likeness), idTag(`design:${img.image_id}`)),
    h("figcaption", { class: "small" }, libSource(img), img.selected ? " ✓" : "",
      h("a", { href: thumbUrl(img, "content"), target: "_blank", rel: "noopener", class: "muted" }, " open")));
}

function renderLibAsset(a) {
  const part = lib.part && libParts(a).find((p) => p.asset_id === lib.part);
  if (lib.part && !part) return h("section", { class: "card" }, libCrumbs(a), h("p", {}, "That look, state or item isn't on this page any more."));
  if (!part) return renderLibTop(a);
  if (lib.shot) return renderLibShot(a, part, lib.shot);
  if (!lib.slot) return renderLibPart(a, part);
  const { v, slots } = libSlotData(part);
  if (!v) return h("div", { class: "lib-asset" }, libCrumbs(a, part), h("p", { class: "muted" }, "Loading takes…"));
  const slot = slots.find((s) => s.deliverable_id === lib.slot);
  if (!slot) return h("section", { class: "card" }, libCrumbs(a, part), h("p", {}, "That picture isn't drawn for it any more."));
  return renderLibSlot(a, part, slot);
}

// "+ New look" (or state): a line and a few sheet fields, added to the story the character
// or set came from; its picture page opens, ready for a first take.
const LIB_NEW = {
  character: [["wardrobe", "Wardrobe", "e.g. a black evening dress, one strap torn"], ["hair_and_makeup", "Hair and makeup", "e.g. hair down and wet, mascara running"], ["condition", "Condition", "e.g. soaked by rain, a cut on the lip"]],
  set: [["time_of_day", "Time of day", "e.g. night"], ["lighting", "Lighting", "e.g. one desk lamp, rain shadows on the wall"], ["dressing_changes", "Dressing changes", "e.g. chairs stacked, a banner up"], ["condition", "Condition", "e.g. ransacked"]],
};

function libNewVariant(a) {
  const meta = LIB_KINDS[a.kind];
  const draft = (lib.newDraft ??= {});
  const field = (key, label, hint, rows = 1) => {
    const box = h("textarea", { rows, placeholder: hint, "aria-label": label, oninput: (e) => { draft[key] = e.target.value; } });
    box.value = draft[key] || "";
    return h("label", { class: "field" }, h("span", {}, label), box);
  };
  const save = async (button) => {
    const description = (draft.description || "").trim();
    if (!description) { lib.newError = "Describe it in a line first."; return renderLibrary(); }
    const sheet = Object.fromEntries(LIB_NEW[a.kind].map(([k]) => [k, (draft[k] || "").trim()]).filter(([, x]) => x));
    button.disabled = true;
    try {
      const out = await api(`/v1/drama-packages/${encodeURIComponent(a.origin_package_id)}/variants`, {
        method: "POST", body: JSON.stringify({ parent_id: a.origin_entity_id, description, sheet, revision_id: a.origin_revision_id }) });
      Object.assign(lib, { adding: false, newDraft: {}, newError: "", stale: true });
      location.hash = libHref(out.lib_asset_id || a.asset_id, out.part_asset_id, out.deliverable_ids[0]).slice(1);
    } catch (e) {
      button.disabled = false;
      lib.newError = e.message;
      renderLibrary();
    }
  };
  return h("section", { class: "card lib-editor" },
    h("strong", {}, `New ${meta.one} for ${a.name}`),
    field("description", "In one line", a.kind === "character" ? "e.g. Wedding dress, rain-soaked, after she runs out" : "e.g. The shop the night of the break-in"),
    LIB_NEW[a.kind].map(([k, label, hint]) => field(k, label, hint)),
    h("p", { class: "muted small" }, `Added to the story ${a.name} comes from. No shot ${a.kind === "character" ? "wears" : "uses"} it yet; its picture is drawn ${a.kind === "character" ? "from the face" : "like any state"}.`),
    h("div", { class: "prompt-edit-actions" },
      h("button", { type: "button", class: "btn small primary-outline", onclick: (e) => save(e.currentTarget) }, `Add ${meta.one}`),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { Object.assign(lib, { adding: false, newError: "" }); renderLibrary(); } }, "Cancel")),
    lib.newError && h("p", { class: "small warn-text", role: "alert" }, lib.newError));
}

// "+ New item" on a character: a prop it carries, added to its story (owned by it), with a
// first state and a detail picture to draw. With a reference photo, a chat for that picture
// opens with the photo in it.
const LIB_ITEM_FIELDS = [["size_and_scale", "Size and scale", "e.g. palm-sized, 9 x 5 cm"], ["materials", "Materials", "e.g. heavy cream card stock"],
  ["color_and_finish", "Color and finish", "e.g. ivory, letterpress in charcoal"], ["condition_and_wear", "Condition and wear", "e.g. crisp, one corner bent"],
  ["distinguishing_marks", "Distinguishing marks", "e.g. a gallery logo embossed top left"], ["handling", "Handling", "e.g. she flicks it between two fingers"]];

function libNewItem(a) {
  const draft = (lib.itemDraft ??= {});
  const field = (key, label, hint) => {
    const box = h("textarea", { rows: 1, placeholder: hint, "aria-label": label, oninput: (e) => { draft[key] = e.target.value; } });
    box.value = draft[key] || "";
    return h("label", { class: "field" }, h("span", {}, label), box);
  };
  const photo = h("input", { type: "file", accept: "image/jpeg,image/png,image/webp", "aria-label": "Reference photo",
    onchange: (e) => { lib.itemPhoto = e.target.files[0] || null; } });
  const save = async (button) => {
    const name = (draft.name || "").trim();
    if (!name) { lib.itemError = "Name the item first."; return renderLibrary(); }
    const sheet = Object.fromEntries(LIB_ITEM_FIELDS.map(([k]) => [k, (draft[k] || "").trim()]).filter(([, x]) => x));
    button.disabled = true;
    try {
      const out = await api(`/v1/drama-packages/${encodeURIComponent(a.origin_package_id)}/items`, {
        method: "POST", body: JSON.stringify({ owner_id: a.origin_entity_id, name, story_function: (draft.story_function || "").trim(),
          state: (draft.state || "").trim(), sheet, revision_id: a.origin_revision_id }) });
      const file = lib.itemPhoto;
      Object.assign(lib, { addingItem: false, itemDraft: {}, itemError: "", itemPhoto: null, stale: true });
      // The reference photo starts a chat for the new picture once its page is up.
      if (file) lib.pendingPhoto = { file, target: { package_id: out.package_id, requirement_id: out.requirement_id, deliverable_id: out.deliverable_ids[0], label: `${name}, detail` } };
      location.hash = libHref(out.lib_asset_id || a.asset_id, out.part_asset_id, out.deliverable_ids[0]).slice(1);
    } catch (e) {
      button.disabled = false;
      lib.itemError = e.message;
      renderLibrary();
    }
  };
  return h("section", { class: "card lib-editor" },
    h("strong", {}, `New item ${a.name} carries`),
    field("name", "Name", "e.g. Business card"),
    field("story_function", "What it does in the story", "e.g. Gives Ryder a reason to call her"),
    field("state", "How it first appears", "e.g. Crisp and new (default: As it first appears)"),
    stickyDetails(`newitem:${a.asset_id}`, "What it looks like (optional)", LIB_ITEM_FIELDS.map(([k, label, hint]) => field(k, label, hint))),
    h("label", { class: "field" }, h("span", {}, "Reference photo (optional)"), photo,
      h("span", { class: "muted small" }, "Starts a chat for its picture with this photo; say what to make of it, then Use as pick.")),
    h("p", { class: "muted small" }, `Added to the story ${a.name} comes from, as a prop ${a.name} carries (in its Props too). No shot uses it yet.`),
    h("div", { class: "prompt-edit-actions" },
      h("button", { type: "button", class: "btn small primary-outline", onclick: (e) => save(e.currentTarget) }, "Add item"),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { Object.assign(lib, { addingItem: false, itemError: "", itemPhoto: null }); renderLibrary(); } }, "Cancel")),
    lib.itemError && h("p", { class: "small warn-text", role: "alert" }, lib.itemError));
}

// An item's entry in its story's Props (the Casting tab), and back.
function libPropLink(part) {
  const job = jobOf(part.origin_package_id);
  if (!job) return null;
  return h("p", { class: "small" }, h("a", { href: `#job=${encodeURIComponent(job.job_id)}&tab=casting&prop=${encodeURIComponent(part.origin_entity_id)}`,
    title: "This item in the story's list of props" }, "See it in the story's Props"), h("span", { class: "muted mono" }, ` ${part.origin_entity_id}`));
}

// After a draw, a pick or a new take: the asset again (its current pictures) and its takes.
async function libAfterDesign() {
  await libReload();
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

// The workbench (docs/workbench.md): develop a picture in a chat that starts from its pick;
// a result's "Use as pick" makes it this picture's pick. The slot's chats are listed.
// What a study of this picture describes and how its tests are drawn (face_lab.SUBJECTS).
const LIB_STUDY = {
  character_identity: { what: "the face", from: "text alone" },
  character_look: { what: "the outfit, hair and pose (never the photo's face)", from: "the face's pick" },
  location: { what: "the set, empty of people", from: "text alone, or the first state's pick" },
  prop: { what: "the object", from: "text alone, or the first state's pick" },
};

function libWorkbench(part, slot, sayView) {
  const mine = (lib.chats || []).filter((s) => s.target && s.target.package_id === part.origin_package_id
    && s.target.requirement_id === slot.requirement_id && s.target.deliverable_id === slot.deliverable_id);
  // "Lila Montgomery, evening torn, full body": the variant by its short name, not its line.
  const label = `${slot.entity}${slot.variant_id ? `, ${libVariant(slot.variant_id, slot.entity_id).toLowerCase()}` : ""}, ${slot.view}`;
  const target = { package_id: part.origin_package_id, requirement_id: slot.requirement_id, deliverable_id: slot.deliverable_id, label };
  const study = LIB_STUDY[slot.asset_type] || LIB_STUDY.character_identity;
  // A study (as Face Lab, for every kind of picture): a photo described in words, or words
  // of your own, tested seed by seed; Use as pick on a render.
  const photo = h("input", {
    type: "file", accept: "image/jpeg,image/png,image/webp", hidden: true,
    onchange: (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) benchFaceFromPhoto(f, target, fromPhoto); },
  });
  const fromPhoto = h("button", { type: "button", class: "btn small",
    title: `Upload a reference photo: GPT describes ${study.what} in words, and you test the wording seed by seed (drawn from ${study.from}); Use as pick on a render`,
    onclick: () => photo.click() }, "From a photo");
  const fromText = h("button", {
    type: "button", class: "btn small",
    title: `Write the description yourself and test it seed by seed (drawn from ${study.from}); Use as pick on a render`,
    onclick: (e) => benchFaceFromText(target, e.currentTarget),
  }, "Create from text");
  const develop = async (button) => {
    button.disabled = true;
    try {
      // Clicked again before writing anything: the untouched chat opens, not another one.
      const s = await api("/v1/experiments", { method: "POST", body: JSON.stringify({ target, reuse_empty: true }) });
      button.disabled = false;
      openBench("chat", s.session_id, label);
    } catch (e) {
      button.disabled = false;
      lib.error = e.message;
      renderLibrary();
    }
  };
  return [
    h("div", { class: "design-row small" },
      h("button", { type: "button", class: "btn small", title: "Change this picture in a chat with the image model, starting from its pick (or, with none, from your words); attach a photo in the chat to work from it", onclick: (e) => develop(e.currentTarget) }, sayView ? `Develop the ${slot.view} in a chat` : "Develop in a chat"),
      fromPhoto, photo, fromText,
      libLinkScratch(target, true)),
    libHistory(mine)];
}

// The picture's chats and studies, newest first: each with its newest image, what it is,
// how much is in it and when it was last used. Each opens in the workbench panel.
function libHistory(mine) {
  if (!mine.length) return h("p", { class: "muted small lib-history-empty" }, "No chats or studies for this picture yet.");
  const all = lib.historyAll;
  const shown = all ? mine : mine.slice(0, 8);
  return h("div", { class: "lib-history" },
    h("div", { class: "muted small" }, `History (${mine.length})`),
    h("ul", { class: "plain lib-history-list" }, shown.map((s) => {
      const kind = s.kind === "face" ? "face" : "chat";
      return h("li", {}, h("a", {
        href: `#${kind === "face" ? "face" : "exp"}=${encodeURIComponent(s.session_id)}`, class: "lib-history-item",
        title: `Open this ${kind === "face" ? "study" : "chat"} in the panel`,
        // Opens in the panel; a middle click still opens the full page.
        onclick: (e) => { if (e.button || e.metaKey || e.ctrlKey) return; e.preventDefault(); openBench(kind, s.session_id, s.title); },
      },
        s.thumb ? h("img", { src: s.thumb, alt: "", loading: "lazy" }) : h("span", { class: "lib-noimg" }),
        h("span", { class: "lib-history-text" },
          h("strong", {}, s.title),
          h("span", { class: "muted small" }, [
            kind === "face" ? "study" : "chat",
            s.messages ? `${s.messages} ${kind === "face" ? "step" : "message"}${s.messages === 1 ? "" : "s"}` : "nothing in it yet",
            s.images ? `${s.images} image${s.images === 1 ? "" : "s"}` : null,
            timeAgo(s.updated_at),
          ].filter(Boolean).join(" · ")))));
    })),
    mine.length > 8 && h("button", { type: "button", class: "btn ghost small", onclick: () => { lib.historyAll = !all; renderLibrary(); } },
      all ? "Show fewer" : `Show all ${mine.length}`));
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
