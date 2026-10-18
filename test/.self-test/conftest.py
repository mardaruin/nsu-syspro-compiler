"""Fixtures for run_tests.py self-tests.

Test cases are created as directories with meta.json + test.spl + golden files
(e.g., tokens.json, ast.json, out.ll, stdout).  The test name is the directory
name itself (or a relative path under root for hierarchical names).  Discovery
walks recursively for directories containing meta.json.
"""

import json
import os
import shutil
import sys

import pytest

# Import the real harness as a module (its `main` only runs under __main__).
TEST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TEST_DIR)
import run_tests  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK_COMPILER = os.path.join(HERE, "mock_compiler.py")

# A stub fuzzer so fuzz orchestration can be tested without Grammarinator.
# Mimics test/fuzz/generate.py: writes N non-empty .spl files and, with --quiet,
# prints each produced path on stdout.
STUB_FUZZER = """\
import os, sys
out = sys.argv[sys.argv.index("--out") + 1]
n = int(sys.argv[sys.argv.index("-n") + 1])
os.makedirs(out, exist_ok=True)
for i in range(n):
    p = os.path.join(out, "test_%d.spl" % i)
    with open(p, "w") as f:
        f.write("0;\\n")
    print(p)
"""

# Default golden token stream used by add_lexer
TOKENS = [
    {"kind": "IDENT", "value": "x", "line": 1, "column": 1},
    {"kind": "SEMICOLON", "value": ";", "line": 1, "column": 2},
]
RAW_TOKENS = json.dumps(TOKENS, indent=2)


class Harness:
    """A temporary harness workspace pointing run_tests at mock fixtures.

    Test case layout (new format):
      {root}/{test_name}/
        meta.json        (always created)
        test.spl         (the source)
        tokens.json      (lexer golden)
        ast.json         (spl_parser golden)
        out.ll           (llvm golden)
        stdout           (compiler golden)
    """

    def __init__(self, root):
        self.root = root
        self.out_dir = root / "out"
        shutil.copy(MOCK_COMPILER, root / "mock_compiler.py")
        os.chmod(root / "mock_compiler.py", 0o755)
        (root / "fuzz").mkdir()
        (root / "fuzz" / "generate.py").write_text(STUB_FUZZER)

    def write_config(self, stages=None, build=None, fuzz=None, out_dir="{root}/out",
                       default_grammar="1"):
        cfg = {"out_dir": out_dir, "stages": stages or {},
               "default_grammar": default_grammar}
        if build is not None:
            cfg["build"] = build
        if fuzz is not None:
            cfg["fuzz"] = fuzz
        (self.root / "config.json").write_text(json.dumps(cfg))
        return str(self.root / "config.json")

    def add_test(self, name, src="", tokens=None, ast=None, out_ll=None,
                  stdout=None, meta=None):
        """Create a test case directory at {root}/{name}/.

        Golden files keys match STAGE_GOLDEN_DEFAULT: 'tokens' -> tokens.json,
        'ast' -> ast.json, 'out_ll' -> out.ll, 'stdout' -> stdout.
        """
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "test.spl").write_text(src)
        meta = meta or {}
        (d / "meta.json").write_text(json.dumps(meta))
        if tokens is not None:
            (d / "tokens.json").write_text(tokens)
        if ast is not None:
            (d / "ast.json").write_text(ast)
        if out_ll is not None:
            (d / "out.ll").write_text(out_ll)
        if stdout is not None:
            (d / "stdout").write_text(stdout)

    def write(self, name, ext, content):
        """Write an arbitrary fixture file in a test case directory.

        Example: harness.write("hw", "stdin", "input\\n")
        """
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ext).write_text(content)


def add_lexer(harness, name, src="0;", meta=None, tokens=RAW_TOKENS):
    """Convenience: add a lexer test (has tokens.json golden)."""
    harness.add_test(name, src=src, tokens=tokens, meta=meta)


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    orig_script = run_tests.SCRIPT_DIR
    orig_root = run_tests.REPO_ROOT
    # Point discovery root and {root} placeholder resolution at the fixture tree.
    run_tests.SCRIPT_DIR = str(h.root)
    run_tests.REPO_ROOT = str(h.root)
    try:
        yield h
    finally:
        run_tests.SCRIPT_DIR = orig_script
        run_tests.REPO_ROOT = orig_root


@pytest.fixture
def run(capsys):
    def _run(argv):
        rc = run_tests.main(list(argv))
        captured = capsys.readouterr()
        return rc, captured.out + captured.err
    return _run