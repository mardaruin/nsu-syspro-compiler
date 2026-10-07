"""Self-tests for the run_tests.py harness, driven by a mock compiler.

These run without a real compiler: `run_tests` is imported and pointed at a
generated fixture tree (see conftest.Harness), then its `main()` is invoked
in-process. Golden tests/docs in test/ are NOT involved.

Test case layout (new format):
  {root}/{name}/
    meta.json        (always created)
    test.spl         (the source)
    tokens.json      (lexer golden)
    ast.json         (spl_parser golden)
    out.ll           (llvm golden)
    stdout           (compiler golden)
"""

import json
import os
import sys

import pytest

# Make the real harness importable; conftest already added test/ to sys.path,
# this makes the import explicit and linter-friendly.
TEST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TEST_DIR)
import run_tests  # noqa: E402

from conftest import add_lexer, RAW_TOKENS, TOKENS, Harness


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------

def test_parse_version_and_cmp():
    assert run_tests.parse_version("2") == (2,)
    assert run_tests.parse_version("2.1") == (2, 1)
    assert run_tests.cmp_versions((2,), (1, 9)) > 0
    assert run_tests.cmp_versions((1, 2), (1, 2, 0)) == 0
    assert run_tests.cmp_versions((1,), (2,)) < 0


def test_grammar_matches():
    gm = run_tests.grammar_matches
    assert gm(None, "1") is True
    assert gm(["1", "2"], "2") is True
    assert gm(["1"], "2") is False
    assert gm("2", "2") is True
    assert gm("2", "1") is False
    assert gm(">=2", "2") is True
    assert gm(">=2", "1") is False
    assert gm(">2", "2") is False
    assert gm("==2", "2") is True
    assert gm("<3", "2") is True
    assert gm("2", "2.0") is True  # padded minor
    assert gm(">=2", "2.1") is True


def test_resolve_placeholders():
    assert run_tests.resolve("{root}/a/{input}", {"{root}": "/t", "{input}": "x.spl"}) == "/t/a/x.spl"


def test_exit_ok():
    eo = run_tests.exit_ok
    assert eo(0, 0) is True
    assert eo(1, 0) is False
    assert eo(1, "nonzero") is True
    assert eo(0, "nonzero") is False
    assert eo(5, "5") is True
    assert eo(5, 5) is True


def test_filter_json_keep_and_drop():
    data = {"kind": "IDENT", "value": "x", "line": 1, "column": 2}
    assert run_tests.filter_json(data, ["kind", "line"], None) == {"kind": "IDENT", "line": 1}
    assert run_tests.filter_json(data, None, ["value"]) == {"kind": "IDENT", "line": 1, "column": 2}
    assert run_tests.filter_json(data, ["kind"], None) == {"kind": "IDENT"}


def test_normalize_llvm():
    src = "%1 = alloca i32\n%2 = add i32 %1, %1\nbr label %3\n3:"
    out = run_tests.normalize_llvm(src)
    assert out == "%v1 = alloca i32\n%v2 = add i32 %v1, %v1\nbr label %v0\n%v0:"


