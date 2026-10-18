#!/usr/bin/env python3
"""Check test case compatibility against a compiler binary.

Runs each test case through the compiler for every grammar version and
prints a pretty matrix showing which tests pass/fail at each stage.

Usage:
    python3 test/check-compat.py                        # uses build/splc
    python3 test/check-compat.py --compiler path/to/splc
    python3 test/check-compat.py --compiler path/to/splc --stage lexer
    python3 test/check-compat.py --compiler path/to/splc --test dangling
    python3 test/check-compat.py --compiler path/to/splc --grammar 3
    python3 test/check-compat.py --compiler path/to/splc --stage llvm -v
"""

import argparse
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_COMPILER = os.path.join(REPO_ROOT, "build", "splc")

# Stages that the compiler can be tested against
# Must match the stage names used by the test harness (run_tests.py)
# and referenced in meta.json exit/stages fields.
STAGES = ["lexer", "spl_parser", "llvm"]

# Grammar versions to test
GRAMMARS = ["1", "2", "3", "4", "5"]


def load_meta(path):
    """Load meta.json from a test directory."""
    meta_path = os.path.join(path, "meta.json")
    if not os.path.exists(meta_path):
        return {}
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def resolve_grammar_constraint(meta):
    """Resolve the grammar constraint from meta.json.

    Returns a list of applicable grammar versions (as strings), or None if
    the constraint is not parseable (meaning all versions are applicable).
    """
    g = meta.get("grammar")
    if g is None:
        return None  # no constraint -> all versions
    if isinstance(g, (int, str)):
        return [str(g)] if isinstance(g, int) else [g]
    if isinstance(g, list):
        return [str(v) for v in g]
    if isinstance(g, dict):
        # Per-stage constraints: collect all referenced versions
        all_versions = set()
        for v in g.values():
            if isinstance(v, (int, str)):
                all_versions.add(str(v))
            elif isinstance(v, list):
                all_versions.update(str(x) for x in v)
        return list(all_versions) if all_versions else None
    return None


def grammar_matches_constraint(constraint_str, target):
    """Check if a single constraint string matches a target version."""
    s = str(constraint_str).strip()
    target_str = str(target)

    # Handle >=N, >N, <=N, <N, ==N
    op = None
    for prefix in ("==", ">=", "<=", ">", "<"):
        if s.startswith(prefix):
            op = prefix
            s = s[len(prefix):].strip()
            break
    if op is None:
        op = "=="

    try:
        target_v = int(target_str)
        constraint_v = int(s)
    except ValueError:
        return target_str == s

    if op == "==":
        return target_v == constraint_v
    elif op == ">=":
        return target_v >= constraint_v
    elif op == "<=":
        return target_v <= constraint_v
    elif op == ">":
        return target_v > constraint_v
    elif op == "<":
        return target_v < constraint_v
    return False


def test_is_applicable(meta, grammar, stage):
    """Check if a test case is applicable for a given grammar and stage."""
    g = meta.get("grammar")
    if g is None:
        return True
    if isinstance(g, dict):
        stage_constraint = g.get(stage)
        if stage_constraint is None:
            return True
        if isinstance(stage_constraint, list):
            return grammar in stage_constraint
        return grammar_matches_constraint(stage_constraint, grammar)
    if isinstance(g, list):
        return grammar in g
    return grammar_matches_constraint(g, grammar)


def get_exit_contract(meta, stage):
    """Get the expected exit code for a stage."""
    e = meta.get("exit", 0)
    if isinstance(e, dict):
        return e.get(stage, 0)
    return e


def run_compiler(compiler, grammar, stage, test_spl):
    """Run the compiler for a given stage.

    Returns (exit_code, stdout, stderr) or (None, None, None) on OSError.
    """
    if stage == "lexer":
        cmd = [compiler, "-g", grammar, "-t", "/dev/stdout", test_spl]
    elif stage == "spl_parser":
        cmd = [compiler, "-g", grammar, "-a", "/dev/stdout", test_spl]
    elif stage == "llvm":
        cmd = [compiler, "-g", grammar, "-o", "/dev/null", test_spl]
    else:
        return None, None, None

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except OSError as e:
        return None, None, f"failed to start: {e}"
    except subprocess.TimeoutExpired:
        return None, None, "timed out"

    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def test_supported_stages(dirpath, meta):
    """Determine which stages a test case supports.

    Returns a set of stage names matching the harness conventions.
    """
    filenames = os.listdir(dirpath)
    found_stages = set()

    # Stages with golden files
    for f in filenames:
        if f == "tokens.json":
            found_stages.add("lexer")
        elif f == "ast.json":
            found_stages.add("spl_parser")
        elif f in ("out.ll", "out.bc"):
            found_stages.add("llvm")

    # Explicit stages in meta
    for stage in meta.get("stages", []):
        found_stages.add(stage)

    # Exit-only negative tests without golden files
    if not found_stages:
        e = meta.get("exit", 0)
        if isinstance(e, dict):
            for stage in STAGES:
                if e.get(stage) == "nonzero" or (isinstance(e.get(stage), int) and e.get(stage) != 0):
                    found_stages.add(stage)
        elif e == "nonzero" or (isinstance(e, int) and e != 0):
            found_stages = set(STAGES)

    return found_stages


