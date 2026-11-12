# Sources

Researched 2026-11-11. MiniMax's own material is authoritative; where a third-party source
disagreed, the official one won (noted below).

## Official (MiniMax)

- [MiniMax-AI/MiniMax-H3-Image](https://github.com/MiniMax-AI/MiniMax-H3-Image): the model repo.
  "Prompting Tips", the Sizes table (16 MP sizes, multiples of 32), request parameters (fixed
  steps and guidance, no negative prompt, 4,096-token prompt limit, 16 references), and the note
  that a local server renders the prompt as given while the API expands it first.
- [skills/h3-image-prompt-writing](https://github.com/MiniMax-AI/MiniMax-H3-Image/tree/main/skills/h3-image-prompt-writing):
  MiniMax's prompt-writing skill (`SKILL.md`, `references/t2i-en.txt`,
  `references/edit-en.txt`). Source of the writing rules, the edit paragraph structure with
  `<Subject N>` and the four relationship forms, the no-text sentence and the final check.
  Install it as is with
  `npx skills add https://github.com/MiniMax-AI/MiniMax-H3-Image --skill h3-image-prompt-writing`.
- [examples/](https://github.com/MiniMax-AI/MiniMax-H3-Image/tree/main/examples): a short request
  next to the API's expanded prompt (`instruction.*.txt` vs `revised_prompt.*.txt`), showing how
  much detail a finished prompt carries.
- [MiniMaxAI/MiniMax-H3-Image-PE](https://huggingface.co/MiniMaxAI/MiniMax-H3-Image-PE): the
  official prompt-expansion model. Its `prompt/t2i/task_prompt.txt` is the source of the aspect-ratio
  policy (lock the ratio from the deliverable before adding detail; "cinematic" or "portrait"
  don't set orientation), camera-relative body orientation, contact and hand rules, subject-count
  discipline and paired-view correspondence.
- [MiniMax API image generation guide](https://platform.minimax.io/docs/guides/image-generation):
  `revised_prompt`, `disable_prompt_enhanced`.
- MCPHub internal doc `gpu-images-minimax-h3.md` (amber-h3-image repo): the GPU route, `X-API-Key`
  auth, 9-reference / 24 MiB limit, one-image-at-a-time behaviour, ~16 MP quality note.

## Third party (used only where consistent with the official material)

- [PrunaAI/P-H3-Image](https://huggingface.co/PrunaAI/P-H3-Image): an H3-based image model. Agrees
  on opening with what the image is, light with source and direction, no hype words, naming what
  an edit keeps. **Overruled:** it says not to mention text when none is wanted; MiniMax's skill
  says to state "The image contains no text, letters or numbers." explicitly. Its 8 MP sizes
  are for that model, not ours.
- [MiniMax-H3 reference-video prompt guide](https://huggingface.co/Simplismart/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md):
  the video model's `<Subject N>` definitions and preservation markers, consistent with the image
  edit guide.
- [JahJedi/MiniMax-H3-Character-Sheet](https://huggingface.co/JahJedi/MiniMax-H3-Character-Sheet):
  community character sheets on the video model; useful tips (exact angles, constraints as positive
  prose, name every panel).
- Third-party H3 guides ([RunDiffusion](https://www.rundiffusion.com/minimax-h3-prompt-guide),
  [Atlas Cloud](https://www.atlascloud.ai/blog/tips/minimax-h3-prompt-guide)) and the community
  `awesome-minimax-h3-prompts` repos on GitHub: almost entirely about H3 *video*; not used for
  image rules.
- Reddit: no H3-Image threads were found (and reddit.com can't be fetched by the research tools).