def test_compare_tolerance(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text("1.0 2.0")
    b.write_text("1.05 1.99")
    assert run_tests.compare_tolerance(str(a), str(b), 0.1)
    b.write_text("2.0 2.0")
    assert not run_tests.compare_tolerance(str(a), str(b), 0.1)


# ---------------------------------------------------------------------------
# Helper to build a lexer-like stage config
# ---------------------------------------------------------------------------

def lexer_stage(keep=None):
    stage = {
        "cmd": ["{root}/mock_compiler.py", "-t", "{tokens_out}", "{input}"],
        "out": "{tokens_out}",
    }
    if keep is not None:
        stage["preprocess"] = [{"type": "json", "keep": keep}]
    return {"lexer": stage}


def parser_stage(keep=None):
    stage = {
        "cmd": ["{root}/mock_compiler.py", "-a", "{ast_out}", "{input}"],
        "out": "{ast_out}",
    }
    if keep is not None:
        stage["preprocess"] = [{"type": "json", "keep": keep}]
    return {"spl_parser": stage}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_list_lists_tests(harness, run):
    add_lexer(harness, "a")
    add_lexer(harness, "b")
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    rc, out = run(["list", "--config", cfg])
    assert rc == 0
    assert "lexer/a" in out
    assert "lexer/b" in out


def test_stage_and_name_filters(harness, run):
    add_lexer(harness, "alpha")
    add_lexer(harness, "beta")
    harness.add_test("gamma", src="0;", ast=json.dumps({"kind": "ast", "ok": True}))
    cfg = harness.write_config(stages={
        **lexer_stage(["kind"]),
        **parser_stage(["kind"]),
    })
    rc, out = run(["list", "--config", cfg, "--stage", "lexer"])
    assert rc == 0
    assert "lexer/alpha" in out and "spl_parser/gamma" not in out
    rc, out = run(["list", "--config", cfg, "--test", "beta"])
    assert rc == 0
    assert "lexer/beta" in out and "lexer/alpha" not in out


def test_grammar_version_filter(harness, run):
    add_lexer(harness, "v1", meta={"grammar": "1"})
    add_lexer(harness, "v2", meta={"grammar": "2"})
    add_lexer(harness, "any")
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["list", "--config", cfg, "--grammar", "1"])
    assert "lexer/v1" in out and "lexer/v2" not in out and "lexer/any" in out
    rc, out = run(["list", "--config", cfg, "--grammar", "2"])
    assert "lexer/v2" in out and "lexer/v1" not in out
    add_lexer(harness, "v2plus", meta={"grammar": ">=2"})
    rc, out = run(["list", "--config", cfg, "--grammar", "2"])
    assert "lexer/v2plus" in out


def test_nested_test_paths(harness, run):
    """Tests in subdirectories of any depth are discovered."""
    add_lexer(harness, "grammar2/booleans/if-else")
    add_lexer(harness, "grammar3/functions/calls/nested")
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["list", "--config", cfg])
    assert rc == 0
    assert "lexer/grammar2/booleans/if-else" in out
    assert "lexer/grammar3/functions/calls/nested" in out


def test_exclude_boolean_skips_test(harness, run):
    """Test that exclude: true in meta.json skips the test."""
    add_lexer(harness, "should-run")
    add_lexer(harness, "should-skip", meta={"exclude": True})
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    rc, out = run(["list", "--config", cfg])
    assert rc == 0
    assert "lexer/should-run" in out
    assert "lexer/should-skip" not in out


def test_exclude_false_runs_test(harness, run):
    """Test that exclude: false (or absent) runs normally."""
    add_lexer(harness, "runs", meta={"exclude": False})
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    rc, out = run(["list", "--config", cfg])
    assert rc == 0
    assert "lexer/runs" in out


def test_exclude_shown_in_summary(harness, run):
    """Test that excluded tests appear in output and summary."""
    add_lexer(harness, "active")
    add_lexer(harness, "hidden", meta={"exclude": True})
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    rc, out = run(["test", "--no-build", "--config", cfg])
    assert rc == 0
    assert "EXCLUDED  hidden" in out
    assert "1 EXCLUDED" in out
    assert "PASS  lexer/active" in out


def test_grammar_list_constraint(harness, run):
    """List grammar constraints are correctly matched."""
    add_lexer(harness, "v12", meta={"grammar": ["1", "2"]})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["list", "--config", cfg, "--grammar", "1"])
    assert "lexer/v12" in out
    rc, out = run(["list", "--config", cfg, "--grammar", "2"])
    assert "lexer/v12" in out
    rc, out = run(["list", "--config", cfg, "--grammar", "3"])
    assert "lexer/v12" not in out