def main():
    parser = argparse.ArgumentParser(
        description="Check test case compatibility against a compiler binary.")
    parser.add_argument("--compiler", default=DEFAULT_COMPILER,
                        help=f"compiler binary path (default: {DEFAULT_COMPILER})")
    parser.add_argument("--stage", default=None,
                        choices=STAGES,
                        help="only test this stage")
    parser.add_argument("--test", default=None,
                        help="only test cases whose path contains this string")
    parser.add_argument("--grammar", default=None,
                        help="only test this grammar version")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="show error details for failures")
    args = parser.parse_args()

    compiler = os.path.abspath(args.compiler)
    if not os.path.exists(compiler):
        print(f"error: compiler not found: {compiler}", file=sys.stderr)
        return 1

    # Determine which grammars to test
    grammars = [args.grammar] if args.grammar else GRAMMARS
    stages = [args.stage] if args.stage else STAGES

    # Discover test cases
    test_dirs = []
    for dirpath, dirnames, filenames in os.walk(SCRIPT_DIR):
        # Skip .self-test and build directories
        skip = False
        for skip_part in [".self-test", "build", ".git", "__pycache__"]:
            if skip_part in dirpath:
                skip = True
                break
        if skip:
            continue
        if dirpath == SCRIPT_DIR:
            continue
        if "meta.json" not in filenames or "test.spl" not in filenames:
            continue
        rel_path = os.path.relpath(dirpath, SCRIPT_DIR)
        if args.test and args.test not in rel_path:
            continue
        test_dirs.append((dirpath, rel_path))

    if not test_dirs:
        print("no test cases found")
        return 0

    test_dirs.sort(key=lambda x: x[1])

    no_color = not sys.stdout.isatty()

    def G(s):
        return s if no_color else "\033[32m" + s + "\033[0m"

    def R(s):
        return s if no_color else "\033[31m" + s + "\033[0m"

    def Y(s):
        return s if no_color else "\033[33m" + s + "\033[0m"

    # Run tests
    print(f"Compiler: {compiler}")
    print()

    for stage in stages:
        print(f"=== Stage: {stage} ===")
        # Header
        header = f"{'Test case':<50}"
        for g in grammars:
            header += f"  G{g:>3}"
        print(header)
        print("-" * len(header))

        for dirpath, rel_path in test_dirs:
            meta = load_meta(dirpath)
            test_spl = os.path.join(dirpath, "test.spl")

            supported = test_supported_stages(dirpath, meta)
            if stage not in supported:
                # Test case doesn't support this stage — skip entirely
                row = f"{rel_path:<50}"
                for g in grammars:
                    row += f"  {Y('—'):>4}"
                print(row)
                continue

            cells = []
            failures = []

            for g in grammars:
                if not test_is_applicable(meta, g, stage):
                    cells.append(("-", Y('—')))
                    continue

                rc, stdout, stderr = run_compiler(compiler, g, stage, test_spl)
                exit_spec = get_exit_contract(meta, stage)

                if rc is None:
                    cells.append(("ERR", R('ERR')))
                    failures.append((g, stderr or stdout or "failed to start"))
                    continue

                # Check if exit code matches expectation
                if exit_spec == "nonzero":
                    ok = rc != 0
                else:
                    expected = int(exit_spec) if isinstance(exit_spec, (int, str)) and str(exit_spec).lstrip("-").isdigit() else 0
                    ok = rc == expected

                if ok:
                    cells.append(("ok", G('✓')))
                else:
                    cells.append(("FAIL", R('✗')))
                    failures.append((g, stderr or stdout or f"exit {rc} != expected {exit_spec}"))

            row = f"{rel_path:<50}"
            for _, mark in cells:
                row += f"  {mark:>4}"
            print(row)

            if args.verbose:
                for g, detail in failures:
                    detail_lines = detail.strip().splitlines()[:3]
                    print(f"    G{g}: {' | '.join(detail_lines)}")

        print()

    # Summary
    print("Legend: ✓=pass, ✗=fail, —=not applicable (constrained out)")
    print(f"Stages tested: {', '.join(stages)}")
    print(f"Grammars tested: {', '.join(grammars)}")


if __name__ == "__main__":
    sys.exit(main())