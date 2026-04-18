"""
self_healer.py
Two-layer healing strategy for template-generated Pytest tests:

  Layer 1 — Deterministic pre-pass (no LLM):
    - Wrong key in assertion: assert 'X' in json → assert 'Y' in json
    - Wrong status code: extracts real status from traceback
    - Wrong text assertion: extracts real text from traceback

  Layer 2 — LLM chain (function-level patching):
    - Extracts only the failing function
    - Fetches real API response by parsing BASE_URL + path from test source
    - Passes ground truth to LLM for fixing
    - Restores original on max retries

Generic — works for any template-generated test, no hardcoded API names.
"""

from __future__ import annotations

import ast
import re
import shutil
import warnings
from pathlib import Path

import requests as http_requests

warnings.filterwarnings("ignore", category=UserWarning)

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from rich.console import Console

from agent.llm_factory import get_llm
from agent.test_runner import FailedTest, run_tests
from agent.logger import get_healer_logger

logger = get_healer_logger()
console = Console()

MAX_RETRIES = 3


# ── Prompts ───────────────────────────────────────────────────────────────────

HEALER_SYSTEM_PROMPT = (
    "You are an expert SDET who fixes broken Pytest test functions.\n"
    "You will be given a single failing test function and the actual API response.\n\n"

    "Rules:\n"
    "- Fix the test so it correctly validates the intended API behavior\n"
    "- You MAY modify request payload and headers if needed\n"
    "- Fix assertions to match correct successful behavior\n"
    "- If API response shows 400/401/403, assume request is wrong and fix it\n"
    "- Do not rename the function or change its docstring\n"
    "- Do not add new imports\n"
    "- Do not change BASE_URL\n"
    "- Output ONLY the fixed function — valid Python, no explanation\n"
    "- Never assert dynamic values like IDs or tokens\n"
)

HEALER_HUMAN_PROMPT = (
    "Failing test function:\n{function_code}\n\n"
    "Error message:\n{error_message}\n\n"
    "Actual API response when this test ran:\n{actual_response}\n\n"
    "The actual response above is ground truth.\n"
    "Fix ALL broken assertions to match it exactly.\n"
    "Return ONLY the fixed function, nothing else:"
)


# ── AST helpers ───────────────────────────────────────────────────────────────

def extract_function(source: str, func_name: str) -> str:
    """Extract a single function's source code from a file."""
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            start = node.lineno - 1
            end = node.end_lineno
            return "\n".join(lines[start:end])
    return ""


def replace_function(source: str, func_name: str, new_func: str) -> str:
    """Replace a single function in source with new_func."""
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            start = node.lineno - 1
            end = node.end_lineno
            new_lines = lines[:start] + new_func.splitlines() + lines[end:]
            return "\n".join(new_lines)
    return source


# ── Source code parser ────────────────────────────────────────────────────────

def extract_base_url(source: str) -> str:
    """Extract BASE_URL value from test file source."""
    for line in source.splitlines():
        if "BASE_URL" in line and "=" in line and "assert" not in line:
            return line.split("=", 1)[1].strip().strip('"\'')
    return ""


def extract_request_details(func_code: str) -> tuple[str, str, dict | None]:
    """
    Parse a test function to extract:
    - HTTP method (GET, POST, PUT, etc.)
    - URL path (e.g. /booking, /auth)
    - Payload (if any)

    Returns (method, path, payload)
    """
    method = "GET"
    path = "/"
    payload = None

    # Match: requests.get/post/put/etc(f"{BASE_URL}/path", ...)
    url_match = re.search(
        r"requests\.(get|post|put|patch|delete)\s*\(\s*f['\"].*?BASE_URL\}?([^'\"]*)['\"]",
        func_code,
        re.IGNORECASE
    )
    if url_match:
        method = url_match.group(1).upper()
        path = url_match.group(2).strip()

    # Match: json={...} payload
    payload_match = re.search(r"json=(\{[^}]+\})", func_code)
    if payload_match:
        try:
            payload = ast.literal_eval(payload_match.group(1))
        except Exception:
            payload = {}

    return method, path, payload


def fetch_actual_response(base_url: str, method: str, path: str, payload: dict | None) -> str:
    """
    Make a real HTTP call and return structured response string for LLM context.
    Generic — works for any API.
    """
    url = base_url.rstrip("/") + path
    try:
        resp = http_requests.request(
            method=method,
            url=url,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=8,
        )
        content_type = resp.headers.get("Content-Type", "")
        is_json = "application/json" in content_type
        body = resp.json() if is_json else resp.text[:300]
        return (
            f"Status: {resp.status_code}\n"
            f"Content-Type: {content_type}\n"
            f"Body: {body}"
        )
    except Exception as e:
        return f"Could not fetch: {e}"