def test_compare_tolerance_edges(harness, run):
    """Tolerance comparison edge cases: empty, single value, mixed non-numeric."""
    # Empty
    (harness.root / "_empty_a").write_text("")
    (harness.root / "_empty_b").write_text("")
    assert run_tests.compare_tolerance(
        str(harness.root / "_empty_a"), str(harness.root / "_empty_b"), 0.1)

    # Single value equal within tolerance
    (harness.root / "_single_a").write_text("42.0")
    (harness.root / "_single_b").write_text("42.05")
    assert run_tests.compare_tolerance(
        str(harness.root / "_single_a"), str(harness.root / "_single_b"), 0.1)

    # Single value beyond tolerance
    (harness.root / "_single_fail_a").write_text("1.0")
    (harness.root / "_single_fail_b").write_text("2.0")
    assert not run_tests.compare_tolerance(
        str(harness.root / "_single_fail_a"), str(harness.root / "_single_fail_b"), 0.1)

    # Same non-numeric value passes (string comparison)
    (harness.root / "_str_a").write_text("abc")
    (harness.root / "_str_b").write_text("abc")
    assert run_tests.compare_tolerance(
        str(harness.root / "_str_a"), str(harness.root / "_str_b"), 0.1)

    # Different non-numeric values fail
    (harness.root / "_str_fail_a").write_text("abc")
    (harness.root / "_str_fail_b").write_text("xyz")
    assert not run_tests.compare_tolerance(
        str(harness.root / "_str_fail_a"), str(harness.root / "_str_fail_b"), 0.1)


# ---------------------------------------------------------------------------
# Execution: golden, exit, crash, timeout
# ---------------------------------------------------------------------------

def test_golden_pass(harness, run):
    add_lexer(harness, "ok")
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 0
    assert "PASS  lexer/ok" in out


def test_golden_fail_prints_diff(harness, run):
    add_lexer(harness, "bad", tokens=json.dumps(TOKENS + [{"kind": "MINUS"}]))
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "FAIL  lexer/bad" in out
    assert "diff" in out


def test_wrong_exit_contract_fails(harness, run):
    add_lexer(harness, "bad", src="// exit: 3\n", meta={"exit": 0})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "FAIL" in out and "got 3" in out


def test_exit_only_nonzero_no_golden(harness, run):
    """Exit-only test: no golden, exit: nonzero, should be auto-discovered and PASS."""
    harness.add_test("neg", src="// exit: 3\n",
                     meta={"exit": "nonzero"})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 0
    assert "PASS  lexer/neg" in out


def test_exit_only_exact_code_auto_discover(harness, run):
    """Exit-only test: no golden, exact non-zero exit, should be auto-discovered and PASS."""
    harness.add_test("exit5", src="// exit: 5\n",
                     meta={"exit": 5})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 0
    assert "PASS  lexer/exit5" in out


def test_exit_only_stages_list_skips(harness, run):
    """Exit-only test: explicit stages list + exit=0, no golden -> SKIP."""
    harness.add_test("skipme", src="// exit: 0\n",
                     meta={"stages": ["lexer"], "exit": 0})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 0
    assert "SKIP  lexer/skipme" in out


def test_nonzero_exit_negative_pass(harness, run):
    add_lexer(harness, "neg", src="// exit: 3\n", meta={"exit": "nonzero"})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 0
    assert "PASS  lexer/neg" in out


def test_crash_fails(harness, run):
    add_lexer(harness, "crash", src="// crash\n", meta={"exit": 0})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "FAIL  lexer/crash" in out


def test_timeout_fails(harness, run):
    add_lexer(harness, "slow", src="// sleep\n", meta={"timeout": 1})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "FAIL" in out and "timed out" in out


def test_missing_stage_skips(harness, run):
    # A test with only tokens.json but no lexer stage configured - the stage
    # has no 'cmd', so discovery still finds it, but run_plain skips it.
    add_lexer(harness, "x")
    cfg = harness.write_config(stages={})  # no stages configured
    rc, out = run(["test", "--config", cfg])
    assert rc == 0
    assert "SKIP  lexer/x" in out
    assert "not configured" in out


