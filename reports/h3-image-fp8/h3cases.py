"""Run a fixed list of H3-Image requests against one server and record time, memory and
GPU use; saves every output. usage: h3cases.py URL GPUS LABEL OUTDIR [replay|synthetic|all]"""
import base64, json, subprocess, sys, time, urllib.request, uuid
from pathlib import Path

URL, GPUS, LABEL, OUT = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
WHAT = sys.argv[5] if len(sys.argv) > 5 else "all"
OUT.mkdir(parents=True, exist_ok=True)
B = Path("/tmp/h3bench")
PROMPT = ("A medium close-up of a woman in her thirties with shoulder-length dark hair, wearing "
          "a cream blouse, standing by a tall window in a bright apartment bedroom, soft morning "
          "light from the left, shallow depth of field, natural skin texture, cinematic still.")
EDIT = ("Keep the woman from <Picture 1> with the same face, hair and blouse. Show her in a "
        "medium close-up turning toward the camera by the tall window, soft morning light from "
        "the left, shallow depth of field, natural skin texture, cinematic still.")

def sample_start(path):
    return subprocess.Popen(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used",
                             "--format=csv,noheader,nounits", "-i", GPUS, "-lms", "100"],
                            stdout=open(path, "w"), stderr=subprocess.DEVNULL)

def sample_read(path):
    rows = {}
    for line in open(path):
        p = [x.strip() for x in line.split(",")]
        if len(p) == 3 and p[1].isdigit():
            rows.setdefault(p[0], []).append((int(p[1]), int(p[2])))
    return {g: {"peak_mib": max(m for _, m in r), "mean_util": round(sum(u for u, _ in r) / len(r))}
            for g, r in rows.items() if r}

def rest():
    o = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits",
                        "-i", GPUS], capture_output=True, text=True).stdout
    return {l.split(",")[0].strip(): int(l.split(",")[1]) for l in o.strip().splitlines()}

def call(name, prompt, size, seed, refs):
    fields = {"prompt": prompt, "size": size, "seed": seed, "n": 1,
              "response_format": "b64_json", "output_format": "jpeg"}
    if refs:
        b = uuid.uuid4().hex; body = b""
        for k, v in fields.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for i, d in enumerate(refs):
            body += (f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="ref{i}.jpg"\r\n'
                     "Content-Type: image/jpeg\r\n\r\n").encode() + d + b"\r\n"
        body += f"--{b}--\r\n".encode()
        req = urllib.request.Request(URL + "/v1/images/edits", body,
                                     {"Content-Type": f"multipart/form-data; boundary={b}"})
    else:
        req = urllib.request.Request(URL + "/v1/images/generations", json.dumps(fields).encode(),
                                     {"Content-Type": "application/json"})
    before = rest(); path = str(OUT / f"{name}.csv"); s = sample_start(path); time.sleep(0.3)
    row = {"label": LABEL, "case": name, "size": size, "refs": len(refs), "seed": seed, "rest_mib": before}
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            d = json.load(resp)
        row.update(wall_s=round(time.time() - t, 2), infer_s=round(d.get("inference_time_s") or 0, 2),
                   torch_peak_mb=d.get("peak_memory_mb"))
        (OUT / f"{name}.jpg").write_bytes(base64.b64decode(d["data"][0]["b64_json"]))
    except Exception as exc:
        row["error"] = repr(exc)[:300]
    time.sleep(0.3); s.terminate(); s.wait(); row["gpu"] = sample_read(path)
    print(json.dumps(row), flush=True)
    return "error" not in row

r1024 = (B / "out_1024x1824.jpg").read_bytes(); r1536 = (B / "out_1536x2720.jpg").read_bytes()
cases = []
if WHAT in ("all", "replay"):
    for d in sorted((B / "replay").iterdir()):
        m = json.load(open(d / "meta.json"))
        cases.append((f"replay_{m['shot_id']}", (d / "prompt.txt").read_text(), m["size"], m["seed"],
                      [(d / n).read_bytes() for n in m["refs"]]))
if WHAT in ("all", "synthetic"):
    cases += [("t2i_1024", PROMPT, "1024x1824", 1000, []), ("t2i_1024_b", PROMPT, "1024x1824", 1001, []),
              ("t2i_1536", PROMPT, "1536x2720", 1000, []),
              ("edit_1024_r1", EDIT, "1024x1824", 1000, [r1024]),
              ("edit_1024_r3", EDIT, "1024x1824", 1000, [r1024] * 3),
              ("edit_1024_r6", EDIT, "1024x1824", 1000, [r1024] * 6),
              ("edit_1024_r6_b", EDIT, "1024x1824", 1001, [r1024] * 6)]
if WHAT == "all" and LABEL != "bf16-prod":  # the production cards cannot take these today
    cases += [("edit_1024_r9", EDIT, "1024x1824", 1000, [r1024] * 9),
              ("edit_1536_r6", EDIT, "1536x2720", 1000, [r1536] * 6),
              ("edit_1536_r9", EDIT, "1536x2720", 1000, [r1536] * 9)]
call("warmup", PROMPT, "512x896", 1, [])
for c in cases:
    if not call(*c):
        break
