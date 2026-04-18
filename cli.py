"""
cli.py
Single entry point for the qa-agent pipeline.

Usage:
  python cli.py --spec specs/restful_booker.yaml --generate
  python cli.py --spec specs/restful_booker.yaml --generate --limit 3
  python cli.py --run
  python cli.py --heal
  python cli.py --spec specs/restful_booker.yaml --all
  python cli.py --spec specs/petstore.yaml --generate --base-url https://petstore3.swagger.io
"""

from __future__ import annotations

import argparse
import sys
import warnings

from agent import logger
from agent.dependency_resolver import get_chained_endpoints
from agent.conftest_generator import generate_conftest


warnings.filterwarnings("ignore", category=UserWarning)

from rich.console import Console
from rich.panel import Panel

from agent.spec_parser import extract_endpoints, load_spec, print_summary
from agent.api_prober import probe_all_endpoints, set_spec
from agent.template_generator import generate_tests_from_template, save_generated_test
from agent.test_runner import run_tests, print_failures
from agent.logger import get_cli_logger

logger = get_cli_logger()
console = Console()

BANNER = """
[bold cyan]
  ██████   █████        █████████    █████████   ██████████ ██████   █████ ███████████
 ███░░███ ░░███        ███░░░░░███  ███░░░░░███ ░░███░░░░░█░░██████ ░░███ ░█░░░███░░░█
░███ ░███  ░███       ░███    ░███ ░███    ░███  ░███  █ ░  ░███░███ ░███ ░   ░███  ░
░███ ░███  ░███       ░███████████ ░███████████  ░██████    ░███░░███░███     ░███
░███ ░███  ░███       ░███░░░░░███ ░███░░░░░███  ░███░░█    ░███ ░░██████     ░███
░░███████  ░███      █░███    ░███ ░███    ░███  ░███ ░   █ ░███  ░░█████     ░███
 ░░░░░███  ███████████░███    ░███ ░███    ░███  ██████████ █████  ░░█████    █████
  █████░  ░░░░░░░░░░░ ░░░     ░░░  ░░░     ░░░  ░░░░░░░░░░ ░░░░░    ░░░░░    ░░░░░
[/bold cyan]
[dim]  AI-powered QA Automation Agent — generates, runs, and self-heals Pytest tests[/dim]
"""


def cmd_generate(
    spec_path: str,
    output_dir: str,
    limit: int | None,
    base_url_override: str | None = None,
    force: bool = False,
) -> None:
    """Generate Pytest tests from an OpenAPI spec using the template engine."""
    console.print(Panel(
        f"[bold]Generating tests from:[/bold] {spec_path}",
        style="cyan"
    ))

    spec = load_spec(spec_path)
    endpoints = extract_endpoints(spec)
    print_summary(endpoints)

    for ep in endpoints:
        if base_url_override:
            ep["base_url"] = base_url_override.rstrip("/")
        elif not ep.get("base_url"):
            console.print(
                "[red]✗ No base URL found in spec.\n"
                "  Use --base-url https://your-api.com to specify one.[/red]"
            )
            return

    if limit:
        endpoints = endpoints[:limit]
        console.print(f"[dim]Limited to first {limit} endpoints[/dim]")

    # Probe all endpoints
    probed = probe_all_endpoints(endpoints, spec=spec)

    # Detect chains and build fixture mapping
    fixture_mapping = get_chained_endpoints(probed)
    if fixture_mapping:
        console.print(
            f"\n[cyan]Detected {len(set(fixture_mapping.values()))} "
            f"resource chain(s) — generating stateful fixtures[/cyan]"
        )

    # Generate conftest.py with session fixtures
    base_url = probed[0].get("base_url", "") if probed else ""
    conftest_path = generate_conftest(probed, output_dir=output_dir, base_url=base_url)
    if conftest_path:
        console.print(f"[green]✓ Generated {conftest_path}[/green]")

    console.print(f"\n[cyan]Generating tests for {len(probed)} endpoint(s)...[/cyan]\n")

    generated = []
    skipped = []
    failed = []

    for ep in probed:
        label = f"{ep['method']} {ep['path']}"
        console.print(f"  [dim]→ {label}[/dim]", end=" ")

        valid = ep.get("valid_response") or {}
        if valid.get("error"):
            console.print(f"[yellow]⚠ Skipped — probe failed: {valid['error']}[/yellow]")
            skipped.append(ep)
            continue

        try:
            op_id = ep.get("operation_id", "")
            fixture_name = fixture_mapping.get(op_id, "")
            code = generate_tests_from_template(ep, fixture_name=fixture_name)
            saved = save_generated_test(ep, code, output_dir, force=force)
            generated.append(saved)
            console.print(f"[green]✓ {saved.name}[/green]")
        except FileExistsError as e:
            console.print(f"[yellow]⚠ Skipped — {e}[/yellow]")
            skipped.append(ep)
        except Exception as e:
            console.print(f"[red]✗ {e}[/red]")
            failed.append(ep)

    console.print(
        f"\n[bold green]✓ Generated: {len(generated)}[/bold green]  "
        f"[yellow]⚠ Skipped: {len(skipped)}[/yellow]  "
        f"[red]✗ Failed: {len(failed)}[/red]"
    )