# ---------------------------------------------------------------------------
# update mode
# ---------------------------------------------------------------------------

def test_update_regenerates_golden(harness, run):
    add_lexer(harness, "u", src="0;")
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    rc, out = run(["update", "--config", cfg])
    assert rc == 0
    assert "UPD" in out
    golden = harness.root / "u" / "tokens.json"
    assert golden.exists()
    # Re-running now passes against the regenerated golden.
    rc, out = run(["test", "--config", cfg])
    assert rc == 0 and "PASS  lexer/u" in out


def test_update_no_output_fails(harness, run):
    """update with no output produced should fail with a clear message."""
    harness.add_test("a", src="0;", meta={"stages": ["lexer"]})
    # Stage that claims to write {tokens_out} but doesn't
    stage = {
        "cmd": ["python3", "-S", "-c", ""],  # writes nothing
        "out": "{tokens_out}",
    }
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["update", "--config", cfg])
    assert rc == 1
    assert "FAIL" in out
    assert "no output produced" in out


def test_update_keeps_consistent_after_edit(harness, run):
    add_lexer(harness, "u")
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    run(["update", "--config", cfg])
    golden = harness.root / "u" / "tokens.json"
    before = golden.read_text()
    run(["update", "--config", cfg])
    assert golden.read_text() == before  # idempotent


# ---------------------------------------------------------------------------
# --keep / -x / parallel / fuzz orchestration
# ---------------------------------------------------------------------------

def make_fuzz_config():
    return {
        "grammar": {"1": "doc/grammar1.g4"},
        "count": 3,
        "max_tokens": 30,
        "exit": {"lexer": 0},
    }


def test_fuzz_creates_tests_and_orders_after_committed(harness, run):
    add_lexer(harness, "committed")
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]), fuzz=make_fuzz_config())
    rc, out = run(["test", "--fuzz", "--fuzz-count", "3", "--fuzz-seed", "7", "--config", cfg])
    assert rc == 0
    committed_pos = out.index("lexer/committed")
    fuzz_pos = out.index("---- fuzz tests ----")
    assert committed_pos < fuzz_pos
    assert "fuzz: grammar version(s) 1" in out
    assert "generated in" in out and "seed 7" in out
    assert "PASS  lexer/fuzz_0000" in out


def test_stop_on_fail_skips_fuzz(harness, run):
    add_lexer(harness, "bad", src="// exit: 3\n", meta={"exit": 0})
    cfg = harness.write_config(stages=lexer_stage(["kind"]), fuzz=make_fuzz_config())
    rc, out = run(["test", "--fuzz", "-x", "--config", cfg])
    assert rc == 1
    assert "skipping fuzz tests" in out
    assert "---- fuzz tests ----" not in out


def test_parallel_keeps_order(harness, run):
    for i in range(20):
        add_lexer(harness, f"t{i:02d}")
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "-j", "8", "--config", cfg])
    assert rc == 0
    for i in range(20):
        assert out.index(f"lexer/t{i:02d}")
    names = [line.split()[1] for line in out.splitlines() if "lexer/t" in line]
    assert names == sorted(names)  # deterministic ordering


def test_keep_modes(harness, run):
    add_lexer(harness, "x")
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]))
    run(["update", "--config", cfg])  # create workdirs via a test pass
    run(["test", "--config", cfg, "--keep", "none"])
    assert not harness.out_dir.exists()
    run(["test", "--config", cfg, "--keep", "all"])
    assert (harness.out_dir / "lexer").exists()


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------

def test_malformed_config_reported_cleanly(harness):
    (harness.root / "config.json").write_text("{not json")
    with pytest.raises(ValueError):
        run_tests.main(["test", "--config", str(harness.root / "config.json")])


