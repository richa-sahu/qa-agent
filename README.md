# qa-agent 🤖

An AI-powered QA automation agent that reads OpenAPI specs, auto-generates
Pytest tests from real API responses, and self-heals flaky or failing tests
using a two-layer deterministic + LLM strategy.

Built as a portfolio project demonstrating senior SDET skills in AI-augmented
test automation, API testing, and software engineering best practices.

![Python](https://img.shields.io/badge/Python-3.12+-blue)
![Pytest](https://img.shields.io/badge/Pytest-8.3-green)
![LangChain](https://img.shields.io/badge/LangChain-0.3-orange)
![Ollama](https://img.shields.io/badge/Ollama-deepseek--r1-purple)
![CI](https://github.com/richa-sahu/qa-agent/actions/workflows/ci.yml/badge.svg)

---

## What it does

OpenAPI Spec (YAML/JSON)
↓
spec_parser.py        — parse endpoints into structured descriptors
↓
api_prober.py         — probe live API, capture real responses
↓
template_generator.py — generate Pytest tests from Jinja2 template
↓
test_runner.py        — run tests, capture structured failures
↓
self_healer.py        — auto-fix failures (deterministic + LLM)

**Key capabilities:**

- Reads any OpenAPI 3.x or 2.x spec
- Makes real HTTP calls to understand actual API behavior
- Generates tests based on real responses — not guesses
- Detects CREATE → READ → UPDATE → DELETE chains for stateful testing
- Self-heals failing tests using a two-layer strategy
- Works with any REST API — no hardcoded assumptions

---

## Architecture

qa-agent/
├── agent/
│   ├── spec_parser.py          # Parse OpenAPI spec → endpoint descriptors
│   ├── api_prober.py           # Probe live API → real responses + $ref resolution
│   ├── template_generator.py   # Jinja2 deterministic test generation
│   ├── dependency_resolver.py  # Detect CREATE→READ→DELETE chains
│   ├── conftest_generator.py   # Generate session-scoped pytest fixtures
│   ├── test_runner.py          # Run pytest, capture FailedTest objects
│   ├── self_healer.py          # Two-layer healing (deterministic + LLM)
│   ├── llm_factory.py          # Swappable Ollama / OpenAI backend
│   ├── logger.py               # Structured per-module logging
│   └── templates/
│       └── test_template.py.j2 # Jinja2 test template
├── specs/
│   ├── restful_booker.yaml     # RestfulBooker OpenAPI spec
│   └── petstore.yaml           # Swagger Petstore v3 spec
├── generated_tests/            # Auto-generated test files land here
├── logs/                       # Structured logs per module
├── cli.py                      # Single entry point
└── .github/workflows/ci.yml   # GitHub Actions pipeline

---

## Self-Healing Strategy

The self-healer uses a two-layer approach — LLM is only called when deterministic
fixes aren't enough:

Failing test
↓
Layer 1 — Deterministic (no LLM, instant):
├── Wrong JSON key:     assert 'error' in json  →  assert 'reason' in json
├── Wrong status code:  assert 200 == 403       →  assert 403 == 403
└── JSON on plaintext:  response.json()         →  response.text
↓ (if still failing)
Layer 2 — LLM (deepseek-r1 via Ollama):
├── Extracts BASE_URL + method + path from test source
├── Makes real HTTP call to get ground truth
├── Sends only the failing function to LLM (not full file)
├── Guards: empty output, non-function output, deleted functions
└── Restores original on max retries

---

## Stateful Testing

The agent detects resource chains and generates stateful fixtures automatically:

```python
# Auto-detected chain: POST /booking → GET /booking/{id} → DELETE /booking/{id}

# generated_tests/conftest.py (auto-generated)
@pytest.fixture(scope="session")
def created_booking():
    response = requests.post(f"{BASE_URL}/booking", json={...})
    created_booking = response.json()["bookingid"]
    yield created_booking
    requests.delete(f"{BASE_URL}/booking/{created_booking}")  # always runs

# generated_tests/test_getBooking.py (auto-generated)
def test_getBooking_happy_path(created_booking):
    response = requests.get(f"{BASE_URL}/booking/{created_booking}")
    assert response.status_code == 200
```

Benefits:
- No more 404s from hardcoded IDs
- No more flaky tests from missing data
- Guaranteed cleanup after every test session

---

## Quick Start

### Prerequisites

- Python 3.12+
- [Ollama](https://ollama.com) installed locally

### Setup

```bash
git clone https://github.com/richa-sahu/qa-agent.git
cd qa-agent

python -m venv venv
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
```

### Start Ollama

```bash
# Terminal 1 — keep running
ollama serve

# Terminal 2 — pull model (one time)
ollama pull deepseek-r1
```

### Run the full pipeline

```bash
# Generate tests from RestfulBooker spec
python cli.py --spec specs/restful_booker.yaml --generate

# Run generated tests
python cli.py --run

# Heal any failures
python cli.py --heal

# Or run everything in one command
python cli.py --spec specs/restful_booker.yaml --all
```

---

## CLI Reference

```bash
python cli.py --spec <path>     # path to OpenAPI spec (YAML or JSON)
              --generate        # generate tests from spec
              --run             # run generated tests
              --heal            # run tests and heal failures
              --all             # full pipeline: generate + run + heal
              --base-url <url>  # override base URL (when spec has no servers section)
              --limit <n>       # limit number of endpoints to generate for
              --force           # overwrite existing generated tests
              --output <dir>    # output directory (default: generated_tests)
```

### Examples

```bash
# RestfulBooker — full spec
python cli.py --spec specs/restful_booker.yaml --generate --force

# Petstore — needs base URL override
python cli.py --spec specs/petstore.yaml \
              --generate \
              --base-url https://petstore3.swagger.io/api/v3 \
              --limit 5

# Full pipeline with force overwrite
python cli.py --spec specs/restful_booker.yaml --all --force
```

---

## Test Results

### RestfulBooker (21 tests)

✓ test_healthCheck_happy_path
✓ test_healthCheck_invalid_input
✓ test_healthCheck_response_structure
✓ test_createToken_happy_path
✓ test_createToken_invalid_input
✓ test_createToken_response_structure
✓ test_getBookings_happy_path
✓ test_getBookings_invalid_input
✓ test_getBookings_response_structure
✓ test_createBooking_happy_path
✓ test_createBooking_invalid_input
✓ test_createBooking_response_structure
✓ test_getBooking_happy_path         ← uses created_booking fixture
✓ test_getBooking_invalid_input
✓ test_getBooking_response_structure
✓ test_updateBooking_happy_path      ← uses created_booking fixture
✓ test_updateBooking_invalid_input
✓ test_updateBooking_response_structure
✓ test_deleteBooking_happy_path      ← uses created_booking fixture
✓ test_deleteBooking_invalid_input
✓ test_deleteBooking_response_structure
21 passed in ~20s


### Petstore
Supported — public server intermittently unavailable.
Tests generate correctly but live results depend on server uptime.

---

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
LLM_PROVIDER=ollama                     # "ollama" or "openai"
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=deepseek-r1
#OPENAI_API_KEY=sk-...                  # only if LLM_PROVIDER=openai
#OPENAI_MODEL=gpt-4o-mini
TARGET_BASE_URL=https://restful-booker.herokuapp.com
```

### Switching LLM backends

```bash
# Use Ollama (local, free)
LLM_PROVIDER=ollama
OLLAMA_MODEL=deepseek-r1   # or llama3.1

# Use OpenAI (cloud, better healing quality)
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

---

## CI/CD

GitHub Actions pipeline runs on every push and PR to `main`:

1. Setup Python 3.12
2. Install dependencies
3. Verify project structure
4. Smoke test spec parser
5. Probe RestfulBooker API
6. Generate tests from spec
7. Run all generated tests
8. Upload results + logs as artifacts

**Note:** Self-healing is a local developer workflow and is skipped in CI.
The pipeline validates that the generated tests pass against the live API.

---

## Logs

Each module writes structured logs to `logs/`:

| File | Contents |
|------|----------|
| `logs/api_prober.log` | Every HTTP call — method, URL, payload, status, body |
| `logs/generator.log` | Template context, generated file paths |
| `logs/test_runner.log` | Pytest results, failure details |
| `logs/healer.log` | Heal attempts, LLM outputs, outcomes |
| `logs/qa_agent.log` | CLI pipeline, spec parsing |

```bash
# Watch API calls in real time
tail -f logs/api_prober.log

# Check healing attempts
cat logs/healer.log
```

---

## Design Decisions

### Why Jinja2 templates instead of LLM generation?

Local LLMs (7B-13B) are unreliable for generating syntactically correct test
code consistently. After extensive testing, a deterministic Jinja2 template
driven by real probed API responses produces better results than LLM generation
every time. The LLM is reserved for healing complex failures where deterministic
fixes aren't enough.

> *"Use deterministic tools where reliability matters, AI where flexibility matters."*

### Why two-layer healing?

Most test failures fall into predictable patterns (wrong key, wrong status code).
A deterministic regex pre-pass fixes these instantly without burning LLM tokens.
The LLM is only invoked for complex logic errors that can't be pattern-matched.
This keeps healing fast and reliable.

### Why session-scoped fixtures for stateful tests?

Stateful tests (GET /booking/{id}) need a real resource to exist before they run.
Instead of hardcoding IDs (which break when the server resets), the agent detects
CREATE→READ→DELETE chains and generates session-scoped fixtures that create
resources at the start and clean them up at the end — guaranteed.

### Why swappable LLM backends?

Locally, Ollama + deepseek-r1 keeps costs at zero during development.
In production or CI, a single env var swap enables OpenAI for higher quality.
The architecture never leaks LLM-specific code into business logic.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Test generation | Jinja2 templates |
| API probing | Python requests |
| Test execution | Pytest |
| Self-healing LLM | LangChain + Ollama (deepseek-r1) |
| CLI | argparse + Rich |
| Logging | Python logging |
| CI/CD | GitHub Actions |
| Spec parsing | PyYAML + custom parser |

---

## Author

**Richa Sahu** — Senior SDET with 11+ years of experience in API automation,
Python testing, and enterprise QA across Dell, HPE, Akamai, Visa, and NetApp.

- GitHub: [github.com/richa-sahu](https://github.com/richa-sahu)
- LinkedIn: [linkedin.com/in/richasahu27](https://linkedin.com/in/richasahu27)