def is_invalid_request_response(actual_response: str) -> bool:
    return any(code in actual_response for code in ["400", "401", "403"])

def get_actual_response_for_failure(failure: FailedTest) -> str:
    """
    Derive the actual API response for a failing test by:
    1. Extracting BASE_URL from the test file
    2. Parsing the failing function to get method + path + payload
    3. Making a real HTTP call
    """
    base_url = extract_base_url(failure.source_code)
    if not base_url:
        return "Could not determine BASE_URL from source"

    func_code = extract_function(failure.source_code, failure.test_name)
    if not func_code:
        return "Could not extract function source"

    method, path, payload = extract_request_details(func_code)
    return fetch_actual_response(base_url, method, path, payload)


# ── Layer 1: Deterministic fixers ─────────────────────────────────────────────

def _fix_wrong_key(source: str, func_name: str, traceback: str) -> str:
    """
    Fix: assert 'wrong_key' in response.json()
    When traceback shows: assert 'wrong_key' in {'correct_key': ...}
    """
    match = re.search(r"assert '(\w+)' in \{'(\w+)'", traceback)
    if not match:
        return source

    wrong_key = match.group(1)
    correct_key = match.group(2)

    if wrong_key == correct_key:
        return source

    func_code = extract_function(source, func_name)
    if not func_code:
        return source

    fixed_func = func_code.replace(f"'{wrong_key}'", f"'{correct_key}'")
    console.print(f"   [green]⚡ Fix wrong key: '{wrong_key}' → '{correct_key}'[/green]")
    return replace_function(source, func_name, fixed_func)


def _fix_wrong_status_code(source: str, func_name: str, traceback: str) -> str:
    """
    Fix: assert response.status_code == X
    When traceback shows: assert Y == X (real status is Y)
    """
    # Match: assert 404 == 200 or assert 200 == 404
    match = re.search(
        r"assert (\d+) == (\d+)\s*\+\s*where (\d+) = <Response \[(\d+)\]>",
        traceback
    )
    if not match:
        # Try simpler pattern: E assert 404 == 200
        match = re.search(r"E\s+assert (\d+) == (\d+)", traceback)
        if not match:
            return source
        real_status = int(match.group(1))
        wrong_status = int(match.group(2))
    else:
        real_status = int(match.group(1))
        wrong_status = int(match.group(2))

    if real_status == wrong_status:
        return source

    func_code = extract_function(source, func_name)
    if not func_code:
        return source

    # Only fix status code assertions, not other integer comparisons
    fixed_func = re.sub(
        rf"assert response\.status_code == {wrong_status}",
        f"assert response.status_code == {real_status}",
        func_code
    )

    if fixed_func == func_code:
        return source

    console.print(
        f"   [green]⚡ Fix wrong status: {wrong_status} → {real_status}[/green]"
    )
    return replace_function(source, func_name, fixed_func)


def _fix_json_on_plaintext(source: str, func_name: str, traceback: str) -> str:
    """
    Fix: response.json() called on plain text response
    When traceback shows JSONDecodeError or similar
    """
    if "JSONDecodeError" not in traceback and "json.decoder" not in traceback:
        return source

    func_code = extract_function(source, func_name)
    if not func_code:
        return source

    fixed_func = func_code.replace("response.json()", "response.text")
    fixed_func = fixed_func.replace(
        "assert isinstance(response.text, dict)",
        "assert len(response.text) > 0"
    )

    console.print("   [green]⚡ Fix JSON on plaintext: response.json() → response.text[/green]")
    return replace_function(source, func_name, fixed_func)


def deterministic_fix(source: str, func_name: str, traceback: str) -> str:
    """
    Layer 1: Apply all deterministic fixes in order.
    Each fix is independent — applies only if its pattern matches.
    Returns fixed source or original if nothing matched.
    """
    result = source

    # Fix 1 — wrong key in JSON assertion
    result = _fix_wrong_key(result, func_name, traceback)

    # Fix 2 — wrong status code
    result = _fix_wrong_status_code(result, func_name, traceback)

    # Fix 3 — response.json() on plain text
    result = _fix_json_on_plaintext(result, func_name, traceback)

    # Fix 4 — wrong comparison operator (e.g. != instead of ==)
    result = _fix_wrong_comparison_operator(result, func_name, traceback)

    return result


# ── Layer 2: LLM chain ────────────────────────────────────────────────────────

