---
name: minimax-h3-image-prompt
description: Write, review or fix prompts for MiniMax-H3-Image (text-to-image and reference-image editing), including the drama agent's character, look, set and prop reference images and its image prompt writer (src/drama_agent/image_prompts.py, images.py). Use when writing an H3 image prompt, judging why an H3 image came out wrong, changing how the app builds image prompts, or rendering a prompt through MCPHub's GPU route.
---

# MiniMax-H3-Image prompts

MiniMax-H3-Image renders from a complete description of the finished image: what the image is,
what is in it and where, how it looks, and every word that appears on it. Our deployment
(MCPHub's GPU route, an SGLang server) **does not expand prompts**: the MiniMax API runs a
prompt expander first, a local server renders the prompt exactly as written. So the prompt has to
be the finished, expanded description.

The rules come from MiniMax's own prompt-writing skill and guides, and the system prompt of its
prompt-expansion model. Sources are in [references/sources.md](references/sources.md).

## Facts about the model and our route

| | |
| --- | --- |
| Endpoint | `POST https://api.mcphub.com/api/v3/gpu-images/minimax-h3-image/generations` (JSON) or `/edits` (multipart, `image` repeated) |
| Auth | MCPHub key in `X-API-Key` (Bearer is refused); `newsbreak.com` accounts only |
| Prompt | English description; at most ~4,096 tokens are read (32,000 characters accepted). No negative prompt. Steps and guidance are fixed. |
| References | `<Picture 1>`, `<Picture 2>`, ... in upload order. MCPHub accepts at most 9 images, 24 MiB in total (the model itself takes 16). |
| Size | Best at about 16 MP, both sides multiples of 32: 1:1 `4096x4096`, 3:4 `3552x4736`, 4:3 `4736x3552`, 2:3 `3360x5024`, 3:2 `5024x3360`, 9:16 `3072x5472`, 16:9 `5472x3072`, 21:9 `6272x2688`. About 1 MP (e.g. `1024x1024`) works for drafts but is harsher. |
| Speed | One image at a time on the GPU; ~8-14 s each at 16 MP when nobody else is queued |
| Seed | Same prompt, size and seed reproduce the image |

## Workflow

1. **Pick the task.** No input images: text-to-image. Any input image to change, combine or borrow
   from: an edit. Look at every input image, in order, before writing an edit.
2. **Plan in this order:** purpose and deliverable, layout, aspect ratio, the subjects and what
   they do, the style, the text. Fill ordinary gaps yourself; ask only when a missing fact would
   make the prompt wrong.
3. **Choose and lock the aspect ratio** from the deliverable and the smallest natural composition,
   before adding detail; later detail must not change it.
4. **Write** with the rules (next section).
5. **Check** with the final check, then deliver the prompt and its size.

## Writing rules, drama recipes and final check

The rules live in the app's package so the app's prompt writer and this skill use the same text:

- **[src/drama_agent/prompts/h3_image_rules.md](../../../src/drama_agent/prompts/h3_image_rules.md)**:
  writing rules (describe the image, text in the image, reference images and editing with
  `<Subject N>`, grids and multi-view), the recipes for the drama's character, look, set and prop
  images, and the final check. **Read it before writing or reviewing a prompt.**
- **[src/drama_agent/prompts/h3_image_examples.md](../../../src/drama_agent/prompts/h3_image_examples.md)**:
  finished prompts for the sample drama, text-to-image and edits.

Edit those files to change the rules; the app picks the change up (bump
`PROMPT_RULES_VERSION` in `image_prompts.py`).

## Output format

Give the prompt in a text block and the size on its own line:

````text
```text
<the prompt>
```

Size: 3552x4736 (3:4)
````

## Rendering a prompt (MCPHub)

```bash
# Text to image
curl -s https://api.mcphub.com/api/v3/gpu-images/minimax-h3-image/generations \
  -H "X-API-Key: $MCPHUB_KEY" -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile p prompt.txt '{prompt: $p, size: "3552x4736", seed: 1, output_format: "jpeg"}')" \
  | python3 -c 'import sys,json,base64; open("out.jpg","wb").write(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"]))'

# Edit: images in <Picture N> order; --form-string so a prompt starting with "<" is not read as a file
curl -s https://api.mcphub.com/api/v3/gpu-images/minimax-h3-image/edits -H "X-API-Key: $MCPHUB_KEY" \
  --form-string "prompt=$(cat edit.txt)" -F "image=@face.jpg;type=image/jpeg" \
  -F "size=3072x5472" -F "seed=1" -F "output_format=jpeg" \
  | python3 -c 'import sys,json,base64; open("out.jpg","wb").write(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"]))'
```

The GPU is shared and works on one image at a time: render one after another, never in parallel,
with a generous client timeout (600 s). In this repo the key is in `.env`; only render when the
user asked for real images.
