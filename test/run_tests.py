#!/usr/bin/env python3
"""Test runner for the compiler sandbox.

Test cases live in subdirectories of any depth under `test/`. Each test case
is a directory containing:

  meta.json               test metadata (grammar versions, expected exit, timeout,
                           and optionally which stages to run)
                           grammar and exit can be flat (applies to all stages)
                           or a dict mapping stage names to values
  test.spl                source file
  tokens.json             golden token stream     (lexer stage)
  ast.json                golden AST              (spl_parser stage)
  out.ll / out.bc         golden LLVM IR          (llvm stage)
  stdout                  golden program output   (run stage)
  stdin                   input for the program   (run stage)

A test case is discovered by the presence of meta.json. Which stages are run
for a test case depends on which golden files exist. A test case can therefore
be shared across multiple stages without duplication.

How each stage is invoked is described by the driver config (test/config.json by
default).

Subcommands:
  build      Run the build command from the config.
  test       Build (unless --no-build) and run the selected tests. [default]
  update     Build, then regenerate golden files from actual output.
  list       Print the tests that would run, without running them.

Golden comparison is text-only: both golden and actual are passed through the
stage's `preprocess` pipeline (config-driven) and then diffed.
"""

import argparse
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config.json")
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "build")

STAGE_GOLDEN_DEFAULT = {
    "lexer": "tokens.json",
    "spl_parser": "ast.json",
    "llvm": "out.ll",
    "run": "stdout",
}

# Which golden files map to which stages (reverse of STAGE_GOLDEN_DEFAULT)
GOLDEN_TO_STAGE = {v: k for k, v in STAGE_GOLDEN_DEFAULT.items()}
# Also support .bc as an alternative for llvm
GOLDEN_TO_STAGE["out.bc"] = "llvm"
# Old meta.json used 'compiler' stage with stdout golden; map it to 'run'
# so that existing stdout files continue to be discovered as 'run' stage
GOLDEN_TO_STAGE["stdout"] = "run"

DEFAULT_TIMEOUT = 30.0
DIFF_MAX_LINES = 30

# Canonical stage order for discovery sorting and output grouping
STAGE_ORDER = ["lexer", "spl_parser", "llvm", "compiler", "run"]


class StepError(Exception):
    pass


class SkipError(Exception):
    pass


class Config:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise StepError(f"config {path}: expected a JSON object")
        self.stages = raw.get("stages", {})
        self.build_cmd = raw.get("build")
        self.default_grammar = str(raw.get("default_grammar", "")).strip() or None
        raw_versions = raw.get("versions")
        if raw_versions is not None:
            if not isinstance(raw_versions, list) or not all(isinstance(v, int) for v in raw_versions):
                raise StepError(f"config {path}: 'versions' must be a list of integers")
            self.versions = [str(v) for v in raw_versions]
        self.out_dir = os.path.abspath(resolve(
            str(raw.get("out_dir", DEFAULT_OUT_DIR)), {"{root}": REPO_ROOT}))
        llvm_dis = raw.get("llvm_dis", "llvm-dis")
        self.llvm_dis = [llvm_dis] if isinstance(llvm_dis, str) else list(llvm_dis)
        raw_fuzz = raw.get("fuzz") or {}
        if not isinstance(raw_fuzz, dict):
            raise StepError(f"config {path}: 'fuzz' must be an object")
        grammar_map = raw_fuzz.get("grammar") or {}
        if not isinstance(grammar_map, dict):
            raise StepError(f"config {path}: 'fuzz.grammar' must be an object")
        # Fall back to fuzz grammar keys if versions not specified
        if not hasattr(self, 'versions'):
            self.versions = sorted(grammar_map.keys(), key=int)
        exit_map = raw_fuzz.get("exit") or {}
        if not isinstance(exit_map, dict):
            raise StepError(f"config {path}: 'fuzz.exit' must be an object")
        stages = raw_fuzz.get("stages")
        if stages is not None and not isinstance(stages, list):
            raise StepError(f"config {path}: 'fuzz.stages' must be a list")
        self.fuzz = {
            "grammar": {str(k): str(v) for k, v in grammar_map.items()},
            "count": int(raw_fuzz.get("count", 40)),
            "max_tokens": int(raw_fuzz.get("max_tokens", 30)),
            "max_depth": raw_fuzz.get("max_depth"),  # optional; None => unlimited
            "exit": {str(k): v for k, v in exit_map.items()},
            "stages": stages,  # None => all configured stages
        }

    def stage_cfg(self, name):
        cfg = self.stages.get(name) or {}
        if not isinstance(cfg, dict):
            raise StepError(f"config {self.path}: stage '{name}' must be an object")
        return cfg


class Test:
    def __init__(self, stage, name, src_path, meta=None, meta_error=None):
        self.stage = stage
        self.name = name
        self.src_path = src_path
        self.dir = os.path.dirname(src_path)
        if meta is not None:
            self.meta = meta
            self.meta_error = meta_error
        else:
            self.meta, self.meta_error = load_meta(
                os.path.join(self.dir, "meta.json"))
        self.is_fuzz = False


def load_meta(path):
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (ValueError, OSError) as e:
        return {}, f"invalid meta {os.path.basename(path)}: {e}"
    if not isinstance(meta, dict):
        return {}, f"invalid meta {os.path.basename(path)}: must be a JSON object"
    return meta, None


# ---------------------------------------------------------------------------
# Placeholders and process running
# ---------------------------------------------------------------------------

def resolve(text, placeholders):
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def run(cmd, cwd=None, stdin=None, timeout=DEFAULT_TIMEOUT):
    # subprocess.run takes bytes to pipe in via `input=`, not `stdin=`; a bytes
    # stdin (as produced by a compiler stage reading a .stdin file) must use `input`.
    stdin_bytes = None
    stdin_arg = stdin
    if isinstance(stdin, (bytes, bytearray)):
        stdin_bytes = bytes(stdin)
        stdin_arg = None
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=stdin_arg,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except OSError as e:
        raise StepError(f"failed to run {' '.join(cmd[:1])!r}: {e}")
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr


