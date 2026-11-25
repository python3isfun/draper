# <What was reviewed>: <one-line verdict>

Date: <YYYY-MM-DD>.

## Summary

<Three to five sentences: what each clip does, the biggest gap, its cause upstream. Then
the ranked lessons, one line each.>

## What was reviewed

| | <Clip A> | <Clip B> |
| --- | --- | --- |
| File | <path> | <path> |
| Story | <one line> | <one line> |
| Package | <package_id, revision, written before or after which rules> | |

Method: cuts by `skills/director/scripts/measure.py` (merged or split by eye: say which),
shot sizes from a frame at the middle of each shot, loudness by `volumedetect`, the
storyboard and lines from the package API.

## Headline numbers

| | <A> | <B> | Target |
| --- | --- | --- | --- |
| Length | | | |
| Shots | | | |
| Mean / median shot | | | 2.0 s median or less (brisk) |
| Shots over 4 s | | | 10% or less |
| CU + MCU + OTS | | | 60% or more |
| Wide two-/group shots | | | 30% or less |
| Inserts | | | |
| Locations | | | |
| Lines, words per line, lines over 8 words | | | 6 or less, 15% or less |
| Loudness mean / peak | | | peak below -1 dB |
| Picture | | | |

## Shot by shot

| # | Start | Length | Size and subject | Line on screen (whose) | What it does |
| --- | --- | --- | --- | --- | --- |

<For our episode, also: what the storyboard asked for (shot count, lengths, framings,
camera) and how the cut plan split it.>

## Findings

### 1. <Finding as a lesson>

<What the better clip does, with shot numbers. What ours does, with shot numbers. Why it
matters on a phone screen.>

**Lesson:** <one rule a storyboard or script could follow>

<... up to five, ranked by impact; then "Also worth noting".>

## Where the reference goes wrong (don't copy)

## What to change in Draper

<Ranked. Each: the step, the file and rule (skills/director/references/draper-levers.md),
the change, and how to re-measure.>

## Measuring the next render

<The metrics table with "now" and "target", and the commands.>
