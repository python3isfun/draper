"""Per-image benchmark of the H3-Image server: time, GPU memory and utilisation by size
and number of reference pictures. Runs on the GPU host against the Service; stops before
a case that would leave GPU 2 without room (an out-of-memory draw kills the server)."""

import base64
import json
import subprocess
import sys
import time
import urllib.request
import uuid

URL = "http://10.43.236.96:8000"
GPUS = "1,2"
TOTAL_MIB = 81559
MIN_FREE_MIB = 700  # stop a series when a case left less than this free at its peak
PROMPT = (
    "A medium close-up of a woman in her thirties with shoulder-length dark hair, wearing "
    "a cream blouse, standing by a tall window in a bright apartment bedroom, soft morning "
    "light from the left, shallow depth of field, natural skin texture, cinematic still."
)
EDIT = (
    "Keep the woman from <Picture 1> with the same face, hair and blouse. Show her in a "
    "medium close-up turning toward the camera by the tall window, soft morning light from "
    "the left, shallow depth of field, natural skin texture, cinematic still."
)
SIZES = ["512x896", "768x1344", "1024x1824", "1536x2720"]


def mp(size: str) -> float:
    w, h = (int(x) for x in size.split("x"))
    return w * h / 1e6


def sample_start(path: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used",
         "--format=csv,noheader,nounits", "-i", GPUS, "-lms", "100"],
        stdout=open(path, "w"), stderr=subprocess.DEVNULL)  # fmt: skip


def sample_read(path: str) -> dict:
    rows = {"1": [], "2": []}
    for line in open(path):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[0] in rows:
            rows[parts[0]].append((int(parts[1]), int(parts[2])))
    out = {}
    for g, r in rows.items():
        if not r:
            continue
        busy = [u for u, _ in r if u > 0]
        out[g] = {"peak_mib": max(m for _, m in r), "mean_util": round(sum(u for u, _ in r) / len(r)),
                  "max_util": max(u for u, _ in r), "busy_share": round(len(busy) / len(r), 2)}  # fmt: skip
    return out


def multipart(fields: dict, files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for name, data in files:
        body += (f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                 f"Content-Type: image/jpeg\r\n\r\n").encode() + data + b"\r\n"  # fmt: skip
    return body + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def call(size: str, refs: list[bytes], seed: int) -> dict:
    fields = {"prompt": EDIT if refs else PROMPT, "size": size, "seed": seed, "n": 1,
              "response_format": "b64_json", "output_format": "jpeg"}  # fmt: skip
    if refs:
        body, ctype = multipart(fields, [(f"ref{i}.jpg", d) for i, d in enumerate(refs)])
        req = urllib.request.Request(URL + "/v1/images/edits", body, {"Content-Type": ctype})
    else:
        req = urllib.request.Request(URL + "/v1/images/generations", json.dumps(fields).encode(),
                                     {"Content-Type": "application/json"})  # fmt: skip
    t = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        d = json.load(resp)
    return {"wall_s": round(time.time() - t, 2), "infer_s": round(d.get("inference_time_s") or 0, 2),
            "torch_peak_mb": d.get("peak_memory_mb"), "image": base64.b64decode(d["data"][0]["b64_json"])}  # fmt: skip


def rest() -> dict:
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits",
                          "-i", GPUS], capture_output=True, text=True).stdout  # fmt: skip
    return {l.split(",")[0].strip(): int(l.split(",")[1]) for l in out.strip().splitlines()}


def run(size: str, ref_size: str | None, n: int, refs_by_size: dict, reps: int = 2) -> dict | None:
    refs = [refs_by_size[ref_size]] * n if n else []
    best = None
    for rep in range(reps):
        path = f"/tmp/h3bench/s_{size}_{ref_size}_{n}_{rep}.csv"
        before = rest()
        s = sample_start(path)
        time.sleep(0.3)
        try:
            r = call(size, refs, seed=1000 + rep)
        except Exception as exc:  # noqa: BLE001
            s.terminate()
            print(json.dumps({"size": size, "ref_size": ref_size, "refs": n, "error": repr(exc)[:300]}), flush=True)
            return None
        time.sleep(0.3)
        s.terminate()
        s.wait()
        g = sample_read(path)
        row = {"size": size, "ref_size": ref_size, "refs": n, "rep": rep,
               "total_mp": round(mp(size) + n * (mp(ref_size) if ref_size else 0), 2),
               "wall_s": r["wall_s"], "infer_s": r["infer_s"], "torch_peak_mb": r["torch_peak_mb"],
               "rest_mib": before, "gpu": g, "bytes": len(r["image"])}  # fmt: skip
        print(json.dumps(row), flush=True)
        if n == 0:
            open(f"/tmp/h3bench/out_{size}.jpg", "wb").write(r["image"])
            refs_by_size.setdefault(size, r["image"])
        best = row
        time.sleep(1.0)
    return best


def main() -> None:
    refs_by_size: dict[str, bytes] = {}
    cases: list[tuple[str, str | None, int]] = [(s, None, 0) for s in SIZES]
    # References at the output's own size (as draper sends them), then smaller references.
    for s in SIZES:
        for n in (1, 3, 6, 9):
            cases.append((s, s, n))
    for n in (3, 6, 9):
        cases.append(("1024x1824", "512x896", n))
        cases.append(("1536x2720", "768x1344", n))
    cases.sort(key=lambda c: (c[2] > 0, mp(c[0]) + c[2] * (mp(c[1]) if c[1] else 0)))
    limit_mp = float(sys.argv[1]) if len(sys.argv) > 1 else 14.0
    stopped_at = None
    for size, ref_size, n in cases:
        total = mp(size) + n * (mp(ref_size) if ref_size else 0)
        if total > limit_mp or (stopped_at is not None and total >= stopped_at):
            print(json.dumps({"size": size, "ref_size": ref_size, "refs": n, "total_mp": round(total, 2),
                              "skipped": "over the memory guard"}), flush=True)  # fmt: skip
            continue
        row = run(size, ref_size, n, refs_by_size)
        if row is None:
            stopped_at = total
            continue
        free = TOTAL_MIB - row["gpu"].get("2", {}).get("peak_mib", 0)
        if free < MIN_FREE_MIB:
            stopped_at = total
            print(json.dumps({"guard": f"GPU 2 had {free} MiB free at the peak; skipping larger cases"}), flush=True)


if __name__ == "__main__":
    main()