def shell_build(cmd, cwd=None, timeout=None):
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except OSError as e:
        raise StepError(f"build failed to start: {e}")
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr


def read_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_file(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Grammar version matching
# ---------------------------------------------------------------------------

def parse_version(s):
    s = s.strip()
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return None


def cmp_versions(a, b):
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def grammar_matches(constraint, target):
    """True if `target` grammar version satisfies `constraint`.

    constraint may be a list of version ids (set membership) or a string
    expression such as "2", "==2", ">=2", ">2", "<=2", "<2", possibly with
    minor/major parts ("2.1"). A missing constraint applies to every version.
    """
    if constraint is None:
        return True
    if isinstance(constraint, list):
        return target in constraint
    s = str(constraint).strip()
    op = None
    for prefix in ("==", ">=", "<=", ">", "<"):
        if s.startswith(prefix):
            op = prefix
            s = s[len(prefix):].strip()
            break
    if op is None:
        op = "=="
    val = s
    target_v, val_v = parse_version(target), parse_version(val)
    if target_v is not None and val_v is not None:
        c = cmp_versions(target_v, val_v)
        cmp_map = {"==": c == 0, "<": c < 0, "<=": c <= 0,
                   ">": c > 0, ">=": c >= 0}
        return cmp_map[op]
    if op == "==":
        return target == val
    return False


def meta_excluded(meta):
    """Check if a test case is excluded via meta.json.

    Supports:
      "exclude": true   -> exclude this test case
      "exclude": false   -> test runs normally (same as absent)
    """
    return bool(meta.get("exclude", False))


def meta_selected(meta, grammar):
    """Check if a test case is applicable for the given grammar version.

    If meta["grammar"] is a dict, it maps stage names to constraints.
    The test is selected if any stage matches (for early filtering).
    If it's a flat value (string/list), it applies to all stages.
    """
    if grammar is None:
        return True
    g = meta.get("grammar")
    if isinstance(g, dict):
        return any(grammar_matches(v, grammar) for v in g.values())
    return grammar_matches(g, grammar)


def stage_selected(meta, stage, grammar):
    """Check if a specific stage of a test is applicable for the grammar.

    If meta["grammar"] is a dict, use the per-stage constraint;
    otherwise the flat constraint was already checked in meta_selected().
    """
    if grammar is None:
        return True
    g = meta.get("grammar")
    if isinstance(g, dict):
        return grammar_matches(g.get(stage), grammar)
    return True


# ---------------------------------------------------------------------------
# Test discovery
# ---------------------------------------------------------------------------

def discover_tests(test_root, grammar, stage_filter, name_filter, skip_dir=None,
                   check_ir=False):
    """Discover test cases by finding directories with meta.json recursively.

    Each directory containing meta.json (and test.spl) is a test case.
    For each such directory, one Test is created per applicable stage,
    based on which golden files (tokens.json, ast.json, out.ll, stdout)
    exist in that directory, or based on explicit "stages" in meta.json
    for exit-only tests.

    When check_ir is False (default), the "llvm" stage (comparing LLVM IR
    against out.ll goldens) is skipped. Pass --check-ir on the command line
    to enable it.
    """
    tests = []
    excluded = []
    abs_skip = os.path.abspath(skip_dir) if skip_dir else None

    for dirpath, dirnames, filenames in os.walk(test_root):
        if dirpath == test_root:
            continue
        if abs_skip and os.path.abspath(dirpath) == abs_skip:
            dirnames.clear()
            continue

        if "meta.json" not in filenames or "test.spl" not in filenames:
            continue

        rel_path = os.path.relpath(dirpath, test_root)
        meta, meta_error = load_meta(os.path.join(dirpath, "meta.json"))

        if not meta_selected(meta, grammar):
            continue

        if meta_excluded(meta):
            excluded.append(rel_path)
            continue

        # Determine applicable stages for this test case
        found_stages = set()

        # Stages with golden files
        for golden_file in filenames:
            stage = GOLDEN_TO_STAGE.get(golden_file)
            if stage is not None:
                found_stages.add(stage)

        # Exit-only tests without golden: explicitly listed in meta["stages"]
        for stage in meta.get("stages", []):
            found_stages.add(stage)

        # Backward compat: if meta.json lists "compiler" in stages but not
        # "run", auto-add "run" (the old "compiler" stage covered both
        # compilation and execution; new scheme splits them).
        meta_stages = meta.get("stages", [])
        if "compiler" in meta_stages and "run" not in meta_stages:
            found_stages.add("run")

        # If the test has an explicit non-zero exit contract but no golden
        # and no stages list, run for all configurable stages (user can filter
        # with --stage). Only do this if no golden files were found either.
        if not found_stages:
            spec = False
            for stage in STAGE_GOLDEN_DEFAULT:
                s = exit_spec(meta, stage=stage)
                if s == "nonzero" or (isinstance(s, int) and s != 0):
                    spec = s
                    break
            if spec:
                found_stages = set(STAGE_GOLDEN_DEFAULT.keys())

        if not found_stages:
            print(f"warning: {rel_path} has meta.json/test.spl but no stages — "
                  "add golden files (tokens.json, ast.json, out.ll, stdout), "
                  "a 'stages' list, or a non-zero 'exit' in meta.json",
                  file=sys.stderr)
            continue

        for stage in sorted(found_stages):
            if not stage_selected(meta, stage, grammar):
                continue
            if stage_filter and stage != stage_filter:
                continue
            if name_filter and name_filter not in f"{stage}/{rel_path}":
                continue

            test = Test(stage, rel_path, os.path.join(dirpath, "test.spl"),
                        meta=meta, meta_error=meta_error)
            tests.append(test)

    tests.sort(key=lambda t: (STAGE_ORDER.index(t.stage) if t.stage in STAGE_ORDER else len(STAGE_ORDER), t.name))
    return tests, excluded


# ---------------------------------------------------------------------------
# Fuzz test generation (delegated to test/fuzz/generate.py)
# ---------------------------------------------------------------------------

def resolve_fuzz_grammar(config, version):
    """Map a grammar version to its grammar file for fuzzing."""
    mapped = config.fuzz["grammar"].get(version)
    if mapped:
        return os.path.join(REPO_ROOT, mapped)
    fallback = os.path.join(REPO_ROOT, "doc", "grammar%s.g4" % version)
    if os.path.exists(fallback):
        return fallback
    raise StepError(
        "no grammar file for version '%s'; add it to 'fuzz.grammar' in %s "
        "or create doc/grammar%s.g4" % (version, config.path, version))


def select_fuzz_versions(config, grammar):
    """Choose which grammar version(s) to fuzz, driven by --grammar."""
    if grammar is not None:
        return [str(grammar).split(".")[0].strip()]
    if len(config.fuzz["grammar"]) == 1:
        return [next(iter(config.fuzz["grammar"]))]
    if not config.fuzz["grammar"]:
        raise StepError("fuzz: no grammar versions configured in 'fuzz.grammar'")
    raise StepError(
        "fuzz: multiple grammar versions configured; select one with --grammar")


def generate_fuzz_tests(config, version, count, seed, max_tokens, max_depth,
                        out_dir, stage_filter, name_filter):
    """Run the fuzzer and wrap each produced .spl as a fuzz Test."""
    grammar_file = resolve_fuzz_grammar(config, version)
    fuzz_dir = os.path.join(out_dir, "fuzz", version)
    build_dir = os.path.join(out_dir, "fuzz", "grammarinator")
    cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, "fuzz", "generate.py"),
        "-n", str(count),
        "--out", fuzz_dir,
        "--build", build_dir,
        "--grammar", grammar_file,
        "--max-tokens", str(max_tokens),
        "--quiet",
    ]
    if max_depth is not None:
        cmd += ["--max-depth", str(max_depth)]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    try:
        rc, stdout, stderr = run(cmd, timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise StepError(f"fuzz generation for grammar {version} timed out")
    if rc != 0:
        raise StepError(f"fuzz generation for grammar {version} failed ({rc}): "
                        f"{stderr.strip()[:200]}")

    stages = [s for s in config.fuzz["stages"] or config.stages
              if not stage_filter or s == stage_filter]
    tests = []
    for i, path in enumerate(stdout.splitlines()):
        path = path.strip()
        if not path or not os.path.isfile(path):
            continue
        for stage in stages:
            name = "fuzz_%04d" % i
            if name_filter and name_filter not in f"{stage}/{name}":
                continue
            test = Test(stage, name, path)
            expected = config.fuzz["exit"].get(stage, 0)
            test.meta = {"grammar": version, "exit": expected}
            test.meta_error = None
            test.is_fuzz = True
            tests.append(test)
    return tests


# ---------------------------------------------------------------------------
# Golden files
# ---------------------------------------------------------------------------

def golden_path(config, test):
    cfg = config.stage_cfg(test.stage)
    template = cfg.get("golden")
    if template is None:
        template = STAGE_GOLDEN_DEFAULT.get(test.stage)
    if template is None:
        raise StepError(f"config {config.path}: no golden template for stage '{test.stage}'")
    return os.path.join(test.dir, resolve(template, {"{name}": test.name}))


def golden_variants(config, test):
    """Return the first existing golden path for the test."""
    primary = golden_path(config, test)
    if os.path.exists(primary):
        return primary
    if test.stage == "llvm" and primary.endswith("out.ll"):
        alt = os.path.join(test.dir, "out.bc")
        if os.path.exists(alt):
            return alt
    return None


# ---------------------------------------------------------------------------
# Pre-processing (config-driven canonicalization)
# ---------------------------------------------------------------------------

def filter_json(value, keep, drop):
    """Recursively select scalar fields of JSON structures.

    Structural containers (nested objects/arrays) are always kept so the tree
    shape survives; only scalar fields are filtered by `keep` (whitelist) or
    `drop` (blacklist). This naturally removes token `value` and error
    `message` fields without forcing their wording.
    """
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                out[key] = filter_json(val, keep, drop)
            else:
                if keep is not None and key not in keep:
                    continue
                if keep is None and drop is not None and key in drop:
                    continue
                out[key] = val
        return out
    if isinstance(value, list):
        return [filter_json(item, keep, drop) for item in value]
    return value


def normalize_llvm(text):
    """Canonicalize compiler-chosen names so IR from different implementations
    can be diffed: unnamed SSA values (%N), unnamed basic block labels (N:)
    and attribute group ids (#N) are renamed in order of first appearance.
    Named values (%foo), globals and constants are left untouched.
    """
    mapping = {}
    counter = [0]

    def canon(key):
        if key not in mapping:
            mapping[key] = "v%d" % counter[0]
            counter[0] += 1
        return mapping[key]

    # LLVM numbers unnamed values, basic blocks and attribute groups from a
    # single namespace, so a block definition "N:" and its reference "%N"
    # resolve to the same canonical name.
    text = re.sub(r"(^[ \t]*)(%?)(\d+):",
                  lambda m: m.group(1) + "%" + canon(m.group(3)) + ":",
                  text, flags=re.M)
    text = re.sub(r"%(\d+)",
                  lambda m: "%" + canon(m.group(1)), text)
    text = re.sub(r"@(\d+)",
                  lambda m: "@" + canon(m.group(1)), text)
    text = re.sub(r"#(\d+)",
                  lambda m: "#" + canon(m.group(1)), text)
    return text


def transform(step, in_path, out_path):
    step_type = step.get("type")
    if step_type == "json":
        keep = step.get("keep")
        drop = step.get("drop")
        if keep is None and drop is None:
            raise StepError("preprocess 'json' step: at least one of 'keep' or 'drop' is required")
        if keep is not None and not isinstance(keep, list):
            raise StepError(f"preprocess 'json' step: 'keep' must be a list")
        if drop is not None and not isinstance(drop, list):
            raise StepError(f"preprocess 'json' step: 'drop' must be a list")
        try:
            data = json.loads(read_file(in_path))
        except (ValueError, OSError) as e:
            raise StepError(f"preprocess 'json' failed on {in_path}: {e}")
        out = json.dumps(filter_json(data, keep, drop), indent=2, sort_keys=True)
        write_file(out_path, out + "\n")
    elif step_type == "regex":
        pattern = step.get("pattern")
        repl = step.get("repl")  # None means delete matched lines
        if not isinstance(pattern, str):
            raise StepError("preprocess 'regex' step: 'pattern' is required")
        text = read_file(in_path)
        if repl is None:
            # Delete matching lines including trailing newline
            write_file(out_path, re.sub(pattern + r"\n?", "", text, flags=re.MULTILINE))
        else:
            write_file(out_path, re.sub(pattern, repl, text))
    elif step_type == "llvm":
        write_file(out_path, normalize_llvm(read_file(in_path)))
    elif step_type == "exec":
        cmd = step.get("cmd")
        if not isinstance(cmd, list):
            raise StepError("preprocess 'exec' step: 'cmd' must be a list")
        ph = {"{input}": in_path, "{output}": out_path, "{root}": REPO_ROOT}
        argv = [resolve(c, ph) for c in cmd]
        rc, _, err = run(argv, timeout=DEFAULT_TIMEOUT)
        if rc != 0:
            raise StepError(f"preprocess 'exec' step failed ({rc}): {err.strip()}")
    else:
        raise StepError(f"unknown preprocess step type: {step_type!r}")


def apply_preprocess(steps, in_path, workdir, tag):
    if not steps:
        return in_path
    current = in_path
    for i, step in enumerate(steps):
        out_path = os.path.join(workdir, "norm_%s_%d.txt" % (tag, i))
        try:
            transform(step, current, out_path)
        except StepError:
            raise
        except Exception as e:
            raise StepError(f"preprocess step {i} ({step.get('type')}) failed: {e}")
        current = out_path
    return current


def decode_llvm_dis(config, path, workdir, tag):
    """Decode a .bc golden/output into textual IR via llvm-dis."""
    out_path = os.path.join(workdir, "dis_%s.ll" % tag)
    argv = config.llvm_dis + [path]
    try:
        with open(out_path, "wb") as f:
            proc = subprocess.run(argv, stdout=f, stderr=subprocess.PIPE)
    except OSError as e:
        raise StepError(f"llvm-dis failed to start: {e}")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise StepError(f"llvm-dis failed on {path}: {err}")
    return out_path


def llvm_prepare(config, golden, actual, workdir):
    if golden.endswith(".bc"):
        golden = decode_llvm_dis(config, golden, workdir, "golden")
    if actual.endswith(".bc"):
        actual = decode_llvm_dis(config, actual, workdir, "actual")
    return golden, actual


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def text_diff(expected_path, actual_path):
    expected = read_file(expected_path).splitlines()
    actual = read_file(actual_path).splitlines()
    diff = list(difflib.unified_diff(
        expected, actual,
        fromfile="expected", tofile="actual", lineterm=""))
    return diff


def compare_tolerance(expected_path, actual_path, tolerance):
    golden_text = read_file(expected_path).split()
    actual_text = read_file(actual_path).split()
    if len(golden_text) != len(actual_text):
        return False
    for x, y in zip(golden_text, actual_text):
        if x == y:
            continue
        try:
            fx, fy = float(x), float(y)
        except ValueError:
            return False
        if abs(fx - fy) > tolerance:
            return False
    return True


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

def exit_spec(meta, stage=None, default=0):
    """Get the expected exit code for a test stage.

    If meta["exit"] is a dict, it maps stage names to exit codes.
    If stage is given, return the per-stage exit; otherwise the
    default for the whole test. If it's a flat value, applies to all.

    Backward compat: for the "run" stage, if no "run" key exists in
    the exit dict but a "compiler" key does, the "compiler" exit code
    is used (old meta.json used "compiler" for the execution exit).
    """
    e = meta.get("exit", default)
    if isinstance(e, dict):
        val = e.get(stage)
        if val is not None:
            return val
        # Backward compat: "run" stage falls back to "compiler" exit
        if stage == "run":
            val = e.get("compiler")
            if val is not None:
                return val
        return default
    return e


def exit_ok(rc, spec):
    if spec == "nonzero":
        return rc != 0
    if isinstance(spec, str) and spec.lstrip("-").isdigit():
        spec = int(spec)
    return rc == spec


def no_golden_result(result, meta, is_fuzz, stage=None):
    """Return the result for a test that has no golden file to compare.

    A failing result is returned as-is. Fuzz tests pass if their exit contract
    was met (no golden to diff). Exit-only tests pass on a nonzero/expected
    exit. Anything else is skipped because there is nothing to compare.
    """
    if result.status == "FAIL":
        return result
    if is_fuzz:
        return result  # fuzz exit contract met; nothing to compare
    spec = exit_spec(meta, stage=stage)
    if spec == "nonzero" or (isinstance(spec, int) and spec != 0):
        return result  # exit-only negative test
    result.status = "SKIP"
    result.lines.append("no golden file; nothing to compare")
    return result


def make_placeholders(test, workdir, stage, grammar):
    return {
        "{root}": REPO_ROOT,
        "{input}": test.src_path,
        "{exe}": os.path.join(workdir, "prog"),
        "{grammar}": grammar,
        "{tokens_out}": os.path.join(workdir, "tokens.json"),
        "{ast_out}": os.path.join(workdir, "ast.json"),
        "{llvm_out}": os.path.join(workdir, "out.ll"),
        "{tokens_in}": os.path.join(workdir, "tokens.json"),
        "{ast_in}": os.path.join(workdir, "ast.json"),
    }


class Result:
    def __init__(self, status, stage, name):
        self.status = status          # PASS / FAIL / SKIP / UPD
        self.stage = stage
        self.name = name
        self.elapsed = 0.0
        self.lines = []
        self.command: str | None = None
        self.is_fuzz = False

    @property
    def key(self):
        return f"{self.stage}/{self.name}"


def run_plain(config, test, workdir, update, grammar, check_ir=False):
    stage = test.stage
    cfg = config.stage_cfg(stage)
    cmd = cfg.get("cmd")
    if not isinstance(cmd, list):
        raise SkipError(f"stage '{stage}' is not configured (no 'cmd')")
    meta = test.meta
    timeout = meta.get("timeout", cfg.get("timeout", DEFAULT_TIMEOUT))

    ph = make_placeholders(test, workdir, stage, grammar)
    argv = [resolve(c, ph) for c in cmd]
    rc, stdout, stderr = run(argv, timeout=timeout)

    out_spec = cfg.get("out", "stdout")
    if out_spec == "stdout":
        out_text = stdout
        compare_path = os.path.join(workdir, stage + ".raw")
        write_file(compare_path, out_text)
        if stage == "lexer":
            write_file(ph["{tokens_in}"], out_text)
        if stage == "spl_parser":
            write_file(ph["{ast_in}"], out_text)
    else:
        out_path = resolve(out_spec, ph)
        compare_path = out_path
        if os.path.exists(out_path):
            out_text = read_file(out_path)
        else:
            out_text = None

    result = Result("PASS", stage, test.name)
    result.command = " ".join(argv)

    spec = exit_spec(meta, stage=stage)
    exit_bad = not exit_ok(rc, spec)
    if exit_bad:
        result.lines.append(f"exit: expected {spec!r}, got {rc}")
    if stderr.strip():
        result.lines.append("stderr: " + stderr.strip().splitlines()[0][:200])

    if update:
        golden = golden_path(config, test)
        if out_text is not None:
            write_file(golden, out_text)
            result.status = "UPD"
            result.lines.append(f"golden -> {os.path.relpath(golden, SCRIPT_DIR)}")
            if exit_bad:
                result.lines.append(
                    "WARNING: exit contract not met; golden updated anyway")
        else:
            result.status = "FAIL"
            result.lines.append("no output produced; golden not updated")
        return result

    if exit_bad:
        result.status = "FAIL"

    # For llvm stage, skip golden comparison unless --check-ir was passed
    if stage == "llvm" and not check_ir:
        return result

    golden = golden_variants(config, test)
    if golden is None:
        return no_golden_result(result, meta, test.is_fuzz, stage=stage)

    if out_text is None:
        result.status = "FAIL"
        result.lines.append(f"stage wrote no output to {out_spec!r}")
        return result

    golden, compare_path = llvm_prepare(config, golden, compare_path, workdir) \
        if stage == "llvm" else (golden, compare_path)

    steps = cfg.get("preprocess", [])
    golden_norm = apply_preprocess(steps, golden, workdir, "golden")
    actual_norm = apply_preprocess(steps, compare_path, workdir, "actual")

    diff = text_diff(golden_norm, actual_norm)
    if diff:
        result.status = "FAIL"
        shown = diff[:DIFF_MAX_LINES]
        result.lines.append("diff (expected vs actual):")
        result.lines.extend("  " + line for line in shown)
        if len(diff) > DIFF_MAX_LINES:
            result.lines.append(f"  ... (+{len(diff) - DIFF_MAX_LINES} more lines)")
    return result


def run_compile(config, test, workdir, update, grammar):
    """'compiler' stage: compile the source only, check compilation exit.

    The config 'compiler' command is responsible for compiling and linking
    the source into an executable. The exit code is from the compilation
    (splc / clang) step, not from program execution.

    This stage has no stdout golden to compare; it only verifies that
    compilation succeeds (exit 0) or matches the expected compiler exit.
    """
    meta = test.meta
    cfg = config.stage_cfg("compile")
    cmd = cfg.get("cmd")
    if not isinstance(cmd, list):
        raise SkipError("stage 'compiler' is not configured (no 'cmd')")
    timeout = meta.get("timeout", cfg.get("timeout", DEFAULT_TIMEOUT))

    ph = make_placeholders(test, workdir, "compiler", grammar)
    ph["{exe}"] = os.path.join(workdir, "prog")
    argv = [resolve(c, ph) for c in cmd]

    rc, _, stderr = run(argv, timeout=timeout)

    result = Result("PASS", "compiler", test.name)
    result.command = " ".join(argv)

    spec = exit_spec(meta, stage="compiler", default=0)
    exit_bad = not exit_ok(rc, spec)
    if exit_bad:
        result.lines.append(f"exit: expected {spec!r}, got {rc}")
    if stderr.strip():
        result.lines.append("stderr: " + stderr.strip().splitlines()[0][:200])

    if exit_bad:
        result.status = "FAIL"

    if update:
        # compiler stage produces no golden file; nothing to update
        if not exit_bad:
            result.status = "UPD"
            result.lines.append("compilation succeeded (no golden file)")
        else:
            result.status = "FAIL"
            result.lines.append("compilation failed; no golden to update")
        return result

    return result


def run_exec(config, test, workdir, update, grammar):
    """'run' stage: compile then execute the program.

    First runs the 'compiler' stage command to produce an executable.
    Then runs the 'run' stage command to execute it and capture stdout.

    The execution exit code is checked against meta["exit"]["run"]
    (falling back to meta["exit"]["compiler"] for backward compat).
    Stdout is compared against the 'stdout' golden file.
    """
    meta = test.meta
    compile_cfg = config.stage_cfg("compile")
    run_cfg = config.stage_cfg("run")
    if not isinstance(compile_cfg.get("cmd"), list):
        raise SkipError("stage 'run' requires a 'compiler' stage with 'cmd' in config")
    if not isinstance(run_cfg.get("cmd"), list):
        raise SkipError("stage 'run' requires a 'run' stage with 'cmd' in config")

    result = Result("PASS", "run", test.name)
    ph = make_placeholders(test, workdir, "run", grammar)
    ph["{exe}"] = os.path.join(workdir, "prog")

    # Step 1: Compile source into executable
    compile_argv = [resolve(c, ph) for c in compile_cfg["cmd"]]
    rc, _, cerr = run(compile_argv,
                      timeout=meta.get("timeout", compile_cfg.get("timeout", DEFAULT_TIMEOUT)))
    if rc != 0:
        result.status = "FAIL"
        result.lines.append(f"compile failed (exit {rc})")
        if cerr.strip():
            result.lines.append("stderr: " + cerr.strip().splitlines()[0][:200])
        return result

    # Step 2: Run the executable
    stdin_path = os.path.join(test.dir, "stdin")
    stdin_data = open(stdin_path, "rb").read() if os.path.exists(stdin_path) else None

    run_argv = [resolve(c, ph) for c in run_cfg["cmd"]]
    timeout = meta.get("timeout", run_cfg.get("timeout", DEFAULT_TIMEOUT))
    rc, stdout, stderr = run(run_argv, stdin=stdin_data, timeout=timeout)
    result.command = " ".join(run_argv)

    raw_path = os.path.join(workdir, "run.raw")
    write_file(raw_path, stdout)

    spec = exit_spec(meta, stage="run")
    exit_bad = not exit_ok(rc, spec)
    if exit_bad:
        result.lines.append(f"exit: expected {spec!r}, got {rc}")
    if stderr.strip():
        result.lines.append("stderr: " + stderr.strip().splitlines()[0][:200])

    golden = golden_path(config, test)
    if update:
        # Only write the golden file if stdout is non-empty.
        # Empty stdout files are not committed as goldens.
        if stdout.strip():
            write_file(golden, stdout)
            result.status = "UPD"
            result.lines.append(f"golden -> {os.path.relpath(golden, SCRIPT_DIR)}")
        else:
            # Stdout is empty; remove golden if it exists (empty goldens are
            # not meaningful) and report success-without-update.
            if os.path.exists(golden):
                os.remove(golden)
                result.lines.append(f"removed empty golden {os.path.relpath(golden, SCRIPT_DIR)}")
            result.status = "UPD"
            result.lines.append("stdout empty; no golden written")
        if exit_bad:
            result.lines.append(
                "WARNING: exit contract not met; golden updated anyway")
        return result

    if exit_bad:
        result.status = "FAIL"

    if not os.path.exists(golden):
        return no_golden_result(result, meta, test.is_fuzz, stage="run")

    steps = run_cfg.get("preprocess", [])
    golden_norm = apply_preprocess(steps, golden, workdir, "golden")
    actual_norm = apply_preprocess(steps, raw_path, workdir, "actual")

    tolerance = meta.get("tolerance")
    if tolerance is not None:
        if not compare_tolerance(golden_norm, actual_norm, tolerance):
            result.status = "FAIL"
            result.lines.append(f"output differs beyond tolerance {tolerance}")
    else:
        diff = text_diff(golden_norm, actual_norm)
        if diff:
            result.status = "FAIL"
            shown = diff[:DIFF_MAX_LINES]
            result.lines.append("diff (expected vs actual):")
            result.lines.extend("  " + line for line in shown)
            if len(diff) > DIFF_MAX_LINES:
                result.lines.append(f"  ... (+{len(diff) - DIFF_MAX_LINES} more lines)")
    return result


def run_test(config, test, workdir_root, update, grammar, check_ir=False):
    if test.meta_error:
        result = Result("FAIL", test.stage, test.name)
        result.lines.append(test.meta_error)
        return result
    workdir = os.path.join(workdir_root, test.stage, test.name)
    os.makedirs(workdir, exist_ok=True)
    start = time.monotonic()
    try:
        if test.stage == "compiler":
            result = run_compile(config, test, workdir, update, grammar)
        elif test.stage == "run":
            result = run_exec(config, test, workdir, update, grammar)
        else:
            result = run_plain(config, test, workdir, update, grammar, check_ir=check_ir)
    except subprocess.TimeoutExpired:
        result = Result("FAIL", test.stage, test.name)
        result.lines.append("timed out")
    except SkipError as e:
        result = Result("SKIP", test.stage, test.name)
        result.lines.append(str(e))
    except StepError as e:
        result = Result("FAIL", test.stage, test.name)
        result.lines.append(str(e))
    result.is_fuzz = test.is_fuzz
    result.elapsed = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

COLOR = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m",
         "UPD": "\033[36m", "RESET": "\033[0m"}