def test_malformed_meta_fails(harness, run):
    harness.add_test("bad", src="0;", tokens=RAW_TOKENS)
    (harness.root / "bad" / "meta.json").write_text("nope")
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "FAIL  lexer/bad" in out


# ---------------------------------------------------------------------------
# compiler (compile+run) stage
# ---------------------------------------------------------------------------

def compile_exe(body):
    """Compile-stage cmd that writes an executable printing/doing `body`."""
    src = "import sys;open(sys.argv[1],'w').write(%r)" % (body.rstrip("\n") + "\n")
    return ["python3", "-S", "-c", src, "{exe}"]


def compiler_config(compile_cmd, run_cmd, run_preprocess=None):
    run = {"cmd": run_cmd}
    if run_preprocess:
        run["preprocess"] = run_preprocess
    return {"compile": {"cmd": compile_cmd}, "run": run}


def test_compiler_run_end_to_end_pass(harness, run):
    harness.add_test("hw", src="0;", stdout="hello\n")
    cfg = harness.write_config(
        stages=compiler_config(compile_exe('print("hello")'), ["python3", "{exe}"]))
    rc, out = run(["test", "--config", cfg])
    assert rc == 0 and "PASS  compiler/hw" in out


def test_compiler_stdin_and_run_preprocess(harness, run):
    harness.add_test("hw", src="0;", stdout="WORLD\n")
    harness.write("hw", "stdin", "world\n")
    stages = compiler_config(
        compile_exe("import sys;print(sys.stdin.read().strip())"),
        ["python3", "{exe}"],
        run_preprocess=[{"type": "regex", "pattern": "world", "repl": "WORLD"}],
    )
    cfg = harness.write_config(stages=stages)
    rc, out = run(["test", "--config", cfg])
    assert rc == 0 and "PASS  compiler/hw" in out


def test_compiler_compile_failure(harness, run):
    harness.add_test("cbad", src="// exit: 3\n", meta={"stages": ["compiler"]})
    stages = {"compile": {"cmd": ["{root}/mock_compiler.py", "{input}"]},
              "run": {"cmd": ["python3", "{exe}"]}}
    cfg = harness.write_config(stages=stages)
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "FAIL  compiler/cbad" in out
    assert "compile failed (exit 3)" in out


def test_compiler_tolerance_pass_and_diff_fail(harness, run):
    golden = "1.0 2.0\n"
    stages = compiler_config(compile_exe("print('1.05 1.99')"), ["python3", "{exe}"])
    harness.add_test("hw", src="0;", stdout=golden)
    harness.add_test("hw", src="0;", stdout=golden, meta={"tolerance": 0.1})
    rc, out = run(["test", "--config", harness.write_config(stages=stages)])
    assert rc == 0 and "PASS  compiler/hw" in out
    # Second run with same test but no tolerance in meta -> will fail because
    # the golden differs from actual output. But meta doesn't have tolerance,
    # so we create the test without tolerance meta.
    harness.add_test("hw2", src="0;", stdout=golden)
    rc, out = run(["test", "--config", harness.write_config(stages=stages)])
    assert rc == 1 and "FAIL  compiler/hw2" in out


def test_compiler_exit_contract(harness, run):
    stages = compiler_config(compile_exe("import sys;sys.exit(5)"), ["python3", "{exe}"])
    harness.add_test("hw", src="0;", meta={"exit": "nonzero", "stages": ["compiler"]})
    rc, out = run(["test", "--config", harness.write_config(stages=stages)])
    assert rc == 0 and "PASS  compiler/hw" in out

    # Exact 0 contract with a golden -> fails on exit mismatch
    harness.add_test("hw2", src="0;", stdout="unused\n", meta={"exit": 0})
    rc, out = run(["test", "--config", harness.write_config(stages=stages)])
    assert rc == 1 and "exit: expected 0, got 5" in out


