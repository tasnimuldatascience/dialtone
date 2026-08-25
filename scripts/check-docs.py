"""Does every link, image and anchor in the docs actually resolve?

A README is the first thing anyone reads, and a dead link in it is the cheapest possible way to
look careless. WRITTEN AFTER FINDING A BADGE READING "tests 603 passing" against a suite of 624 --
nobody had touched it in weeks and nobody had noticed, because a badge is an image and an image
looks fine whatever it says.

    python scripts/check-docs.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ["README.md", "docs/COMPARISON.md", "docs/EVALUATION.md", "docs/DEBUGGING.md"]

problems = []


def slug(heading: str) -> str:
    text = heading.lstrip("#").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


for name in DOCS:
    path = ROOT / name
    if not path.exists():
        problems.append(f"{name}: missing")
        continue
    body = path.read_text(encoding="utf-8")
    anchors = {slug(line) for line in body.splitlines() if line.startswith("#")}

    for label, target in re.findall(r"\[([^\]]*)\]\(([^)]+)\)", body):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            if target[1:] not in anchors:
                problems.append(f"{name}: anchor {target} does not exist")
            continue
        file_part = target.split("#")[0]
        resolved = (path.parent / file_part).resolve()
        if not resolved.exists():
            problems.append(f"{name}: {target} -> missing file")

    # Fenced blocks must be balanced, or half the page renders as code.
    if body.count("```") % 2:
        problems.append(f"{name}: unbalanced ``` fences")

    # Mermaid: every node referenced by an edge must be declared.
    for block in re.findall(r"```mermaid\n(.*?)```", body, re.DOTALL):
        declared = set(re.findall(r"^\s*(\w+)[\[\(\{]", block, re.MULTILINE))
        for a, b in re.findall(r"^\s*(\w+)\s*--[^>]*>\s*(?:\|[^|]*\|)?\s*(\w+)", block, re.MULTILINE):
            for node in (a, b):
                if node not in declared:
                    problems.append(f"{name}: mermaid edge references undeclared {node!r}")

    trailing = [i + 1 for i, line in enumerate(body.splitlines()) if line.rstrip() != line]
    if trailing:
        problems.append(f"{name}: trailing whitespace on {len(trailing)} line(s)")

    long_lines = [i + 1 for i, line in enumerate(body.splitlines())
                  if len(line) > 110 and not line.lstrip().startswith("|")
                  and "](" not in line and not line.startswith("    ")]
    if long_lines:
        problems.append(f"{name}: {len(long_lines)} line(s) over 110 chars: {long_lines[:6]}")

for name in DOCS:
    body = (ROOT / name).read_text(encoding="utf-8") if (ROOT / name).exists() else ""
    for image in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", body):
        if image.startswith(("http://", "https://")):
            continue          # shields.io badges
        if not (ROOT / name).parent.joinpath(image).exists():
            problems.append(f"{name}: image {image} missing")

if problems:
    print("\n".join(f"  {p}" for p in problems))
    sys.exit(1)
print("  docs clean: every link, image, anchor, fence and mermaid node resolves")
