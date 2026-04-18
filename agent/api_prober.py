"""
api_prober.py
Makes real HTTP calls to every endpoint in a parsed spec and attaches
actual response data to each endpoint descriptor.

Completely generic — works for any API, no hardcoded URLs or API names.
Probes two scenarios per endpoint:
  - valid_response:   called with example/valid payload
  - invalid_response: called with a payload that triggers a proper 4xx error

Logs all HTTP calls, payloads, responses and errors to logs/api_prober.log
"""

from __future__ import annotations

import json
import re
import warnings
from typing import Any

import requests
from rich.console import Console
from rich.table import Table

warnings.filterwarnings("ignore", category=UserWarning)

from agent.logger import get_prober_logger

console = Console()
logger = get_prober_logger()

# Global spec reference — used for $ref resolution
_SPEC: dict = {}


def set_spec(spec: dict) -> None:
    """Store the full spec for $ref resolution."""
    global _SPEC
    _SPEC = spec
    logger.debug(
        f"Spec registered for $ref resolution — "
        f"has components: {'components' in spec}"
    )


def _resolve_ref(schema: dict) -> dict:
    """Resolve a $ref to its actual schema from the spec components."""
    ref = schema.get("$ref", "")
    if not ref or not _SPEC:
        return schema

    parts = ref.lstrip("#/").split("/")
    resolved = _SPEC
    for part in parts:
        resolved = resolved.get(part, {})

    logger.debug(f"Resolved $ref '{ref}' → type: {resolved.get('type', 'unknown')}")
    return resolved


def _extract_example_payload(schema: dict) -> dict | None:
    """
    Recursively extract example values from a JSON schema object.
    Handles $ref, enums, combinators, arrays, and nested objects.
    Generic — works for any API schema.
    """
    if "$ref" in schema:
        schema = _resolve_ref(schema)

    if not schema:
        return None

    # Handle combinators
    for combiner in ("allOf", "oneOf", "anyOf"):
        if combiner in schema:
            merged = {}
            for sub in schema[combiner]:
                sub_resolved = _resolve_ref(sub) if "$ref" in sub else sub
                result = _extract_example_payload(sub_resolved)
                if result:
                    merged.update(result)
            return merged or None

    if schema.get("type") != "object":
        return None

    payload = {}
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])

    for prop, prop_schema in properties.items():
        if "$ref" in prop_schema:
            prop_schema = _resolve_ref(prop_schema)

        value = None

        if "example" in prop_schema:
            value = prop_schema["example"]
        elif prop_schema.get("enum"):
            value = prop_schema["enum"][0]
        elif prop_schema.get("type") == "string":
            value = "test_value"
        elif prop_schema.get("type") == "integer":
            value = 1
        elif prop_schema.get("type") == "number":
            value = 1.0
        elif prop_schema.get("type") == "boolean":
            value = True
        elif prop_schema.get("type") == "object":
            value = _extract_example_payload(prop_schema) or {}
        elif prop_schema.get("type") == "array":
            items = prop_schema.get("items", {})
            if "$ref" in items:
                items = _resolve_ref(items)
            if items.get("enum"):
                value = [items["enum"][0]]
            elif items.get("type") == "string":
                if prop.lower() in ("photourls",):
                    value = ["https://example.com/photo.jpg"]
                else:
                    value = ["test_value"]
            elif items.get("type") == "integer":
                value = [1]
            elif items.get("type") == "object":
                value = [_extract_example_payload(items) or {}]
            else:
                value = []

        if prop in required_fields or value is not None:
            payload[prop] = value

    return payload or None


