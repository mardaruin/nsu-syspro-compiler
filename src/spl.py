from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lexer import Lexer
from lexer.tokens import KEYWORDS, Token, TokenType
from spl_parser import Parser, node_to_dict
from spl_parser.ast_nodes import Error as AstError

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

def _run_parser(src: str, output_path: str | None) -> int:
    tokens = Lexer(src).tokenize()

    if any(t.type == TokenType.ERROR for t in tokens):
        if output_path:
            _emit({"kind": "LexerError"}, output_path)
        return 1

    parser = Parser(tokens)
    ast = parser.parse()
    _emit(node_to_dict(ast), output_path)

    if _has_ast_errors(ast):
        return 1
    if parser.errors:
        for e in parser.errors:
            print(f"{e.line}:{e.column}: {e.message}", file=sys.stderr)
        return 2
    return 0

def _has_ast_errors(node) -> bool:
    if node is None:
        return False
    if isinstance(node, AstError):
        return True
    if isinstance(node, list):
        return any(_has_ast_errors(x) for x in node)
    if hasattr(node, "__dataclass_fields__"):
        for field_name in node.__dataclass_fields__:
            if _has_ast_errors(getattr(node, field_name)):
                return True
    return False

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="splc")
    p.add_argument("--stage", choices=["lexer", "parser", "ir"], required=True)
    p.add_argument("input")
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    src = _read_source(args.input)

    if args.stage == "lexer":
        return _run_lexer(src, args.output)
    if args.stage == "parser":
        return _run_parser(src, args.output)

    print(f"stage {args.stage!r} not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
