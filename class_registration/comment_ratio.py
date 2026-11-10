#!/usr/bin/env python3
"""
comment_ratio.py - report how much of this project is commentary.

The brief asks for at least 30% comments in the source, so this small tool
measures it instead of guessing.  A line counts as commentary when it is:

    * a `#` comment line, or
    * a line inside a docstring / module description.

Blank lines and pure code lines are not counted, and the percentage is
"commentary lines / all non-blank lines".

    python3 comment_ratio.py
"""

from __future__ import annotations

import io
import os
import tokenize

# The files that make up the deliverable (this tool does not measure itself).
FILES = ("server.py", "client_gui.py", "server_gui.py", "theme.py",
         "test_registration.py")

# A line that starts a logical line of code: a string token that follows one of
# these starts a docstring rather than a value in an expression.
DOC_STARTERS = (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                tokenize.DEDENT, tokenize.ENCODING)


def measure(path: str) -> tuple[int, int]:
    """Return (non-blank lines, commentary lines) for one file."""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    lines = source.splitlines()

    # Every line that contains something counts towards the total.
    non_blank = [number for number, text in enumerate(lines, 1) if text.strip()]

    comments: set[int] = set()      # line numbers holding a `#` comment
    docstrings: set[int] = set()    # line numbers inside a docstring
    previous = tokenize.NEWLINE     # type of the previous significant token

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.add(token.start[0])
        elif token.type == tokenize.STRING and previous in DOC_STARTERS:
            # A string at the start of a statement is a docstring: count every
            # line it spans, so multi-line descriptions are measured fairly.
            docstrings.update(range(token.start[0], token.end[0] + 1))
        if token.type not in (tokenize.NL, tokenize.COMMENT):
            previous = token.type

    marked = {n for n in non_blank if n in comments or n in docstrings}
    return len(non_blank), len(marked)


def main() -> None:
    """Print one row per file and a project total."""
    here = os.path.dirname(os.path.abspath(__file__))
    print(f"{'file':24}{'lines':>8}{'comments':>10}{'ratio':>9}")
    print("-" * 51)
    total_lines = total_comments = 0
    for name in FILES:
        lines, comments = measure(os.path.join(here, name))
        total_lines += lines
        total_comments += comments
        print(f"{name:24}{lines:>8}{comments:>10}{comments / lines * 100:>8.1f}%")
    print("-" * 51)
    ratio = total_comments / total_lines * 100
    print(f"{'TOTAL':24}{total_lines:>8}{total_comments:>10}{ratio:>8.1f}%")
    print()
    print("Requirement: at least 30%  ->  "
          + ("PASS" if ratio >= 30 else "NOT MET"))


if __name__ == "__main__":
    main()