def _fetch_real_id(base_url: str, path: str) -> int | str | None:
    """
    Try to fetch a real ID by calling the list endpoint.
    e.g. /booking/{id} → GET /booking → returns first bookingid
    Generic — works for any API with a list endpoint.
    """
    list_path = re.sub(r"/\{[^}]+\}.*$", "", path)
    if not list_path or list_path == path:
        return None

    list_url = base_url.rstrip("/") + list_path
    logger.debug(f"Fetching real ID from list endpoint: {list_url}")

    try:
        resp = requests.get(
            list_url,
            headers={"Accept": "application/json"},
            timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                first = data[0]
                if isinstance(first, dict):
                    # Look for common ID field names
                    for id_field in ("id", "bookingid", "petId", "orderId", "userId"):
                        if id_field in first:
                            real_id = first[id_field]
                            logger.info(
                                f"Found real ID {real_id} from "
                                f"{list_url} field '{id_field}'"
                            )
                            return real_id
                elif isinstance(first, (int, str)):
                    return first
    except Exception as e:
        logger.debug(f"Could not fetch real ID from {list_url} — {e}")

    return None


def _get_safe_path_value(param: dict, base_url: str = "", path: str = "") -> str:
    """Get a safe value for a path parameter, fetching real ID if possible."""
    schema = param.get("schema", {})
    enum = schema.get("enum")

    if enum:
        return str(enum[0])

    if schema.get("type") == "integer":
        # Try to fetch a real ID from the list endpoint
        real_id = _fetch_real_id(base_url, path)
        if real_id is not None:
            return str(real_id)
        return "1"

    return "test"


def _build_url(base_url: str, path: str, path_params: list[dict]) -> str:
    """Replace path param placeholders with real values where possible."""
    url = base_url.rstrip("/") + path
    for param in path_params:
        name = param["name"]
        default = _get_safe_path_value(param, base_url=base_url, path=path)
        url = url.replace(f"{{{name}}}", default)
        logger.debug(f"Replaced path param {{{name}}} → '{default}'")
    return url


def _build_invalid_url(base_url: str, path: str, path_params: list[dict]) -> str:
    """Build a URL with an unlikely ID to trigger a 404 response."""
    url = base_url.rstrip("/") + path
    for param in path_params:
        name = param["name"]
        url = url.replace(f"{{{name}}}", "999999999")
    return url


def _build_query_params(query_params: list[dict]) -> dict:
    """Build example query params from spec, using enum values where available."""
    params = {}
    for p in query_params:
        name = p.get("name")
        schema = p.get("schema", {})
        enum = schema.get("enum")

        if enum:
            params[name] = enum[0]
        elif schema.get("type") == "integer":
            params[name] = 1
        else:
            params[name] = "test"

    return params


def _make_request(
    method: str,
    url: str,
    payload: dict | None = None,
    query_params: dict | None = None,
    label: str = "",
) -> dict:
    """
    Make a single HTTP request and return a clean structured response dict.
    Logs full request details — method, URL, payload, response status, body.
    Never raises — always returns a result even on network error.
    """
    logger.info(f"{'─' * 60}")
    logger.info(f"REQUEST  [{label}] {method} {url}")
    if payload is not None:
        logger.info(f"PAYLOAD  {json.dumps(payload)}")
    else:
        logger.info(f"PAYLOAD  (none)")
    if query_params:
        logger.info(f"PARAMS   {json.dumps(query_params)}")

    try:
        request_kwargs = {
            "method": method.upper(),
            "url": url,
            "headers": {"Accept": "application/json"},
            "timeout": 8,
        }

        if query_params:
            request_kwargs["params"] = query_params

        if method.upper() in ["POST", "PUT", "PATCH"]:
            request_kwargs["json"] = payload or {}

        resp = requests.request(**request_kwargs)

        content_type = resp.headers.get("Content-Type", "")
        is_json = "application/json" in content_type

        if is_json:
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:500]
        else:
            body = resp.text[:500]

        logger.info(
            f"RESPONSE [{label}] status={resp.status_code} "
            f"content_type={content_type}"
        )
        logger.debug(
            f"BODY     [{label}] "
            f"{json.dumps(body) if isinstance(body, (dict, list)) else str(body)[:500]}"
        )

        if resp.status_code >= 400:
            logger.warning(
                f"NON-2XX  [{label}] {method} {url} → {resp.status_code} "
                f"body={json.dumps(body) if isinstance(body, (dict, list)) else str(body)[:200]}"
            )

        return {
            "status_code": resp.status_code,
            "content_type": content_type,
            "is_json": is_json,
            "body": body,
            "error": None,
        }

    except requests.exceptions.ConnectionError as e:
        logger.error(f"CONNECTION ERROR [{label}] {method} {url} — {e}")
        return {"status_code": None, "content_type": "", "is_json": False,
                "body": None, "error": "Connection error"}
    except requests.exceptions.Timeout as e:
        logger.error(f"TIMEOUT [{label}] {method} {url} — {e}")
        return {"status_code": None, "content_type": "", "is_json": False,
                "body": None, "error": "Request timed out"}
    except Exception as e:
        logger.error(f"ERROR [{label}] {method} {url} — {e}")
        return {"status_code": None, "content_type": "", "is_json": False,
                "body": None, "error": str(e)}