def test_compiler_update_regenerates_and_exit_warning(harness, run):
    stages = compiler_config(compile_exe("import sys;sys.exit(5)"), ["python3", "{exe}"])
    harness.add_test("hw", src="0;", meta={"exit": 0, "stages": ["compiler"]})
    cfg = harness.write_config(stages=stages)
    rc, out = run(["update", "--config", cfg])
    assert rc == 0
    assert "UPD  compiler/hw" in out
    assert "exit contract not met" in out
    golden = harness.root / "hw" / "stdout"
    assert golden.exists()
    before = golden.read_text()
    run(["update", "--config", cfg])
    assert golden.read_text() == before  # idempotent


# ---------------------------------------------------------------------------
# run_plain / CLI branches
# ---------------------------------------------------------------------------

def test_out_defaults_to_stdout_mode(harness, run):
    harness.add_test("a", src="0;", tokens="STDX\n")
    stage = {"cmd": ["python3", "-S", "-c", "print('STDX')"]}  # no "out" key
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["test", "--config", cfg, "--keep", "all"])
    assert rc == 0 and "PASS  lexer/a" in out
    raw = harness.out_dir / "lexer" / "a" / "lexer.raw"
    assert raw.exists() and raw.read_text() == "STDX\n"


def test_stage_out_file_not_produced_fails(harness, run):
    harness.add_test("a", src="0;")
    harness.write("a", "tokens.json", RAW_TOKENS)
    stage = {"cmd": ["{root}/mock_compiler.py", "-t", "{tokens_out}", "{input}"],
             "out": "{ast_out}"}
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "FAIL  lexer/a" in out
    assert "stage wrote no output to" in out


def test_build_subcommand(harness, run):
    # success
    cfg = harness.write_config(stages={}, build="exit 0")
    rc, out = run(["build", "--config", cfg])
    assert rc == 0 and "> exit 0" in out
    # failure
    cfg = harness.write_config(stages={}, build="exit 3")
    rc, _ = run(["build", "--config", cfg])
    assert rc == 1
    # missing build command
    cfg = harness.write_config(stages={})
    rc, out = run(["build", "--config", cfg])
    assert rc == 1 and "no 'build' command" in out


def test_build_failure_stops_test_run(harness, run):
    add_lexer(harness, "a")
    cfg = harness.write_config(stages=lexer_stage(["kind"]), build="exit 3")
    rc, out = run(["test", "--config", cfg])
    assert rc == 1 and "build failed" in out


def test_update_with_fuzz_rejected(harness, run):
    cfg = harness.write_config(stages=lexer_stage(["kind"]), fuzz=make_fuzz_config())
    with pytest.raises(run_tests.StepError, match="cannot be combined"):
        run(["update", "--fuzz", "--config", cfg])


def test_fuzz_zero_tests_warning(harness, run):
    cfg = harness.write_config(stages=lexer_stage(["kind", "line", "column"]), fuzz=make_fuzz_config())
    rc, out = run(["test", "--fuzz", "--fuzz-count", "0", "--config", cfg])
    assert rc == 0
    assert "produced no fuzz tests" in out
    assert "---- fuzz tests ----" not in out


def test_no_tests_selected(harness, run):
    cfg = harness.write_config(stages=lexer_stage(["kind"]))  # no test case dirs
    rc, out = run(["test", "--config", cfg])
    assert rc == 0 and "no tests selected" in out
    rc, out = run(["list", "--config", cfg])
    assert rc == 0 and out.strip() == ""


def test_keep_failed_removes_passing_workdirs(harness, run):
    add_lexer(harness, "ok")
    add_lexer(harness, "bad", src="// exit: 3\n", meta={"exit": 0})
    cfg = harness.write_config(stages=lexer_stage(["kind"]))
    rc, out = run(["test", "--config", cfg, "--keep", "failed"])
    assert rc == 1
    assert (harness.out_dir / "lexer" / "bad").exists()
    assert not (harness.out_dir / "lexer" / "ok").exists()


