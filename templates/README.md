# Story templates

One JSON file per template: the craft of a film or drama, abstracted, for creators to start
a new story from. See `docs/templates.md`.

- Learn one: `python -m drama_agent.templates learn "<title>" [--script stories/...]`
- List them: `python -m drama_agent.templates list`

Only the abstract pattern lives here. Scripts and synopses a template is learned from stay
in `stories/` (not committed). Read a new template before committing it: no names, lines or
signature scenes of the original should be in its pattern.