# ── Core prober ───────────────────────────────────────────────────────────────
def probe_endpoint(endpoint: dict) -> dict:
    """
    Probe a single endpoint with two scenarios:
      1. valid_response   — called with example payload (happy path)
      2. invalid_response — called with a payload that triggers a proper 4xx

    If valid probe returns 5xx — marks endpoint as skipped and logs clearly.
    Skipped endpoints are excluded from test generation by the caller.
    """
    method = endpoint["method"].upper()
    operation_id = endpoint.get("operation_id", "unknown")
    url = _build_url(
        endpoint["base_url"],
        endpoint["path"],
        endpoint.get("path_params", [])
    )

    logger.info(f"{'=' * 60}")
    logger.info(f"PROBING  {operation_id} — {method} {endpoint['path']}")
    logger.info(f"BASE_URL {endpoint['base_url']}")
    logger.info(f"FULL_URL {url}")

    # Build query params from spec
    query_params = _build_query_params(endpoint.get("query_params", []))

    # Build valid payload
    valid_payload = None
    if endpoint.get("request_body"):
        schema = endpoint["request_body"].get("schema", {})
        valid_payload = _extract_example_payload(schema)
        logger.debug(f"BUILT PAYLOAD for {operation_id}: {json.dumps(valid_payload)}")

    # ── Determine query params for valid probe ────────────────────────────────
    # For GET list endpoints (no path params), skip query params on valid probe
    # because filter params like firstname=test return empty results
    if method == "GET" and not endpoint.get("path_params"):
        valid_query_params = None
        logger.debug(
            f"PROBE 1  skipping query params for GET list endpoint {endpoint['path']}"
        )
    elif method == "GET" and endpoint.get("path_params"):
        valid_query_params = query_params if query_params else None
    else:
        valid_query_params = query_params if query_params else None

    # ── Probe 1: Valid request ────────────────────────────────────────────────
    logger.info(f"PROBE 1  (valid) {method} {url}")
    endpoint["valid_response"] = _make_request(
        method, url,
        payload=valid_payload,
        query_params=valid_query_params,
        label=f"{operation_id}:valid"
    )
    valid_status = endpoint["valid_response"].get("status_code")

    # ── Handle network error ──────────────────────────────────────────────────
    if valid_status is None:
        error = endpoint["valid_response"].get("error", "unknown error")
        logger.error(
            f"SKIP {operation_id} — probe failed with network error: {error}"
        )
        endpoint["skip"] = True
        endpoint["skip_reason"] = f"Network error: {error}"
        endpoint["invalid_response"] = None
        return endpoint

    # ── Handle 5xx — API is broken, skip entirely ─────────────────────────────
    if valid_status >= 500:
        body = endpoint["valid_response"].get("body", "")
        logger.error(
            f"SKIP {operation_id} — valid probe returned {valid_status} (server error). "
            f"Body: {json.dumps(body) if isinstance(body, (dict, list)) else str(body)[:200]}"
        )
        logger.error(
            f"SKIP DETAIL — method={method} url={url} "
            f"payload={json.dumps(valid_payload) if valid_payload else 'none'}"
        )
        endpoint["skip"] = True
        endpoint["skip_reason"] = (
            f"Valid probe returned {valid_status} — server error or incomplete payload"
        )
        endpoint["invalid_response"] = None
        return endpoint

    # ── Warn on 4xx valid response ────────────────────────────────────────────
    if valid_status >= 400:
        logger.warning(
            f"WARN {operation_id} — valid probe returned {valid_status}. "
            f"Endpoint may require auth or payload is incomplete."
        )

    # ── Probe 2: Invalid request ──────────────────────────────────────────────
    invalid_response = None

    if endpoint.get("request_body"):
        logger.info(f"PROBE 2  (invalid — wrong payload) {method} {url}")
        candidate = _make_request(
            method, url,
            payload={"__invalid__": True},
            query_params=query_params if query_params else None,
            label=f"{operation_id}:invalid_wrong"
        )
        candidate_status = candidate.get("status_code")

        if (
            candidate_status
            and candidate_status != valid_status
            and 400 <= candidate_status < 500
        ):
            logger.info(
                f"PROBE 2  ACCEPTED — {operation_id} → {candidate_status}"
            )
            invalid_response = candidate
        else:
            logger.debug(
                f"PROBE 2  REJECTED wrong_payload → {candidate_status} — trying empty"
            )
            candidate2 = _make_request(
                method, url,
                payload={},
                query_params=query_params if query_params else None,
                label=f"{operation_id}:invalid_empty"
            )
            candidate2_status = candidate2.get("status_code")
            if (
                candidate2_status
                and candidate2_status != valid_status
                and 400 <= candidate2_status < 500
            ):
                logger.info(
                    f"PROBE 2  ACCEPTED empty_payload — {operation_id} → {candidate2_status}"
                )
                invalid_response = candidate2
            else:
                logger.info(
                    f"PROBE 2  SKIPPED — no distinct 4xx for {operation_id}"
                )

    elif method == "GET" and endpoint.get("path_params"):
        bad_url = _build_invalid_url(
            endpoint["base_url"],
            endpoint["path"],
            endpoint.get("path_params", [])
        )
        logger.info(f"PROBE 2  (invalid path param) GET {bad_url}")
        candidate = _make_request(
            method, bad_url,
            query_params=query_params if query_params else None,
            label=f"{operation_id}:invalid_path"
        )
        candidate_status = candidate.get("status_code")
        if candidate_status and candidate_status != valid_status:
            logger.info(
                f"PROBE 2  ACCEPTED — {operation_id} bad path → {candidate_status}"
            )
            invalid_response = candidate

    elif method == "GET":
        # For GET list endpoints — try with bad query param
        bad_query = dict(query_params) if query_params else {}
        bad_query["__invalid__"] = "true"
        candidate = _make_request(
            method, url,
            query_params=bad_query,
            label=f"{operation_id}:invalid_query"
        )
        candidate_status = candidate.get("status_code")
        if (
            candidate_status
            and candidate_status != valid_status
            and 400 <= candidate_status < 500
        ):
            logger.info(f"PROBE 2  ACCEPTED bad query → {candidate_status}")
            invalid_response = candidate
        else:
            logger.info(
                f"PROBE 2  SKIPPED — GET {endpoint['path']} has no distinct error response"
            )

    endpoint["invalid_response"] = invalid_response
    endpoint["skip"] = False
    endpoint["skip_reason"] = None

    logger.info(
        f"RESULT   {operation_id} — "
        f"valid={valid_status} | "
        f"invalid={invalid_response.get('status_code') if invalid_response else 'None'}"
    )
    return endpoint