def test_preprocess_regex_and_exec(harness, run):
    harness.add_test("a", src="0;")
    harness.write("a", "tokens.json", "xabcx\n")
    stage = {
        "cmd": ["python3", "-S", "-c", "print('xabcx')"],  # stdout mode
        "preprocess": [{"type": "regex", "pattern": "x", "repl": "X"}],
    }
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["test", "--config", cfg])
    assert rc == 0 and "PASS  lexer/a" in out


def test_preprocess_regex_delete_lines(harness, run):
    """Regex preprocess without repl deletes matching lines."""
    harness.add_test("a", src="0;")
    # Golden has only the meaningful line
    harness.write("a", "tokens.json", "42\n")
    # Actual output contains metadata lines that should be deleted
    script = "import sys; open(sys.argv[1],'w').write('version=1\\n42\\n')"
    stage = {
        "cmd": ["python3", "-S", "-c", script, "{tokens_out}"],
        "out": "{tokens_out}",
        "preprocess": [{"type": "regex", "pattern": "version=.*"}],
    }
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["test", "--config", cfg])
    assert rc == 0 and "PASS  lexer/a" in out


def test_malformed_preprocess_exec_fails(harness, run):
    harness.add_test("a", src="0;")
    harness.write("a", "tokens.json", RAW_TOKENS)
    stage = {"cmd": ["{root}/mock_compiler.py", "-t", "{tokens_out}", "{input}"],
             "out": "{tokens_out}",
             "preprocess": [{"type": "exec", "cmd": "not-a-list"}]}
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "must be a list" in out


def test_preprocess_json_drop(harness, run):
    """End-to-end test of json drop preprocessing."""
    # The mock compiler always writes [IDENT, SEMICOLON]; after dropping `value`:
    tokens_no_value = json.dumps([
        {"kind": "IDENT", "line": 1, "column": 1},
        {"kind": "SEMICOLON", "line": 1, "column": 2},
    ], indent=2)
    harness.add_test("a", src="0;", tokens=tokens_no_value)
    stage = {
        "cmd": ["{root}/mock_compiler.py", "-t", "{tokens_out}", "{input}"],
        "out": "{tokens_out}",
        "preprocess": [{"type": "json", "drop": ["value"]}],
    }
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["test", "--config", cfg])
    assert rc == 0
    assert "PASS  lexer/a" in out


def test_preprocess_llvm_step(harness, run):
    """End-to-end test of llvm canonicalization preprocessing."""
    # normalize_llvm starts at v0 for unnamed values; block labels consume v0 first
    golden_ll = "%v0 = alloca i32\n"  # after llvm normalize
    harness.add_test("ll", src="0;", out_ll=golden_ll)
    # A script that writes raw LLVM with unnamed values
    script = "import sys; open(sys.argv[1],'w').write('%1 = alloca i32\\n')"
    stage = {
        "cmd": ["python3", "-S", "-c", script, "{llvm_out}"],
        "out": "{llvm_out}",
        "preprocess": [{"type": "llvm"}],
    }
    cfg = harness.write_config(stages={"llvm": stage})
    rc, out = run(["test", "--config", cfg, "--check-ir"])
    assert rc == 0
    assert "PASS  llvm/ll" in out


def test_preprocess_json_no_keep_drop_rejected(harness, run):
    """Json preprocess step without keep or drop is rejected."""
    harness.add_test("a", src="0;", tokens=RAW_TOKENS)
    stage = {
        "cmd": ["{root}/mock_compiler.py", "-t", "{tokens_out}", "{input}"],
        "out": "{tokens_out}",
        "preprocess": [{"type": "json"}],
    }
    cfg = harness.write_config(stages={"lexer": stage})
    rc, out = run(["test", "--config", cfg])
    assert rc == 1
    assert "at least one of 'keep' or 'drop'" in out