from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import re
import subprocess
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lexer import Lexer
from lexer.tokens import KEYWORDS, Token, TokenType
from spl_parser import Parser, node_to_dict
from spl_parser.ast_nodes import Error as AstError
from ir.codegen import LLVMCodeGen
from ir.errors import CodegenError

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

def _run_ir(src: str, output_path: str | None, llvm_out: str | None) -> int:
    tokens = Lexer(src).tokenize()
    if any(t.type == TokenType.ERROR for t in tokens):
        if output_path:
            _emit({"kind": "LexerError"}, output_path)
        return 1

    parser = Parser(tokens)
    ast = parser.parse()
    _emit(node_to_dict(ast), output_path)

    try:
        codegen = LLVMCodeGen()
        module = codegen.generate(ast)
    except CodegenError as e:
        print(f"codegen: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"codegen crashed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    ll_path = Path(llvm_out) if llvm_out else (
        Path(output_path).with_suffix(".ll") if output_path else Path("out.ll")
    )
    ll_path.parent.mkdir(parents=True, exist_ok=True)

    ir_text = str(module)

    # 1. ModuleID: "spl" → 'spl'
    ir_text = ir_text.replace('; ModuleID = "spl"', "; ModuleID = 'spl'")

    # 2. @"main" → @main
    ir_text = re.sub(r'@"([^"]+)"', r'@\1', ir_text)

    # 3. %"x" → %x
    ir_text = re.sub(r'%"([^"]+)"', r'%\1', ir_text)

    # 4. i64* → ptr  (typed pointers → opaque)
    ir_text = re.sub(r'\bi64\*+', 'ptr', ir_text)

    # 5. define i64 @main()\n{ → define i64 @main() {
    ir_text = re.sub(r'(define [^\n]+\))\n\{', r'\1 {', ir_text)

    ll_path.write_text(ir_text, encoding="utf-8")

    #ll_path.write_text(str(module), encoding="utf-8")

    if _has_ast_errors(ast):
        return 1
    if parser.errors:
        for e in parser.errors:
            print(f"{e.line}:{e.column}: {e.message}", file=sys.stderr)
        return 2
    return 0

def _run_compile(src: str, exe_path: str, llvm_out: str | None) -> int:
    ll_path = llvm_out or (exe_path + ".ll")
    rc = _run_ir(src, None, ll_path)
    if rc != 0:
        return rc

    clang = shutil.which("clang")
    if not clang:
        print("Error: clang not found in PATH. Cannot compile to executable.", file=sys.stderr)
        return 3

    cmd = [clang, ll_path, "-o", exe_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"clang failed: {proc.stderr}", file=sys.stderr)
            return proc.returncode
    except Exception as e:
        print(f"Failed to run clang: {e}", file=sys.stderr)
        return 3

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
    p.add_argument("--stage", choices=["lexer", "parser", "llvm", "compile"], required=True)
    p.add_argument("input")
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--llvm-out", default=None,
                   help="path to write generated LLVM IR (default: alongside --output)")
    args = p.parse_args(argv)

    src = _read_source(args.input)

    if args.stage == "lexer":
        return _run_lexer(src, args.output)
    if args.stage == "parser":
        return _run_parser(src, args.output)
    if args.stage == "llvm":
        return _run_ir(src, args.output, args.llvm_out)
    if args.stage == "compile":
        if not args.output:
            print("Error: -o/--output required for compile stage (exe path)", file=sys.stderr)
            return 2
        return _run_compile(src, args.output, args.llvm_out)

    print(f"stage {args.stage!r} not implemented yet", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