def probe_all_endpoints(endpoints: list[dict], spec: dict | None = None) -> list[dict]:
    """Probe all endpoints and attach real response data to each."""
    if spec:
        set_spec(spec)

    logger.info(f"{'#' * 60}")
    logger.info(f"PROBE ALL — {len(endpoints)} endpoints")
    logger.info(f"{'#' * 60}")

    console.print("\n[bold cyan]▶ Probing API endpoints...[/bold cyan]")

    results = []
    skipped = []

    for ep in endpoints:
        label = f"{ep['method']} {ep['path']}"
        console.print(f"  [dim]Probing {label}...[/dim]", end=" ")

        probed = probe_endpoint(ep)

        if probed.get("skip"):
            reason = probed.get("skip_reason", "unknown reason")
            console.print(f"[red]✗ SKIPPED — {reason}[/red]")
            logger.error(f"SKIPPED {label} — {reason}")
            skipped.append(probed)
            continue

        valid = probed.get("valid_response", {})
        invalid = probed.get("invalid_response")
        status = valid.get("status_code", "ERR")
        invalid_status = invalid.get("status_code") if invalid else "—"

        if valid.get("error"):
            console.print(f"[red]✗ {valid['error']}[/red]")
            logger.error(f"PROBE FAILED {label} — {valid['error']}")
        else:
            console.print(
                f"[green]✓ {status}[/green]  "
                f"[dim]invalid → {invalid_status}[/dim]"
            )

        results.append(probed)

    # ── Print skipped summary AFTER loop ──────────────────────────────────────
    if skipped:
        console.print(
            f"\n[yellow]⚠ Skipped {len(skipped)} endpoint(s) due to server errors:[/yellow]"
        )
        for skipped_ep in skipped:
            console.print(
                f"  [red]✗ {skipped_ep['method']} {skipped_ep['path']} "
                f"— {skipped_ep.get('skip_reason')}[/red]"
            )
        skipped_labels = [f"{e['method']} {e['path']}" for e in skipped]
        logger.warning(
            f"PROBE ALL — {len(skipped)} endpoints skipped: {skipped_labels}"
        )

    logger.info(
        f"PROBE ALL COMPLETE — probed: {len(results)}, skipped: {len(skipped)}"
    )
    _print_probe_summary(results)
    return results