def cmd_run(test_dir: str) -> int:
    """Run all generated tests and report results. Returns exit code."""
    console.print(Panel("[bold]Running generated tests[/bold]", style="cyan"))
    passed, failed, failures = run_tests(test_dir)
    print_failures(failures)
    return 0 if failed == 0 else 1


def cmd_heal(test_dir: str) -> None:
    """Run tests and heal any failures."""
    from agent.self_healer import heal_all  # lazy import — LangChain only needed locally
    console.print(Panel("[bold]Running self-healer[/bold]", style="yellow"))
    heal_all(test_dir)


def cmd_all(
    spec_path: str,
    output_dir: str,
    limit: int | None,
    base_url_override: str | None = None,
    force: bool = False,
) -> None:
    """Full pipeline: generate → run → heal."""
    console.print(Panel(
        f"[bold]Full pipeline[/bold]\n"
        f"Spec:   {spec_path}\n"
        f"Output: {output_dir}",
        style="cyan"
    ))

    # Step 1 — Generate
    cmd_generate(spec_path, output_dir, limit, base_url_override, force)

    # Step 2 — Run
    console.print()
    passed, failed_count, failures = run_tests(output_dir)

    if failed_count == 0:
        console.print("[bold green]✓ All tests passing — no healing needed![/bold green]")
        return

    # Step 3 — Heal
    from agent.self_healer import heal_all  # lazy import — LangChain only needed locally
    console.print(
        f"\n[yellow]{failed_count} failure(s) detected — starting self-healer...[/yellow]"
    )
    heal_all(output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="qa-agent: AI-powered QA automation agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cli.py --spec specs/restful_booker.yaml --generate
  python cli.py --spec specs/restful_booker.yaml --generate --limit 3
  python cli.py --spec specs/petstore.yaml --generate --base-url https://petstore3.swagger.io
  python cli.py --run
  python cli.py --heal
  python cli.py --spec specs/restful_booker.yaml --all
  python cli.py --spec specs/restful_booker.yaml --all --force
        """
    )

    parser.add_argument("--spec",     type=str,            help="Path to OpenAPI spec (YAML or JSON)")
    parser.add_argument("--generate", action="store_true", help="Generate tests from spec")
    parser.add_argument("--run",      action="store_true", help="Run generated tests")
    parser.add_argument("--heal",     action="store_true", help="Run tests and heal failures")
    parser.add_argument("--all",      action="store_true", help="Full pipeline: generate + run + heal")
    parser.add_argument("--output",   type=str, default="generated_tests", help="Output directory for tests")
    parser.add_argument("--limit",    type=int, default=None,              help="Limit number of endpoints")
    parser.add_argument("--base-url", type=str, default=None,              help="Override base URL when spec does not define one")
    parser.add_argument("--force",    action="store_true",                 help="Overwrite existing generated tests")

    args = parser.parse_args()

    # Print banner
    console.print(BANNER)
    logger.info("=== qa-agent CLI started ===")
    logger.info(f"Args: {vars(args)}")


    # Validate
    if args.generate and not args.spec:
        console.print("[red]--generate requires --spec[/red]")
        sys.exit(1)
    if args.all and not args.spec:
        console.print("[red]--all requires --spec[/red]")
        sys.exit(1)
    if not any([args.generate, args.run, args.heal, args.all]):
        parser.print_help()
        sys.exit(0)

    # Dispatch
    if args.generate:
        cmd_generate(args.spec, args.output, args.limit, args.base_url, args.force)
    elif args.run:
        exit_code = cmd_run(args.output)
        sys.exit(exit_code)
    elif args.heal:
        cmd_heal(args.output)
    elif args.all:
        cmd_all(args.spec, args.output, args.limit, args.base_url, args.force)
    
    logger.info("=== qa-agent CLI finished ===")


if __name__ == "__main__":
    main()