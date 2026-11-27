"""Export real keyframe takes as replayable requests: prompt, seed, size, and the reference
pictures exactly as the app sends them (shrunk to the keyframe's short edge)."""
import json, sqlite3, sys, shutil
from pathlib import Path
from drama_agent.shots import assemble as asm

out = Path(sys.argv[1]); media = Path("data/media")
db = sqlite3.connect("file:data/drama.db?mode=ro", uri=True)
def take(tid):
    return json.loads(db.execute("select data from shot_takes where take_id=?", (tid,)).fetchone()[0])
def ref_path(ref_id):
    if ref_id.startswith("frame:"):
        _, tid, n = ref_id.split(":")
        d = next(d for d in take(tid)["results"] if d["n"] == int(n)); return media / d["file"]
    row = db.execute("select data from generated_images where image_id=?", (ref_id,)).fetchone()
    return media / json.loads(row[0])["file"]
for tid in sys.argv[2:]:
    t = take(tid); d = out / tid; d.mkdir(parents=True)
    (d / "prompt.txt").write_text(t["prompt"])
    refs = []
    for i, r in enumerate(t["references"]):
        p = ref_path(r["ref_id"]); name = f"ref{i}.jpg"
        data = asm.shrink_picture(p, 1024); (d / name).write_bytes(data if data is not None else p.read_bytes()); refs.append(name)
    first = t["results"][0]
    shutil.copy(media / first["file"], d / "app_draw0.jpg")
    json.dump({"take_id": tid, "shot_id": t["shot_id"], "size": t["size"], "seed": t["seeds"][0],
               "refs": refs, "roles": [r["role"] for r in t["references"]]}, open(d / "meta.json", "w"), indent=1)
    print(tid, t["shot_id"], t["size"], len(refs), "refs", [round((d / n).stat().st_size / 1024) for n in refs], "KB")
