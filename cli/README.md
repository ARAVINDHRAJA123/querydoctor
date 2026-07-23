# QueryDoctor CLI

Local, offline batch linting with QueryDoctor's rule engine — no server, no
network call, no API key. Same 34 lint checks, syntax diagnosis, and
auto-fix as the [web app](https://querydoctor-616665622891.asia-south1.run.app)
and the [GitHub Action](../action.yml), for pre-commit hooks or local CI
parity.

## Install

Not published to PyPI yet — install from this checkout:

```bash
pip install -e cli/
```

(An editable install works from anywhere; a non-editable `pip install cli/`
also works since `lint_engine.py` ships vendored inside the package — see
[`sync_vendor.sh`](sync_vendor.sh) if you're touching this repo's source and
need to keep that copy current. Not relevant if you're just installing it.)

## Usage

```bash
querydoctor path/to/*.sql
querydoctor models/**/*.sql --dialect postgres
cat query.sql | querydoctor -
querydoctor query.sql --schema schema.json      # {"table": ["col", ...]}
querydoctor query.sql --ddl schema.sql          # real CREATE TABLE statements
querydoctor query.sql --json                    # machine-readable output
querydoctor query.sql --fix                     # writes safe auto-fixes back to the file
querydoctor query.sql --fail-on medium           # exit non-zero at medium+ too (default: high)
```

Exit code is non-zero if any file fails to parse, or has a finding at or
above `--fail-on`'s threshold (default `high`) — wire it into a pre-commit
hook or a local CI step the same way you'd use the GitHub Action, without
needing a GitHub PR to trigger it.

`--fix` is opt-in and only ever applies the same small set of
zero-ambiguity fixes the API offers (`x = NULL` → `IS NULL`, etc.) — see
the main [README](../README.md#auto-fix). Without `--fix`, nothing you run
this against is ever modified.
