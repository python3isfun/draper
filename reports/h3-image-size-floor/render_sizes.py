"""Render one MiniMax-H3-Image prompt at falling sizes through MCPHub's GPU route and time
each call (reports/h3-image-size-floor.md).

Run from the repo root (it reads MCPHUB_KEY from .env); real, paid calls, one at a time:
    .venv/bin/python reports/h3-image-size-floor/render_sizes.py <out_dir>
Writes <size>.jpg per size and times.json.
"""

import base64
import json
import sys
import time
from pathlib import Path

import httpx

URL = "https://api.mcphub.com/api/v3/gpu-images/minimax-h3-image/generations"
PROMPT = (
    "A studio casting photograph of one woman in her early thirties, head and shoulders, "
    "front-facing, looking straight into the camera with a calm, slight smile. She has warm "
    "brown skin, dark brown eyes, shoulder-length black curly hair parted on the left, a small "
    "mole above the right corner of her upper lip and thin gold hoop earrings. She wears a "
    "plain charcoal crew-neck sweater. Soft key light from a large softbox at camera left, a "
    "gentle fill from the right, even plain light-grey seamless backdrop. Natural skin texture, "
    "sharp focus on the eyes. The image contains no text, letters or numbers."
)
SIZES = ["1728x2304", "1152x1536", "768x1024", "576x768", "384x512", "288x384", "192x256", "96x128"]
SEED = 7


def main(out: Path) -> None:
    lines = Path(".env").read_text().splitlines()
    key = next(x.split("=", 1)[1].strip().strip('"') for x in lines if x.startswith("MCPHUB_KEY="))
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with httpx.Client(timeout=600) as client:
        for size in SIZES:  # one at a time: the GPU draws one image at a time
            t0 = time.monotonic()
            row: dict = {"size": size}
            try:
                r = client.post(URL, headers={"X-API-Key": key}, json={
                    "prompt": PROMPT, "size": size, "seed": SEED, "output_format": "jpeg"})  # fmt: skip
                row["s"] = round(time.monotonic() - t0, 1)
                if r.status_code != 200:
                    row["error"] = f"{r.status_code} {r.text[:200]}"
                else:
                    data = base64.b64decode(r.json()["data"][0]["b64_json"])
                    (out / f"{size}.jpg").write_bytes(data)
                    row["kb"] = len(data) // 1024
            except httpx.HTTPError as exc:
                row.update(s=round(time.monotonic() - t0, 1), error=repr(exc)[:200])
            rows.append(row)
            print(json.dumps(row), flush=True)
    (out / "times.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
