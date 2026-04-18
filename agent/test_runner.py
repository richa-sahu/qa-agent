"""
test_runner.py
Runs pytest on generated_tests/ and returns structured failure data
ready for the self-healer to consume.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)

from rich.console import Console
from rich.table import Table
from agent.logger import get_runner_logger

console = Console()
logger = get_runner_logger()


@dataclass
class FailedTest:
    """Structured representation of a single test failure."""
    test_id: str          # e.g. generated_tests/test_createToken.py::test_createToken_edgeCase
    file_path: str        # absolute path to the test file
    test_name: str        # just the function name
    error_message: str    # assertion error or exception message
    traceback: str        # full traceback string
    source_code: str      # full content of the test file (for self-healer context)


def run_tests(test_dir: str = "generated_tests") -> tuple[int, int, list[FailedTest]]:
    """
    Run pytest on test_dir and return (passed, failed, list_of_failures).

    Uses pytest-json-report to capture structured output without
    relying on stdout parsing.
    """
    logger.info(f"Running tests in: {test_dir}")
    test_path = Path(test_dir)
    if not test_path.exists():
        raise FileNotFoundError(f"Test directory not found: {test_dir}")

    # Write JSON report to a temp file so we don't pollute the workspace
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report_path = tmp.name

    cmd = [
        "python", "-m", "pytest",
        str(test_path),
        "--json-report",
        f"--json-report-file={report_path}",
        "--tb=short",
        "-q",
    ]

    console.print(f"\n[bold cyan]▶ Running tests in:[/bold cyan] {test_dir}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Parse the JSON report
    report_file = Path(report_path)
    if not report_file.exists():
        console.print("[red]✗ JSON report not generated — pytest may have crashed[/red]")
        console.print(result.stdout)
        console.print(result.stderr)
        return 0, 0, []

    report = json.loads(report_file.read_text())
    report_file.unlink()  # clean up temp file

    # Extract summary
    summary = report.get("summary", {})
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    total = summary.get("total", 0)

    console.print(
        f"[green]✓ Passed: {passed}[/green]  "
        f"[red]✗ Failed: {failed}[/red]  "
        f"Total: {total}"
    )

    # Build FailedTest objects
    failures: list[FailedTest] = []
    for test in report.get("tests", []):
        if test.get("outcome") != "failed":
            continue

        node_id = test.get("nodeid", "")
        # nodeid format: "generated_tests/test_foo.py::test_bar"
        file_part = node_id.split("::")[0] if "::" in node_id else node_id
        test_name = node_id.split("::")[-1] if "::" in node_id else node_id

        # Extract error details from the call phase
        call = test.get("call", {})
        longrepr = call.get("longrepr", "")  # full traceback string

        # Pull just the final assertion/error line
        error_lines = [l for l in longrepr.splitlines() if l.strip().startswith("E ")]
        error_message = "\n".join(error_lines) if error_lines else longrepr[:300]

        # Read source file for self-healer context
        source_code = ""
        source_path = Path(file_part)
        if source_path.exists():
            source_code = source_path.read_text(encoding="utf-8")

        failures.append(
            FailedTest(
                test_id=node_id,
                file_path=str(source_path.resolve()),
                test_name=test_name,
                error_message=error_message,
                traceback=longrepr,
                source_code=source_code,
            )
        )

    logger.info(f"Results — passed: {passed}, failed: {failed}, total: {total}")
    for f in failures:
        logger.warning(f"FAILED: {f.test_id}\n{f.error_message}")

    return passed, failed, failures


def print_failures(failures: list[FailedTest]) -> None:
    """Pretty-print a summary table of failures."""
    if not failures:
        console.print("[bold green]✓ No failures![/bold green]")
        return

    table = Table(title="Failed Tests", show_lines=True)
    table.add_column("Test", style="red")
    table.add_column("Error", style="yellow", max_width=60)

    for f in failures:
        table.add_row(f.test_name, f.error_message)

    console.print(table)

    # Also print full tracebacks for debugging
    for f in failures:
        console.print(f"\n[bold red]TRACEBACK — {f.test_name}[/bold red]")
        console.print(f.traceback)


# ── Smoke test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    passed, failed, failures = run_tests("generated_tests")
    print_failures(failures)

    if failures:
        console.print(f"\n[yellow]→ {len(failures)} failure(s) ready for self-healer[/yellow]")
    else:
        console.print("\n[bold green]✓ All tests passing — nothing to heal[/bold green]")