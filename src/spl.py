from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lexer import Lexer, LexerError
from lexer.tokens import KEYWORDS, Token, TokenType

def _read_source(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()

def _emit(data, output_path: str | None) -> None:
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.write("\n")
    else:
        json.dump(data, sys.stdout)
        sys.stdout.write("\n")

def _run_lexer(src: str, output_path: str | None) -> int:
    tokens = Lexer(src).tokenize()
    _emit([t.to_json() for t in tokens], output_path)
    if any(t.type == TokenType.ERROR for t in tokens):
        return 1
    return 0

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="splc")
    p.add_argument("--stage", choices=["lexer", "parser", "ir"], required=True)
    p.add_argument("input")
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    src = _read_source(args.input)

    if args.stage == "lexer":
        return _run_lexer(src, args.output)

    print(f"stage {args.stage!r} not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
