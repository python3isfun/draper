"use strict";

/*
 * Characters and Sets: the asset library (docs/entity-reorganization.md, phase 2).
 * Read pages for now: every character or set, and one with its sheet, its looks or states,
 * its items, current pictures (click to enlarge; out-of-date ones marked) and the stories
 * and shots that use it. Editing still happens in the story (Cast / Sets / Assets tabs);
 * each page links there.
 */

const lib = { kind: null, id: null, list: null, view: null, error: "", filter: "" };

const LIB_KINDS = {
  character: { mode: "chars", path: "characters", plural: "Characters", child: "looks", childLabel: "Looks", one: "look", many: "looks", storyTab: "cast" },
  set: { mode: "sets", path: "sets", plural: "Sets", child: "states", childLabel: "States", one: "state", many: "states", storyTab: "sets" },
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
  lib.id = id;
  lib.view = null;
  lib.error = "";
  renderLibrary();
  if (!state.jobs.length) await loadJobs();
  try {
    lib.list = await api(`/v1/library/${meta.path}`);
    if (id) lib.view = await api(`/v1/library/${meta.path}/${encodeURIComponent(id)}`);
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
  const f = lib.filter.trim().toLowerCase();
  const shown = lib.list.filter((a) => !f || a.name.toLowerCase().includes(f));
  list.replaceChildren(...shown.map((a) =>
    h("li", {}, h("a", { href: `#lib=${a.asset_id}`, class: `lib-side-item${a.asset_id === lib.id ? " active" : ""}` },
      a.cover ? h("img", { src: a.cover.thumb, alt: "", loading: "lazy" }) : h("span", { class: "lib-noimg" }),
      h("span", {}, a.name, h("span", { class: "muted small" }, ` · ${count(a.children, meta.one, meta.many)}`))))));
  if (!shown.length) list.replaceChildren(h("li", { class: "muted small" }, f ? "No match." : "Nothing yet."));
}

// Every character (or set) as a card with its cover.
function renderLibGrid() {
  const meta = LIB_KINDS[lib.kind];
  if (!lib.list) return h("p", { class: "muted" }, "Loading…");
  return h("section", {},
    h("div", { class: "section-head" }, h("h2", {}, meta.plural),
      h("span", { class: "muted small" }, `${lib.list.length} in the library, from every story. Edit them in their story for now.`)),
    h("div", { class: "lib-grid" }, lib.list.map((a) =>
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
    part.description && h("p", {}, part.description),
    libPictures(part.pictures, part.name),
    h("details", {}, h("summary", { class: "small" }, "Sheet"), libSheet(part.sheet)),
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
            title: "Editing happens in the story for now" }, "Edit in its story"))),
      a.description && h("p", { class: "muted" }, a.description),
      a.kind === "character" && libPictures(a.pictures, a.name),
      libSheet(a.sheet),
      h("div", { class: "small" }, h("strong", {}, "Used in"), libUsedIn(a.used_in, "storyboard"))),
    h("h3", {}, `${meta.childLabel} (${a[meta.child].length})`),
    a[meta.child].length ? a[meta.child].map((x) => libPart(x, a.kind === "character" ? "look" : "state", a.origin_entity_id)) : h("p", { class: "muted" }, "None yet."),
    h("h3", {}, a.kind === "character" ? `Carried items (${a.items.length})` : `Dressing (${a.items.length})`),
    a.items.length ? a.items.map((x) => libPart(x, "item", a.origin_entity_id)) : h("p", { class: "muted small" }, a.kind === "character" ? "Nothing this character carries is registered." : "No registered props dress this set."));
}

document.addEventListener("DOMContentLoaded", () => {
  const input = $("#lib-filter");
  if (input) input.addEventListener("input", (e) => { lib.filter = e.target.value; renderLibSide(); });
});
