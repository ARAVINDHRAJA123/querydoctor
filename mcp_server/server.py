"""QueryDoctor MCP server — exposes the same zero-LLM sqlglot engine that
powers the web app and GitHub Action as MCP tools, so any MCP client
(Claude Code, Claude Desktop, Cursor, etc.) can lint/format/translate SQL
mid-conversation. No network calls, no credentials, nothing stored."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from mcp.server.fastmcp import FastMCP

from lint_engine import DIALECTS, check_sql

mcp = FastMCP("querydoctor")


@mcp.tool()
def lint_sql(sql: str, dialect: str = "bigquery", target_dialect: str | None = None, dbt_mode: bool = False) -> dict:
    """Diagnose a SQL query: syntax check, health score (0-100), lint
    findings with plain-English explanations, auto-formatted SQL, a
    deterministic optimized rewrite, and optional translation to another
    dialect. Set dbt_mode=True to strip {{ ref(...) }}/{{ source(...) }}
    dbt/Jinja templating before linting.

    dialect/target_dialect: one of bigquery, postgres, mysql, snowflake,
    spark, sqlite, tsql, oracle, duckdb, redshift.
    """
    return check_sql(sql, dialect=dialect, target_dialect=target_dialect, dbt_mode=dbt_mode)


@mcp.tool()
def list_dialects() -> list[str]:
    """List the SQL dialects QueryDoctor supports for linting and translation."""
    return DIALECTS


def main():
    mcp.run()


if __name__ == "__main__":
    main()
