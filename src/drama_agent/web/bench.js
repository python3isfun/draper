"use strict";

/*
 * The workbench panel (docs/workbench.md, phase 2): a chat (experiment.js) or a Face Lab
 * study (face.js) opened over a character or set page, so the sheet, the other looks and
 * the out-of-date flags stay in view. The views mount into the panel's host element
 * (exp.host / face.host) instead of #main. Uses h(), api(), $, uploadFile() from app.js and
 * libLoadDesign() from library.js.
 */

const bench = { kind: null, sid: null };

// Open a chat ("chat") or a study ("face") in the panel.
function openBench(kind, sid, label) {
  closeBench({ refresh: false });
  bench.kind = kind;
  bench.sid = sid;
  const host = h("div", { class: "bench-host" });
  $("#bench-title").textContent = label || (kind === "face" ? "Study" : "Chat");
  $("#bench-full").href = `#${kind === "face" ? "face" : "exp"}=${encodeURIComponent(sid)}`;
  $("#bench-body").replaceChildren(host);
  $("#bench").hidden = false;
  document.body.classList.add("bench-open");
  $("#bench-close").focus();  // keyboard users land in the panel (Tab goes on into it)
  if (kind === "face") {
    Object.assign(face, { host, sid, view: null, mounted: null, error: "", used: {}, notice: "" });
    loadFaceStudy();
  } else {
    Object.assign(exp, { host, sid, view: null, mounted: null, error: "", used: {}, notice: "", renaming: false });
    loadExpSession();
  }
}

// Close the panel. `refresh`: show the page's pictures and chats again (off when the page
// itself is being left).
function closeBench({ refresh = true } = {}) {
  if (!bench.kind) return;
  const kind = bench.kind;
  bench.kind = bench.sid = null;
  $("#bench").hidden = true;
  $("#bench-body").replaceChildren();  // detaches the host: polls in flight stop drawing
  document.body.classList.remove("bench-open");
  if (kind === "face") {
    stopFace();
    face.host = null;
  } else {
    stopExp();
    exp.host = null;
  }
  if (refresh) benchRefreshPage();
}

// The page under the panel shows what changed: a new pick, what is now out of date, the
// picture's chats.
function benchRefreshPage() {
  const inLib = ["chars", "sets"].includes(document.body.dataset.mode);
  if (inLib && lib.kind && lib.id) libReload();
  if (inLib && lib.kind) libLoadScratch();  // a chat may have been made, renamed or deleted
  // A keyframe on show (the story's Keyframes tab, or a shot on a look's page).
  if (frames.pkg && (inLib || state.tab === "keyframes")) loadFrames(frames.pkg);
}

// "From a photo" on a face: upload a reference photo, start a study for that face (or, with
// no target, a scratch study) and have the photo described; the study opens in the panel
// (or, with `open` off, is only returned: the caller opens it). Returns { study, error }:
// the study (null if none was made) and, if it failed, why (also said beside the button).
async function benchFaceFromPhoto(file, target, button, { open = true } = {}) {
  // Problems are said beside the button: the page itself stays as it is.
  let error = "";
  const say = (msg) => {
    error = msg;
    button.parentElement?.querySelector(".bench-error")?.remove();
    button.after(h("span", { class: "form-error bench-error" }, msg));
  };
  if (!isPhotoType(file)) {
    say(photoTypeError(file));
    return { study: null, error };
  }
  const label = button.textContent;
  button.disabled = true;
  let study = null;
  try {
    button.textContent = "Uploading…";
    const asset = await uploadFile(file, (p) => { button.textContent = `Uploading (${Math.round(p * 100)}%)…`; }, () => { button.textContent = "Processing…"; });
    button.textContent = "Describing…";
    study = await api("/v1/experiments", { method: "POST", body: JSON.stringify(target ? { kind: "face", target } : { kind: "face" }) });
    await api(`/v1/experiments/${encodeURIComponent(study.session_id)}/describe`, {
      method: "POST",
      body: JSON.stringify({ asset_id: asset.asset_id, restrictive: restrictiveBox.checked }),
    });
  } catch (e) {
    if (!study) say(e.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
    // Open the study even if describing failed (it says why), so it isn't left unseen.
    if (study && open) openBench("face", study.session_id, target ? target.label : study.title);
  }
  return { study, error };
}

// "Create from text" on a face: a study for that face with no photo, where the description
// is written by hand and drawn from text alone (H3 or a local image server). Clicked again
// before anything is in it, the same empty study opens (`open` off: it is only returned).
// Returns { study, error } as benchFaceFromPhoto.
async function benchFaceFromText(target, button, { open = true } = {}) {
  button.parentElement?.querySelector(".bench-error")?.remove();
  button.disabled = true;
  try {
    const study = await api("/v1/experiments", { method: "POST", body: JSON.stringify({ kind: "face", target, reuse_empty: true }) });
    if (open) openBench("face", study.session_id, target.label);
    return { study, error: "" };
  } catch (e) {
    button.after(h("span", { class: "form-error bench-error" }, e.message));
    return { study: null, error: e.message };
  } finally {
    button.disabled = false;
  }
}

// "Develop in a chat" on a picture: its chat, starting from its pick. Clicked again before
// anything is written in it, the same untouched chat comes back, not another one. Returns
// { study, error } as benchFaceFromPhoto (the chat as `study`); the caller says the error.
async function benchStartChat(target) {
  try {
    return { study: await api("/v1/experiments", { method: "POST", body: JSON.stringify({ target, reuse_empty: true }) }), error: "" };
  } catch (e) {
    return { study: null, error: e.message };
  }
}

// The photos a study or chat takes.
const isPhotoType = (file) => /^image\/(jpeg|png|webp)$/.test(file.type);
const photoTypeError = (file) => `${file.name}: use a JPEG, PNG or WebP photo.`;

$("#bench-close").addEventListener("click", () => closeBench());

// Scratch's buttons (the Characters and Sets sidebar).
$("#scratch-chat").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  button.disabled = true;
  try {
    const s = await api("/v1/experiments", { method: "POST", body: "{}" });
    openBench("chat", s.session_id, s.title);
    libLoadScratch();
  } catch (err) {
    button.after(h("span", { class: "form-error bench-error" }, err.message));
  } finally {
    button.disabled = false;
  }
});
$("#scratch-face").addEventListener("click", () => $("#scratch-file").click());
$("#scratch-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) benchFaceFromPhoto(file, null, $("#scratch-face")).then(libLoadScratch);
});
document.addEventListener("keydown", (e) => {
  // Not while typing, and not while a picture is shown full size (Escape closes that).
  if (e.key === "Escape" && bench.kind && !$(".lightbox") && !e.target.closest?.("input, textarea, select")) closeBench();
});
