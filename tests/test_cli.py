"""QueryDoctor CLI tests — exercises cli/querydoctor_cli.py as a real subprocess,
not just importing it, since exit codes and stdin/file I/O are the actual contract."""
import json
import os
import subprocess
import sys
import tempfile

CLI = os.path.join(os.path.dirname(__file__), "..", "cli", "querydoctor_cli.py")


def run_cli(args, input_text=None):
    return subprocess.run(
        [sys.executable, CLI] + args,
        input=input_text, capture_output=True, text=True,
    )


def write_tmp(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False)
    f.write(content)
    f.close()
    return f.name


def test_clean_file_exits_zero():
    path = write_tmp("SELECT 1")
    try:
        r = run_cli([path])
        assert r.returncode == 0
        assert "clean, no findings" in r.stdout
    finally:
        os.unlink(path)


def test_high_severity_finding_exits_nonzero_by_default():
    path = write_tmp("SELECT * FROM t WHERE x = NULL")
    try:
        r = run_cli([path])
        assert r.returncode == 1
        assert "never matches" in r.stdout
    finally:
        os.unlink(path)


def test_fail_on_never_always_exits_zero():
    path = write_tmp("SELECT * FROM t WHERE x = NULL")
    try:
        r = run_cli([path, "--fail-on", "never"])
        assert r.returncode == 0
    finally:
        os.unlink(path)


def test_syntax_error_exits_nonzero_regardless_of_fail_on():
    path = write_tmp("SELCT FROM")
    try:
        r = run_cli([path, "--fail-on", "never"])
        assert r.returncode == 1
        assert "SYNTAX ERROR" in r.stdout
    finally:
        os.unlink(path)


def test_stdin_mode():
    r = run_cli(["-"], input_text="SELECT 1")
    assert r.returncode == 0
    assert "clean" in r.stdout


def test_json_mode_produces_valid_json():
    path = write_tmp("SELECT 1")
    try:
        r = run_cli([path, "--json"])
        parsed = json.loads(r.stdout)
        assert parsed["valid"] is True
        assert parsed["file"] == path
    finally:
        os.unlink(path)


def test_fix_flag_writes_file_in_place():
    path = write_tmp("SELECT * FROM t WHERE x = NULL")
    try:
        r = run_cli([path, "--fix"])
        assert "fixed and wrote" in r.stdout
        with open(path) as f:
            fixed_content = f.read()
        assert "IS NULL" in fixed_content
        assert "= NULL" not in fixed_content
    finally:
        os.unlink(path)


def test_no_fix_flag_never_modifies_the_file():
    original = "SELECT * FROM t WHERE x = NULL"
    path = write_tmp(original)
    try:
        run_cli([path])
        with open(path) as f:
            assert f.read() == original
    finally:
        os.unlink(path)


def test_missing_file_reports_error_and_nonzero_exit():
    r = run_cli(["/tmp/definitely_does_not_exist_qd.sql"])
    assert r.returncode != 0
    assert "error" in r.stderr.lower() or "error" in r.stdout.lower()


def test_malformed_schema_json_fails_gracefully():
    sql_path = write_tmp("SELECT 1")
    schema_path = write_tmp("not valid json")
    try:
        r = run_cli([sql_path, "--schema", schema_path])
        assert r.returncode == 2
        assert "error" in r.stderr.lower()
    finally:
        os.unlink(sql_path)
        os.unlink(schema_path)


def test_ddl_flag_catches_unknown_column():
    ddl_path = write_tmp("CREATE TABLE employees (employee_id INT, salary DECIMAL);")
    sql_path = write_tmp("SELECT e.employee_id, e.bogus FROM employees e")
    try:
        r = run_cli([sql_path, "--ddl", ddl_path])
        assert r.returncode == 1
        assert "Unknown column" in r.stdout
    finally:
        os.unlink(ddl_path)
        os.unlink(sql_path)