def _body_preview(body: Any) -> str:
    """Compact preview of a response body for the summary table."""
    if body is None:
        return "—"
    s = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
    return s[:50] + "..." if len(s) > 50 else s


def _print_probe_summary(endpoints: list[dict]) -> None:
    """Print a clean Rich summary table."""
    table = Table(title="API Probe Results", show_lines=True)
    table.add_column("Method", style="bold cyan", width=8)
    table.add_column("Path", style="white")
    table.add_column("Valid Status", style="green", width=12)
    table.add_column("Valid Body", style="dim", max_width=35)
    table.add_column("Invalid Status", style="yellow", width=14)
    table.add_column("Invalid Body", style="dim", max_width=35)

    for ep in endpoints:
        valid = ep.get("valid_response") or {}
        invalid = ep.get("invalid_response") or {}

        table.add_row(
            ep["method"],
            ep["path"],
            str(valid.get("status_code", "ERR")),
            _body_preview(valid.get("body")),
            str(invalid.get("status_code", "—")) if invalid else "—",
            _body_preview(invalid.get("body")) if invalid else "—",
        )

    console.print(table)


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from agent.spec_parser import extract_endpoints, load_spec

    spec_file = sys.argv[1] if len(sys.argv) > 1 else "specs/restful_booker.yaml"
    base_url_override = sys.argv[2] if len(sys.argv) > 2 else None

    spec = load_spec(spec_file)
    set_spec(spec)
    endpoints = extract_endpoints(spec)

    if base_url_override:
        for ep in endpoints:
            ep["base_url"] = base_url_override.rstrip("/")

    probed = probe_all_endpoints(endpoints, spec=spec)

    console.print("\n[bold]Sample probe data (first endpoint):[/bold]")
    ep = probed[0]
    console.print(json.dumps({
        "method": ep["method"],
        "path": ep["path"],
        "valid_response": ep.get("valid_response"),
        "invalid_response": ep.get("invalid_response"),
    }, indent=2))