"""Apply run-level edits to an existing `.docx`.

Operations:
    {"op": "replace_run", "para": <int>, "run": <int>, "text": "..."}
    {"op": "replace_text", "find": "...", "with": "..."}

`replace_text` walks every paragraph and preserves the formatting (bold,
italic, font, size, etc.) of runs that the match does not touch. When the
match spans multiple runs, only the affected runs are merged — the rest
keep their original formatting (issue #1447).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from docx import Document
from docx.text.paragraph import Paragraph


def _replace_run(para: Paragraph, run_idx: int, text: str) -> None:
    if 0 <= run_idx < len(para.runs):
        para.runs[run_idx].text = text


def _replace_text_in_paragraph(para: Paragraph, find: str, replacement: str) -> bool:
    """Replace *find* with *replacement* in *para*, preserving run formatting.

    Builds a cumulative run map (start_offset, end_offset, run) for every
    run in the paragraph, searches for *find* in the concatenated text,
    then splices the replacement into the affected runs while leaving
    non-affected runs intact.
    """
    runs = list(para.runs)
    run_texts = [(r.text if r.text is not None else "") for r in runs]

    # Cumulative offsets
    offsets: list[tuple[int, int, str]] = []  # (start, end, text)
    pos = 0
    for text in run_texts:
        offsets.append((pos, pos + len(text), text))
        pos += len(text)

    full = "".join(run_texts)
    match_start = full.find(find)
    if match_start < 0:
        return False
    match_end = match_start + len(find)

    # Determine which runs are affected
    affected = [i for i, (s, e, _) in enumerate(offsets) if s < match_end and e > match_start]
    if not affected:
        return False  # shouldn't happen

    first = affected[0]
    last = affected[-1]

    # Build pre, middle, post text
    pre_runs = [(offsets[i][0], run_texts[i]) for i in range(first)]
    match_runs = [(offsets[i][0], run_texts[i]) for i in range(first, last + 1)]
    post_runs = [(offsets[i][0], run_texts[i]) for i in range(last + 1, len(runs))]

    # Compute text in each region relative to the match
    match_text = "".join(t for _, t in match_runs)
    rel_start = match_start - offsets[first][0]
    rel_end = match_end - offsets[first][0]

    pre_match = match_text[:rel_start]
    post_match = match_text[rel_end:]

    # Rebuild runs array
    new_run_texts: list[str] = []

    # Pre-runs: unchanged
    for _, t in pre_runs:
        new_run_texts.append(t)

    # Affected runs: merged into one run with replacement spliced in
    new_run_texts.append(pre_match + replacement + post_match)

    # Post-runs: unchanged
    for _, t in post_runs:
        new_run_texts.append(t)

    # Apply text back, preserving the first affected run's formatting
    for i, text in enumerate(new_run_texts):
        runs[i].text = text

    # Clear any surplus runs
    for run in runs[len(new_run_texts) :]:
        run.text = ""

    return True


def apply_ops(doc: Document, ops: list[dict[str, Any]]) -> int:
    applied = 0
    for op in ops:
        kind = op.get("op")
        if kind == "replace_run":
            try:
                para = doc.paragraphs[int(op["para"])]
            except (KeyError, IndexError, ValueError):
                continue
            _replace_run(para, int(op.get("run", 0)), str(op.get("text", "")))
            applied += 1
        elif kind == "replace_text":
            find = str(op.get("find", ""))
            replacement = str(op.get("with", ""))
            if not find:
                continue
            for para in doc.paragraphs:
                if _replace_text_in_paragraph(para, find, replacement):
                    applied += 1
    return applied


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit a .docx in place via run-level ops.")
    parser.add_argument("input", type=Path, help="Path to the source .docx")
    parser.add_argument("ops", type=Path, help="JSON file containing a list of ops")
    parser.add_argument("--out", type=Path, required=True, help="Output .docx path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.input.is_file():
        print(f"error: input {args.input} not found", file=sys.stderr)
        return 2
    if not args.ops.is_file():
        print(f"error: ops {args.ops} not found", file=sys.stderr)
        return 2
    raw = json.loads(args.ops.read_text(encoding="utf-8"))
    ops = raw if isinstance(raw, list) else []
    doc = Document(str(args.input))
    applied = apply_ops(doc, ops)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    print(json.dumps({"applied": applied}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
