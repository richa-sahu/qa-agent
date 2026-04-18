"""
logger.py
Centralized logging for qa-agent.

Log files:
  logs/qa_agent.log      — general pipeline (CLI, spec parser)
  logs/api_prober.log    — all HTTP calls, payloads, responses
  logs/generator.log     — test generation, template context
  logs/test_runner.log   — pytest results, failures
  logs/healer.log        — heal attempts, LLM outputs, outcomes
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)

# ── Log directory ─────────────────────────────────────────────────────────────

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

# ── Formatter ─────────────────────────────────────────────────────────────────

FILE_FORMATTER = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Factory ───────────────────────────────────────────────────────────────────

def get_logger(name: str, log_file: str) -> logging.Logger:
    """
    Get a named logger that writes to a specific log file.
    Safe to call multiple times — never adds duplicate handlers.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    log_path = LOGS_DIR / log_file
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(FILE_FORMATTER)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


# ── Module loggers ────────────────────────────────────────────────────────────

def get_cli_logger() -> logging.Logger:
    return get_logger("cli", "qa_agent.log")

def get_spec_logger() -> logging.Logger:
    return get_logger("spec_parser", "qa_agent.log")

def get_prober_logger() -> logging.Logger:
    return get_logger("api_prober", "api_prober.log")

def get_generator_logger() -> logging.Logger:
    return get_logger("template_generator", "generator.log")

def get_runner_logger() -> logging.Logger:
    return get_logger("test_runner", "test_runner.log")

def get_healer_logger() -> logging.Logger:
    return get_logger("self_healer", "healer.log")