def print_result(result, verbose, no_color=False):
    def paint(status, text):
        if no_color:
            return text
        return COLOR[status] + text + COLOR["RESET"]

    head = paint(result.status, result.status)
    print(f"{head}  {result.stage}/{result.name}  ({result.elapsed:.2f}s)")
    if result.status != "PASS" or verbose:
        if result.command:
            print(f"    command: {result.command}")
        for line in result.lines:
            print(f"    {line}")


def run_batch(config, batch, workdir_root, update, jobs, grammar, check_ir=False):
    """Run a set of tests (serial or parallel) preserving their input order."""
    if not batch:
        return []
    if jobs == 1:
        return [run_test(config, t, workdir_root, update, grammar, check_ir=check_ir) for t in batch]
    ordered_index = {id(t): i for i, t in enumerate(batch)}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {id(t): pool.submit(run_test, config, t, workdir_root, update, grammar, check_ir=check_ir)
                   for t in batch}
        return [futures[tid].result()
                for tid in sorted(futures, key=lambda tid: ordered_index[tid])]


def list_compatibility_matrix(config, test_root, name_filter, stage_filter):
    """Print a pretty table of test cases with grammar version compatibility."""
    versions = config.versions
    no_color = not sys.stdout.isatty()

    def G(s):
        return s if no_color else "\033[32m" + s + "\033[0m"

    def R(s):
        return s if no_color else "\033[31m" + s + "\033[0m"

    def Y(s):
        return s if no_color else "\033[33m" + s + "\033[0m"

    # Discover all tests without grammar filtering, collect unique test dirs
    test_dirs = set()
    for dirpath, dirnames, filenames in os.walk(test_root):
        if dirpath == test_root:
            continue
        if "meta.json" not in filenames or "test.spl" not in filenames:
            continue
        rel_path = os.path.relpath(dirpath, test_root)
        if name_filter and name_filter not in rel_path:
            continue
        test_dirs.add((dirpath, rel_path))

    if not test_dirs:
        print("no tests found")
        return

    # Sort test dirs
    test_dirs = sorted(test_dirs, key=lambda x: x[1])

    # Header
    header = f"{'Test case':<50}"
    for v in versions:
        header += f"  G{v}"
    print(header)
    print("-" * len(header))

    for dirpath, rel_path in test_dirs:
        meta_path = os.path.join(dirpath, "meta.json")
        meta, _ = load_meta(meta_path)

        # Determine which stages are applicable
        filenames = os.listdir(dirpath)
        found_stages = set()
        for f in filenames:
            stage = GOLDEN_TO_STAGE.get(f)
            if stage is not None:
                found_stages.add(stage)
        for stage in meta.get("stages", []):
            found_stages.add(stage)
        if not found_stages:
            for stage in STAGE_GOLDEN_DEFAULT:
                s = exit_spec(meta, stage=stage)
                if s == "nonzero" or (isinstance(s, int) and s != 0):
                    found_stages.add(stage)
                    break
        if not found_stages:
            found_stages = set(STAGE_GOLDEN_DEFAULT.keys())

        # Check each grammar version
        cells = []
        for v in versions:
            # Check if the test is meta-selected for this version
            if not meta_selected(meta, v):
                cells.append(Y("—"))  # not applicable by constraint
                continue
            # Check if any stage is selected
            any_stage = False
            for stage in found_stages:
                if stage_selected(meta, stage, v):
                    any_stage = True
                    break
            if any_stage:
                cells.append(G("✓"))
            else:
                cells.append(R("✗"))

        row = f"{rel_path:<50}"
        for c in cells:
            row += f"  {c:>2}"
        print(row)

    print(f"\nLegend: {G('✓')}=supported, {R('✗')}=not supported, {Y('—')}=excluded by constraint")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="run_tests.py",
        description="Compiler sandbox test runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="test",
                        choices=["build", "test", "update", "list", "list-matrix"])
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="driver config (default: %(default)s)")
    parser.add_argument("--check-ir", action="store_true",
                        help="also compare LLVM IR against golden out.ll files")
    parser.add_argument("--grammar", default=None,
                        help="target grammar version (required if not set in config default_grammar)")
    parser.add_argument("--stage", default=None,
                        help="run only tests under test/<stage>")
    parser.add_argument("--test", dest="name_filter", default=None,
                        help="run only tests whose 'stage/name' contains this")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-j", dest="jobs", type=int, default=1,
                        help="number of parallel test workers (default: %(default)s)")
    parser.add_argument("-x", "--stop-on-fail", action="store_true")
    parser.add_argument("--keep", choices=["none", "all", "failed"],
                        default="failed",
                        help="which per-test working directories to keep "
                             "(default: %(default)s)")
    parser.add_argument("--no-build", action="store_true",
                        help="skip the build step (test/update only)")
    parser.add_argument("--fuzz", action="store_true",
                        help="in addition to committed tests, generate and run "
                             "random Grammarinator tests for the selected grammar")
    parser.add_argument("--fuzz-count", type=int, default=None,
                        help="number of fuzz inputs to generate (default: config 'fuzz.count')")
    parser.add_argument("--fuzz-seed", type=int, default=None,
                        help="seed for fuzz generation (reproducible runs)")
    parser.add_argument("--fuzz-tokens", type=int, default=None,
                        help="max tokens per fuzz input (default: config 'fuzz.max_tokens')")
    parser.add_argument("--fuzz-depth", type=int, default=None,
                        help="optional max generation depth for fuzz inputs "
                             "(default: unlimited, size bounded by --fuzz-tokens)")
    args = parser.parse_args(argv)

    no_color = not sys.stdout.isatty()
    config = Config(args.config)

    # Resolve the target grammar: CLI --grammar takes priority, then config default.
    effective_grammar = args.grammar or config.default_grammar
    if effective_grammar is None:
        parser.error(
            "--grammar is required (no default_grammar in config.json)")

    if args.command == "build":
        if not config.build_cmd:
            print("no 'build' command in config")
            return 1
        resolved = resolve(config.build_cmd, {"{root}": REPO_ROOT})
        print("> " + resolved)
        rc, stdout, stderr = shell_build(resolved)
        if stdout.strip():
            print(stdout.rstrip())
        if stderr.strip():
            print(stderr.rstrip(), file=sys.stderr)
        return 0 if rc == 0 else 1

    if args.command == "update" and args.fuzz:
        raise StepError("--fuzz cannot be combined with 'update' "
                        "(fuzz inputs are ephemeral; nothing to freeze)")

    if args.command in ("test", "update") and config.build_cmd and not args.no_build:
        resolved = resolve(config.build_cmd, {"{root}": REPO_ROOT})
        print("> " + resolved)
        rc, _, stderr = shell_build(resolved)
        if rc != 0:
            print(stderr.rstrip(), file=sys.stderr)
            print("build failed", file=sys.stderr)
            return 1

    check_ir = args.check_ir
    committed, excluded = discover_tests(SCRIPT_DIR, effective_grammar, args.stage, args.name_filter,
                                            os.path.abspath(config.out_dir),
                                            check_ir=check_ir)

    workdir_root = config.out_dir

    # Fuzz inputs are generated in a background thread so committed tests can
    # run (and their results show) without waiting for the fuzzer.
    fuzz_seed_line = None
    gen_thread = None
    gen_start = None
    gen_error = []
    fuzz_tests = []
    if args.fuzz:
        shutil.rmtree(workdir_root, ignore_errors=True)
        os.makedirs(workdir_root, exist_ok=True)
        count = args.fuzz_count if args.fuzz_count is not None else config.fuzz["count"]
        tokens = args.fuzz_tokens if args.fuzz_tokens is not None else config.fuzz["max_tokens"]
        depth = args.fuzz_depth if args.fuzz_depth is not None else config.fuzz["max_depth"]
        # Always pick an explicit seed so a run is reproducible; print it.
        fuzz_seed = args.fuzz_seed if args.fuzz_seed is not None \
            else random.SystemRandom().randrange(1, 2**31)
        versions = select_fuzz_versions(config, args.grammar)
        fuzz_seed_line = f"fuzz: grammar version(s) {', '.join(versions)}, " \
            f"count {count}, max_tokens {tokens}"
        if depth is not None:
            fuzz_seed_line += f", depth {depth}"
        fuzz_seed_line += f", seed {fuzz_seed}"

        def generate():
            try:
                for version in versions:
                    fuzz_tests.extend(generate_fuzz_tests(
                        config, version, count, fuzz_seed, tokens, depth,
                        workdir_root, args.stage, args.name_filter))
            except BaseException as e:  # noqa: BLE001 - re-raised in main
                gen_error.append(e)

        gen_start = time.monotonic()
        gen_thread = threading.Thread(target=generate, name="fuzz-gen", daemon=True)
        gen_thread.start()
    else:
        shutil.rmtree(workdir_root, ignore_errors=True)
        os.makedirs(workdir_root, exist_ok=True)

    if args.command == "list":
        if gen_thread is not None:
            gen_thread.join()
            if gen_error:
                raise gen_error[0]
        # Group by stage in canonical order
        stage_tests = {}
        for test in committed:
            stage_tests.setdefault(test.stage, []).append(test)
        for stage in STAGE_ORDER:
            tests_s = stage_tests.get(stage)
            if not tests_s:
                continue
            print(f"=== {stage.upper()} ===")
            for test in tests_s:
                print(f"  {test.stage}/{test.name}")
        if fuzz_tests:
            print("=== FUZZ ===")
            for test in fuzz_tests:
                print(f"  {test.stage}/{test.name}")
        return 0

    if args.command == "list-matrix":
        list_compatibility_matrix(config, SCRIPT_DIR, args.name_filter, args.stage)
        return 0

    if not committed and not fuzz_tests and gen_thread is None:
        print("no tests selected")
        return 0

    # Run and print committed tests immediately.
    committed_results = run_batch(config, committed, workdir_root,
                                  args.command == "update", args.jobs,
                                  effective_grammar, check_ir=check_ir)
    print()
    # Group results by stage in canonical order
    stage_results = {}
    for result in committed_results:
        stage_results.setdefault(result.stage, []).append(result)
    for stage in STAGE_ORDER:
        results_stage = stage_results.get(stage)
        if not results_stage:
            continue
        stage_upper = stage.upper()
        print(f"=== {stage_upper} ===")
        for result in results_stage:
            # Strip stage prefix from name when showing under grouped header
            #orig_name = result.name
            #short_name = orig_name.removeprefix(result.stage + "/")
            #result.name = short_name
            print_result(result, args.verbose, no_color)
            #result.name = orig_name
        print()

    # Now wait for fuzz generation and run the fuzz tests.
    fuzz_results = []
    gen_failed = None
    if args.fuzz:
        assert gen_thread is not None and fuzz_seed_line is not None and gen_start is not None
        gen_thread.join()
        gen_elapsed = time.monotonic() - gen_start
        fuzz_seed_line += f", generated in {gen_elapsed:.2f}s"
        if gen_error:
            gen_failed = gen_error[0]
        elif args.stop_on_fail and any(r.status == "FAIL" for r in committed_results):
            print("stop-on-fail: skipping fuzz tests after committed failure",
                  file=sys.stderr)
        else:
            if not fuzz_tests:
                print(f"warning: {fuzz_seed_line} produced no fuzz tests; "
                      "check --fuzz-tokens (too few tokens, or invalid grammar)",
                      file=sys.stderr)
            else:
                fuzz_results = run_batch(config, fuzz_tests, workdir_root,
                                         args.command == "update", args.jobs,
                                         effective_grammar, check_ir=check_ir)
                print("---- fuzz tests ----")
                print(fuzz_seed_line)
                stage_results_fuzz = {}
                for result in fuzz_results:
                    stage_results_fuzz.setdefault(result.stage, []).append(result)
                for stage in STAGE_ORDER:
                    results_s = stage_results_fuzz.get(stage)
                    if not results_s:
                        continue
                    for result in results_s:
                        print_result(result, args.verbose, no_color)
                print(fuzz_seed_line)

    results = committed_results + fuzz_results
    print()

    # Print excluded tests
    if excluded:
        for name in excluded:
            print(f"EXCLUDED  {name}")
        print()

    counts = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    if excluded:
        counts["EXCLUDED"] = len(excluded)
    summary = ", ".join(f"{v} {k}" for k, v in counts.items())
    print(f"summary: {summary}")

    if args.keep == "none":
        shutil.rmtree(workdir_root, ignore_errors=True)
    elif args.keep == "failed":
        for result in results:
            if result.status != "FAIL":
                shutil.rmtree(
                    os.path.join(workdir_root, result.stage, result.name),
                    ignore_errors=True)

    if gen_failed is not None:
        raise gen_failed

    failed = sum(1 for r in results if r.status == "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StepError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