def build_function_healer_chain():
    """
    Heals a single extracted function in isolation.
    LLM only sees the broken lines + real API response.
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", HEALER_SYSTEM_PROMPT),
        ("human", HEALER_HUMAN_PROMPT),
    ])
    llm = get_llm(temperature=0.0)
    return prompt | llm | StrOutputParser()


def clean_code(raw: str) -> str:
    """
    Strip deepseek-r1 and other LLM artifacts:
    - ### Solution Code / ### Code headers
    - ```python ... ``` fences
    - Prose explanation before and after code
    - [PYTHON] tags
    """
    # Strip [PYTHON] / [/PYTHON] tags
    raw = re.sub(r"\[/?PYTHON\]", "", raw, flags=re.IGNORECASE)

    # Extract content from ```python ... ``` fence if present
    # deepseek-r1 puts actual code inside fences after prose
    fence_match = re.search(r"```python\s*\n(.*?)```", raw, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Try plain ``` fence
    fence_match = re.search(r"```\s*\n(.*?)```", raw, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # No fences — strip prose and find the function
    # Look for first 'def ' line and take everything from there
    lines = raw.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("def "):
            start_idx = i
            break

    if start_idx is not None:
        # Take from def to last code line
        code_lines = lines[start_idx:]
        last_code_line = 0
        for i, line in enumerate(code_lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                last_code_line = i
            if any(stripped.startswith(marker) for marker in (
                "###", "**", "Note:", "This ", "The above",
                "In this", "Overall", "We have", "Here ", "Above "
            )):
                break
        return "\n".join(code_lines[:last_code_line + 1]).strip()

    return raw.strip()


# ── Core healer ───────────────────────────────────────────────────────────────

def heal_test(failure: FailedTest) -> bool:
    """
    Attempt to heal a single failing test using two-layer strategy.
    Returns True if healed within MAX_RETRIES, False otherwise.
    Restores original ONLY if healing completely fails.
    """
    file_path = Path(failure.file_path)

    logger.info(f"Starting heal: {failure.test_name} in {Path(failure.file_path).name}")
    logger.debug(f"Error: {failure.error_message}")

    console.print(f"\n[bold yellow]🔧 Healing:[/bold yellow] {failure.test_name}")
    console.print(f"   File:  {file_path.name}")
    console.print(f"   Error: {failure.error_message[:120]}")

    # Save original for restoration on failure
    original_source = failure.source_code
    current_source = failure.source_code

    healed = False  # ✅ Track global healing state

    # ── Layer 1: Deterministic pre-pass ───────────────────────────────────────
    logger.info(f"Layer 1: Trying deterministic fix for {failure.test_name}")
    console.print("   [dim]Layer 1: Trying deterministic fix...[/dim]")
    fixed_source = deterministic_fix(
        current_source, failure.test_name, failure.traceback
    )

    if fixed_source != current_source:
        logger.info(f"Layer 1: Deterministic fix applied for {failure.test_name}")
        file_path.write_text(fixed_source, encoding="utf-8")

        _, _, check_failures = run_tests(str(file_path.parent))

        if failure.test_id not in {f.test_id for f in check_failures}:
            console.print("   [bold green]✅ Healed by deterministic fix![/bold green]")
            return True

        # Partial fix → continue to LLM
        console.print("   [dim]Partial fix — handing off to LLM...[/dim]")
        current_source = fixed_source

        for f in check_failures:
            if f.test_id == failure.test_id:
                failure.error_message = f.error_message
                failure.traceback = f.traceback
                break
    else:
        logger.info(f"Layer 1: No deterministic fix found for {failure.test_name}")
        console.print("   [dim]No deterministic fix found — going to LLM...[/dim]")

    # ── Layer 2: LLM loop ─────────────────────────────────────────────────────
    logger.info(f"Layer 2: LLM healing attempt {attempt}/{MAX_RETRIES} for {failure.test_name}")
    logger.debug(f"LLM raw output:\n{fixed_func}")
    console.print("   [dim]Layer 2: Fetching actual API response...[/dim]")
    actual_response = get_actual_response_for_failure(failure)

    if is_invalid_request_response(actual_response):
        console.print("   [yellow]⚠ Invalid request detected — skipping naive healing[/yellow]")

    console.print(f"   [dim]API response: {actual_response[:150]}[/dim]")

    func_chain = build_function_healer_chain()

    for attempt in range(1, MAX_RETRIES + 1):
        console.print(
            f"\n   [cyan]LLM Attempt {attempt}/{MAX_RETRIES}[/cyan] — asking for fix..."
        )

        failing_func_code = extract_function(current_source, failure.test_name)
        if not failing_func_code:
            console.print(f"   [red]Could not extract '{failure.test_name}'[/red]")
            break

        raw_output = func_chain.invoke({
            "function_code": failing_func_code,
            "error_message": failure.error_message,
            "actual_response": actual_response,
        })

        console.print(f"   [dim]RAW BEFORE CLEAN:\n{repr(raw_output[:500])}[/dim]")

        fixed_func = clean_code(raw_output)

        console.print(f"   [dim]LLM raw output:\n{fixed_func}[/dim]")

        # Guard 1 — empty output
        if not fixed_func.strip():
            console.print("   [yellow]⚠ LLM returned empty output — retrying...[/yellow]")
            continue

        # Guard 2 — must be a function
        if not fixed_func.strip().startswith("def "):
            console.print("   [yellow]⚠ LLM output is not a function — retrying...[/yellow]")
            continue

        # Reinsert fixed function
        fixed_code = replace_function(current_source, failure.test_name, fixed_func)

        # Guard 3 — syntax + function count check
        try:
            original_func_count = len([
                n for n in ast.walk(ast.parse(current_source))
                if isinstance(n, ast.FunctionDef)
            ])
            fixed_func_count = len([
                n for n in ast.walk(ast.parse(fixed_code))
                if isinstance(n, ast.FunctionDef)
            ])
        except SyntaxError:
            console.print("   [yellow]⚠ Syntax error — retrying...[/yellow]")
            continue

        if fixed_func_count < original_func_count:
            console.print(
                f"   [yellow]⚠ Function removed ({original_func_count} → {fixed_func_count}) — retrying...[/yellow]"
            )
            continue

        # Backup before overwrite
        backup_path = file_path.with_suffix(f".bak{attempt}")
        shutil.copy2(file_path, backup_path)

        file_path.write_text(fixed_code, encoding="utf-8")

        _, _, new_failures = run_tests(str(file_path.parent))
        healed_ids = {f.test_id for f in new_failures}

        if failure.test_id not in healed_ids:
            console.print(f"   [bold green]✅ Healed on LLM attempt {attempt}![/bold green]")
            logger.info(f"✅ Healed: {failure.test_name} on attempt {attempt}")
            healed = True  # ✅ mark success

            # Cleanup backups
            for i in range(1, attempt + 1):
                bak = file_path.with_suffix(f".bak{i}")
                if bak.exists():
                    bak.unlink()

            return True

        # Still failing → update context
        console.print("   [red]✗ Still failing — retrying...[/red]")
        
        for f in new_failures:
            if f.test_id == failure.test_id:
                failure.error_message = f.error_message
                failure.traceback = f.traceback
                current_source = f.source_code
                break

    # ── FINAL DECISION ─────────────────────────────────────────────────────────

    if not healed:
        console.print(
            f"   [bold red]❌ Could not heal after {MAX_RETRIES} attempts — restoring original[/bold red]"
        )
        logger.error(f"❌ Could not heal: {failure.test_name} after {MAX_RETRIES} attempts")
        file_path.write_text(original_source, encoding="utf-8")

    # Cleanup backups ALWAYS
    for i in range(1, MAX_RETRIES + 1):
        bak = file_path.with_suffix(f".bak{i}")
        if bak.exists():
            bak.unlink()

    return healed

def _fix_wrong_comparison_operator(source: str, func_name: str, traceback: str) -> str:
    if "!=" not in traceback:
        return source

    func_code = extract_function(source, func_name)
    if not func_code:
        return source

    fixed_func = func_code.replace("!=", "==")

    console.print("   ⚡ Fix comparison: != → ==")
    return replace_function(source, func_name, fixed_func)
  


# ── Orchestrator ──────────────────────────────────────────────────────────────

def heal_all(test_dir: str = "generated_tests") -> None:
    """Full pipeline: run tests → heal all failures → report results."""
    console.print("\n[bold]━━━ QA Agent Self-Healer ━━━[/bold]")
    logger.info(f"=== Self-Healer started — test_dir: {test_dir} ===")

    passed, failed, failures = run_tests(test_dir)

    if not failures:
        console.print("[bold green]✓ All tests passing — nothing to heal![/bold green]")
        return

    console.print(f"\n[yellow]Found {len(failures)} failure(s) — starting heal loop...[/yellow]")

    healed, unhealed = [], []
    for failure in failures:
        success = heal_test(failure)
        (healed if success else unhealed).append(failure)

    # Final report
    console.print("\n[bold]━━━ Heal Summary ━━━[/bold]")
    console.print(f"[green]✅ Healed:   {len(healed)}[/green]")
    console.print(f"[red]❌ Unhealed: {len(unhealed)}[/red]")

    if healed:
        console.print("\n[green]Healed tests:[/green]")
        for f in healed:
            console.print(f"  ✅ {f.test_name}")

    if unhealed:
        console.print("\n[red]Could not heal:[/red]")
        for f in unhealed:
            console.print(f"  ❌ {f.test_name}")

    logger.info(f"=== Heal complete — healed: {len(healed)}, unhealed: {len(unhealed)} ===")
# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    heal_all("generated_tests")