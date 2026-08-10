# Getting `article-medium.md` into Medium

## Why this file exists

`docs/article-cross-cloud-auth.md` is the repo version: markdown tables, GitHub
renders them, done. Medium does not render markdown tables at all, and pasting
one produces a wall of pipe characters. So the Medium version carries the same
content with every table rendered as an image instead.

That is the only structural difference. The prose is the same argument.

## The steps

1. Open a new Medium story and paste the body of `article-medium.md`, starting
   at the H1. Medium keeps `#`/`##`, `>`, backtick fences, bold and italics from
   pasted markdown; it drops image references, because it has no way to resolve
   a relative path.

2. Upload the nine images by hand, in order, at the point each `![...]` line
   sits. Delete the `![...]` line once its image is in place.

   | # | File | Section |
   |---|---|---|
   | 1 | `img/three-clouds-architecture.jpg` | hero |
   | 2 | `img/medium/01-coordinator-choice.png` | The one decision |
   | 3 | `img/medium/02-three-legs.png` | Three legs, three mechanisms |
   | 4 | `img/medium/07-traps.png` | Five traps |
   | 5 | `img/medium/04-matrix.png` | Does it actually interoperate? |
   | 6 | `img/medium/06-cold-warm.png` | Deployment decisions |
   | 7 | `img/medium/08-scaffolding.png` | Scaffolding worth stealing |
   | 8 | `img/medium/05-controls.png` | Scaffolding worth stealing |
   | 9 | `img/medium/03-latency.png` | What it costs |

3. **Paste each image's alt text into Medium's caption field.** The alt text in
   `article-medium.md` states the numbers in words. Every table in this piece is
   an image, so without captions a screen reader — and Medium's own search index
   — gets nothing from a third of the article. Medium's alt-text field is behind
   the image's `⌥`/settings control; the caption is the visible line under it.
   Use both.

4. Set the images to full width (click the image, pick the widest layout). They
   are rendered 1500px wide, which is enough for Medium's largest layout on a
   retina screen.

5. Subtitle: the `###` line under the H1 becomes Medium's subtitle if you paste
   it as the second block. Check it did — Medium sometimes takes the first
   paragraph instead.

## Regenerating the graphics

```bash
python3 docs/img/make_medium_graphics.py
```

Every number in those images is hard-coded in that script, sourced from the
rebuilt-mesh verification pass of 2026-08-07/08 recorded in `README.md` and
`docs/DEPLOYMENT_PLAN.md`. **If a measurement changes there, change it in the
script too** — an image is the one place in this repo where a stale number
cannot be caught by grep.

Requires `matplotlib` on the system interpreter (`uv pip install --system
matplotlib`), and Liberation Sans / DejaVu Sans, both of which are already
present on this machine.

## What was checked

- Palette validated against the dataviz skill's six checks on a `#ffffff`
  surface: blue `#2a78d6` and orange `#eb6834` pass lightness band, chroma
  floor, CVD separation (worst adjacent ΔE 24.7 protan), normal-vision floor,
  and 3:1 contrast.
- Every image was rendered and inspected for clipping and collisions rather
  than assumed correct — four of the eight needed layout fixes on the first
  pass, including a check-mark glyph that Liberation Sans does not carry.
