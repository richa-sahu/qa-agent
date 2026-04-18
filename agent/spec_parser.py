"""
spec_parser.py
Parses an OpenAPI 3.x or 2.x (Swagger) spec (JSON or YAML) into a list
of EndpointInfo dicts. Each dict is self-contained — includes base_url,
method, path, params, request body schema, and expected responses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

console = Console()


def load_spec(spec_path: str) -> dict:
    """Load a JSON or YAML OpenAPI spec from disk."""
    path = Path(spec_path)
    if not path.exists():
        raise FileNotFoundError(f"Spec not found: {spec_path}")

    raw = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(raw)
    return json.loads(raw)


def _resolve_base_url(spec: dict) -> str:
    """
    Resolve base URL from spec — handles OpenAPI 3.x and 2.x (Swagger).
    Returns empty string if no valid URL found, with a clear warning.

    Priority:
      1. OpenAPI 3.x servers[0].url (must be a full https:// URL)
      2. OpenAPI 2.x host + basePath + schemes
      3. Empty string — caller must handle with --base-url override
    """
    # OpenAPI 3.x — servers array
    servers = spec.get("servers", [])
    if servers:
        url = servers[0].get("url", "").rstrip("/")
        if url.startswith("http://") or url.startswith("https://"):
            return url
        # Relative URL like /api/v3 — not usable without a host
        if url:
            console.print(
                f"[yellow]⚠ Spec defines a relative server URL '{url}' "
                f"— cannot use without a host.[/yellow]"
            )

    # OpenAPI 2.x (Swagger) — host + basePath + schemes
    host = spec.get("host", "")
    base_path = spec.get("basePath", "").rstrip("/")
    schemes = spec.get("schemes", ["https"])
    scheme = schemes[0] if schemes else "https"
    if host:
        return f"{scheme}://{host}{base_path}"

    # Nothing found
    console.print(
        "[bold yellow]⚠ Warning: No base URL found in spec.[/bold yellow]\n"
        "  For OpenAPI 3.x add a servers section:\n"
        "    servers:\n"
        "      - url: https://your-api.com\n"
        "  Or pass --base-url when running cli.py\n"
        "  Or pass base_url as 3rd arg to template_generator"
    )
    return ""


def extract_endpoints(spec: dict) -> list[dict[str, Any]]:
    """
    Walk the spec paths and return a flat list of endpoint descriptors.

    Each descriptor contains everything needed to probe and test:
      - method, path, operation_id, summary
      - base_url (resolved from spec)
      - path_params, query_params
      - request_body schema (if any)
      - expected status codes + descriptions
    """
    base_url = _resolve_base_url(spec)
    endpoints: list[dict] = []

    for path, path_item in spec.get("paths", {}).items():
        # Collect path-level params (inherited by all methods)
        path_level_params = path_item.get("parameters", [])

        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not operation:
                continue

            # Merge path-level + operation-level params
            all_params = path_level_params + operation.get("parameters", [])

            path_params = [
                {"name": p["name"], "schema": p.get("schema", {})}
                for p in all_params
                if p.get("in") == "path"
            ]
            query_params = [
                {
                    "name": p["name"],
                    "required": p.get("required", False),
                    "schema": p.get("schema", {}),
                }
                for p in all_params
                if p.get("in") == "query"
            ]

            # Request body
            request_body = None
            if "requestBody" in operation:
                content = operation["requestBody"].get("content", {})
                for media_type, media_obj in content.items():
                    request_body = {
                        "media_type": media_type,
                        "schema": media_obj.get("schema", {}),
                        "required": operation["requestBody"].get("required", False),
                    }
                    break  # take first content type

            # Responses
            responses = []
            for status_code, resp_obj in operation.get("responses", {}).items():
                responses.append(
                    {
                        "status_code": status_code,
                        "description": resp_obj.get("description", ""),
                    }
                )

            endpoints.append(
                {
                    "base_url": base_url,
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation.get(
                        "operationId", f"{method}_{path}"
                    ),
                    "summary": operation.get("summary", ""),
                    "path_params": path_params,
                    "query_params": query_params,
                    "request_body": request_body,
                    "responses": responses,
                }
            )

    return endpoints


def print_summary(endpoints: list[dict]) -> None:
    """Pretty-print a Rich table of all parsed endpoints."""
    table = Table(title="Parsed API Endpoints", show_lines=True)
    table.add_column("Method", style="bold cyan", width=8)
    table.add_column("Path", style="white")
    table.add_column("Operation ID", style="yellow")
    table.add_column("Summary", style="dim")
    table.add_column("Base URL", style="green")

    for ep in endpoints:
        table.add_row(
            ep["method"],
            ep["path"],
            ep["operation_id"],
            ep["summary"],
            ep["base_url"] or "[red]missing[/red]",
        )

    console.print(table)


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    spec_file = sys.argv[1] if len(sys.argv) > 1 else "specs/restful_booker.yaml"
    spec = load_spec(spec_file)
    endpoints = extract_endpoints(spec)
    print_summary(endpoints)
    console.print(
        f"\n[bold green]✓ Parsed {len(endpoints)} endpoints[/bold green]"
    )