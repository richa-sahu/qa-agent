"""
template_generator.py
Generates Pytest test files from a Jinja2 template using real probed
API responses. 100% deterministic — no LLM involved.
Generic — works for any REST API including those with $ref schemas.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)

from jinja2 import Environment, FileSystemLoader
from rich.console import Console
from rich.syntax import Syntax

# Use api_prober's payload builder — single source of truth, handles $ref
from agent.api_prober import _extract_example_payload, set_spec
from agent.logger import get_generator_logger
from agent.dependency_resolver import get_chained_endpoints

console = Console()
logger = get_generator_logger()


# Path to templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_top_keys(body) -> list[str]:
    """Extract top-level keys from a response body."""
    if isinstance(body, dict):
        return list(body.keys())
    if isinstance(body, list) and len(body) > 0 and isinstance(body[0], dict):
        return list(body[0].keys())
    return []


def _build_example_payload(request_body: dict | None) -> dict:
    """
    Build example payload from request body schema.
    Delegates to api_prober._extract_example_payload() which handles $ref.
    """
    if not request_body:
        return {}
    schema = request_body.get("schema", {})
    payload = _extract_example_payload(schema) or {}
    logger.debug(f"Built example payload: {json.dumps(payload)}")
    return payload


def _build_example_query_params(query_params: list[dict]) -> dict:
    params = {}

    for p in query_params:
        schema = p.get("schema", {})
        enum = schema.get("enum")

        if enum:
            params[p["name"]] = enum[0]   # ✅ MATCH PROBER
        elif schema.get("type") == "integer":
            params[p["name"]] = 1
        else:
            params[p["name"]] = "test"

    return params


def _build_invalid_path(path: str, path_params: list[dict]) -> str:
    """Build a path with an invalid ID to trigger 404."""
    result = path
    for param in path_params:
        result = result.replace(f"{{{param['name']}}}", "999999999")
    return result


def _build_valid_path(path: str, path_params: list[dict]) -> str:
    """Build a path with valid default values."""
    result = path
    for param in path_params:
        schema = param.get("schema", {})
        default = "1" if schema.get("type") == "integer" else "test"
        result = result.replace(f"{{{param['name']}}}", default)
    return result


def _sanitize_text_body(body: str) -> str:
    """
    Sanitize plain text response body for use in a Python string assertion.
    - If HTML: return empty string (template will use len() check instead)
    - Otherwise: take first line, escape quotes
    """
    if not body:
        return ""
    if body.strip().startswith("<"):
        return ""
    first_line = body.strip().splitlines()[0]
    return first_line.replace('"', '\\"').replace("'", "\\'")


# ── Context builder ───────────────────────────────────────────────────────────

def build_template_context(endpoint: dict, fixture_name: str = "") -> dict:
    """
    Build the full Jinja2 template context from a probed endpoint descriptor.
    All values derived from real API responses — nothing invented.
    """
    operation_id = endpoint.get("operation_id", "unknown")
    method = endpoint.get("method", "GET").upper()
    path = endpoint.get("path", "/")
    logger.info(f"Building template context for {method} {path} ({operation_id})")


    valid = endpoint.get("valid_response") or {}
    invalid = endpoint.get("invalid_response")


    valid_body = valid.get("body")
    valid_is_json = valid.get("is_json", False)
    valid_top_keys = _get_top_keys(valid_body)
    valid_body_is_list = isinstance(valid_body, list)

    invalid_body = invalid.get("body") if invalid else None
    invalid_is_json = invalid.get("is_json", False) if invalid else False
    invalid_top_keys = _get_top_keys(invalid_body) if invalid else []

    valid_body_text = _sanitize_text_body(
        valid_body if isinstance(valid_body, str) else ""
    )
    invalid_body_text = _sanitize_text_body(
        invalid_body if isinstance(invalid_body, str) else ""
    )

    path_params = endpoint.get("path_params", [])
    valid_path = _build_valid_path(endpoint.get("path", "/"), path_params)
    invalid_path = _build_invalid_path(endpoint.get("path", "/"), path_params)

    if fixture_name:
        import re
        # Replace numeric path segments with fixture variable
        # e.g. /booking/1 → /booking/{created_booking}
        rendered_path = re.sub(r"/\d+", f"/{{{fixture_name}}}", valid_path)
        rendered_invalid_path = invalid_path  # keep 999999999 for invalid
    else:
        rendered_path = valid_path
        rendered_invalid_path = invalid_path

    # Build example payload using the $ref-aware builder from api_prober
    example_payload = _build_example_payload(endpoint.get("request_body"))

    logger.debug(
        f"Context for {operation_id} — "
        f"valid_status={valid.get('status_code')} "
        f"valid_is_json={valid_is_json} "
        f"valid_top_keys={valid_top_keys} "
        f"invalid_status={invalid.get('status_code') if invalid else None}"
    )

    return {
        # Endpoint metadata
        "operation_id": endpoint.get("operation_id", "unknown"),
        "method": endpoint.get("method", "GET").upper(),
        "path": _build_valid_path(endpoint.get("path", "/"), path_params),
        "invalid_path": _build_invalid_path(endpoint.get("path", "/"), path_params),
        "base_url": endpoint.get("base_url", ""),
        "summary": endpoint.get("summary") or f"Tests for {endpoint.get('method')} {endpoint.get('path')}",
        "query_params": endpoint.get("query_params", []),
        "example_query_params": _build_example_query_params(
            endpoint.get("query_params", [])
        ),
        "example_payload": example_payload,
        "invalid_payload": {"__invalid__": True},

        # Valid response
        "valid_status": valid.get("status_code", 200),
        "valid_is_json": valid_is_json,
        "valid_top_keys": valid_top_keys,
        "valid_body_is_list": valid_body_is_list,
        "valid_body_text": valid_body_text,
        "fixture_name": fixture_name,
        "path": rendered_path,
        
        # Invalid response
        "invalid_response": invalid,
        "invalid_status": invalid.get("status_code") if invalid else None,
        "invalid_is_json": invalid_is_json,
        "invalid_top_keys": invalid_top_keys,
        "invalid_body_text": invalid_body_text,
        "invalid_path": rendered_invalid_path,
    }


# ── Template renderer ─────────────────────────────────────────────────────────

def generate_tests_from_template(endpoint: dict, fixture_name: str = "") -> str:
    """Generate a complete Pytest test file from the Jinja2 template."""
    operation_id = endpoint.get("operation_id", "unknown")
    method = endpoint.get("method", "GET")
    path = endpoint.get("path", "/")

    logger.info(f"Generating tests — {method} {path} ({operation_id})")
    if fixture_name:
        logger.info(f"Using fixture: {fixture_name} for {operation_id}")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("test_template.py.j2")
    context = build_template_context(endpoint, fixture_name=fixture_name)
    code = template.render(**context)
    logger.debug(f"Generated {len(code.splitlines())} lines for {operation_id}")
    return code


def save_generated_test(
    endpoint: dict,
    code: str,
    output_dir: str = "generated_tests",
    force: bool = False,
) -> Path:
    """
    Write generated test to disk.
    Raises FileExistsError if file exists and force=False.
    """
    out = Path(output_dir)
    out.mkdir(exist_ok=True)

    op_id = endpoint.get("operation_id", "unknown").replace("/", "_")
    file_path = out / f"test_{op_id}.py"

    if file_path.exists() and not force:
        logger.warning(f"Skipped — file exists: {file_path.name} (use force=True to overwrite)")
        raise FileExistsError(
            f"{file_path.name} already exists. Use --force to overwrite."
        )

    file_path.write_text(code, encoding="utf-8")
    logger.info(f"Saved test file → {file_path}")
    return file_path


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from agent.spec_parser import extract_endpoints, load_spec
    from agent.api_prober import probe_all_endpoints

    spec_file = sys.argv[1] if len(sys.argv) > 1 else "specs/restful_booker.yaml"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    base_url_override = sys.argv[3] if len(sys.argv) > 3 else None

    logger.info(f"template_generator smoke test — spec={spec_file} limit={limit}")

    spec = load_spec(spec_file)
    endpoints = extract_endpoints(spec)

    if limit:
        endpoints = endpoints[:limit]

    for ep in endpoints:
        if base_url_override:
            ep["base_url"] = base_url_override.rstrip("/")
        elif not ep.get("base_url"):
            console.print(
                "[red]✗ No base URL found in spec. "
                "Pass it as 3rd argument:\n"
                "  python -m agent.template_generator <spec> <limit> <base_url>[/red]"
            )
            sys.exit(1)

    # probe_all_endpoints registers spec internally via set_spec()
    probed = probe_all_endpoints(endpoints, spec=spec)

    for ep in probed:
        console.print(
            f"\n[bold cyan]Generating:[/bold cyan] "
            f"{ep['method']} {ep['path']}"
        )
        code = generate_tests_from_template(ep)
        syntax = Syntax(code, "python", theme="monokai", line_numbers=True)
        console.print(syntax)
        saved = save_generated_test(ep, code, force=True)
        console.print(f"[green]✓ Saved → {saved}[/green]")

    logger.info("template_generator smoke test complete")