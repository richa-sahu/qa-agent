"""
dependency_resolver.py
Analyses parsed endpoints and detects CREATE → READ → UPDATE → DELETE chains.
Generic — works for any REST API, no hardcoded resource names.

Chain detection rules:
  - Creator:  POST /resource        (no path params, has request body)
  - Reader:   GET  /resource/{id}   (has path params, path prefix matches creator)
  - Updater:  PUT  /resource/{id}   (has path params + request body)
  - Deleter:  DELETE /resource/{id} (has path params, no request body)

Output: list of ResourceChain objects, one per detected resource.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent.logger import get_logger

logger = get_logger("dependency_resolver", "qa_agent.log")


@dataclass
class ResourceChain:
    """
    Represents a detected CREATE → READ → UPDATE → DELETE chain
    for a single resource.
    """
    resource_name: str          # e.g. "booking", "pet", "order"
    base_path: str              # e.g. "/booking"
    creator: dict | None        # POST endpoint descriptor
    readers: list[dict]         # GET /{id} endpoint descriptors
    updaters: list[dict]        # PUT/PATCH /{id} endpoint descriptors
    deleters: list[dict]        # DELETE /{id} endpoint descriptors
    id_field: str = "id"        # field name in creator response containing the ID
    fixture_name: str = ""      # e.g. "created_booking"

    def __post_init__(self):
        if not self.fixture_name:
            self.fixture_name = f"created_{self.resource_name}"

    @property
    def has_chain(self) -> bool:
        """True if we have at least a creator + one consumer."""
        return (
            self.creator is not None
            and (self.readers or self.updaters or self.deleters)
        )


def _get_path_prefix(path: str) -> str:
    """
    Extract the base resource path — everything before the first path param.
    e.g. /booking/{id}  → /booking
         /pet/{petId}   → /pet
         /orders/{id}/items/{itemId} → /orders
    """
    match = re.match(r"^(/[^{]+)", path)
    return match.group(1).rstrip("/") if match else path


def _get_resource_name(base_path: str) -> str:
    """
    Extract resource name from base path.
    e.g. /booking → booking
         /api/v1/pets → pets
    """
    parts = base_path.strip("/").split("/")
    return parts[-1] if parts else "resource"


def _infer_id_field(creator_response_body: Any, resource_name: str) -> str:
    """
    Infer the ID field name from the creator response body.
    Checks common patterns: bookingid, id, petId, orderId, etc.
    """
    if not isinstance(creator_response_body, dict):
        return "id"

    # Check direct ID fields
    candidates = [
        f"{resource_name}id",      # bookingid
        f"{resource_name}_id",     # booking_id
        "id",                       # id
        f"{resource_name}Id",      # bookingId
    ]
    for candidate in candidates:
        if candidate in creator_response_body:
            logger.debug(f"Inferred ID field: '{candidate}' for {resource_name}")
            return candidate

    # Check nested — e.g. {"booking": {"id": 1}}
    for key, value in creator_response_body.items():
        if isinstance(value, dict) and "id" in value:
            logger.debug(
                f"Inferred nested ID field: '{key}.id' for {resource_name}"
            )
            return f"{key}.id"

    return "id"


def detect_chains(endpoints: list[dict]) -> list[ResourceChain]:
    """
    Analyse all endpoints and return detected resource chains.
    Each chain groups related CREATE/READ/UPDATE/DELETE endpoints.
    """
    logger.info(f"Detecting resource chains from {len(endpoints)} endpoints")

    # Group endpoints by base path
    groups: dict[str, dict] = {}

    for ep in endpoints:
        path = ep.get("path", "")
        method = ep.get("method", "").upper()
        has_path_params = bool(ep.get("path_params"))
        has_request_body = bool(ep.get("request_body"))
        base_path = _get_path_prefix(path)

        if base_path not in groups:
            groups[base_path] = {
                "creator": None,
                "readers": [],
                "updaters": [],
                "deleters": [],
            }

        # Creator: POST with no path params and a request body
        if method == "POST" and not has_path_params and has_request_body:
            groups[base_path]["creator"] = ep
            logger.debug(f"Creator detected: POST {path}")

        # Reader: GET with path params
        elif method == "GET" and has_path_params:
            groups[base_path]["readers"].append(ep)
            logger.debug(f"Reader detected: GET {path}")

        # Updater: PUT/PATCH with path params
        elif method in ("PUT", "PATCH") and has_path_params:
            groups[base_path]["updaters"].append(ep)
            logger.debug(f"Updater detected: {method} {path}")

        # Deleter: DELETE with path params
        elif method == "DELETE" and has_path_params:
            groups[base_path]["deleters"].append(ep)
            logger.debug(f"Deleter detected: DELETE {path}")

    # Build ResourceChain objects
    chains = []
    for base_path, group in groups.items():
        resource_name = _get_resource_name(base_path)
        chain = ResourceChain(
            resource_name=resource_name,
            base_path=base_path,
            creator=group["creator"],
            readers=group["readers"],
            updaters=group["updaters"],
            deleters=group["deleters"],
        )

        if chain.has_chain:
            logger.info(
                f"Chain detected: {resource_name} — "
                f"creator={'✓' if chain.creator else '✗'} "
                f"readers={len(chain.readers)} "
                f"updaters={len(chain.updaters)} "
                f"deleters={len(chain.deleters)}"
            )
            chains.append(chain)
        else:
            logger.debug(
                f"No chain for {resource_name} — "
                f"missing creator or consumers"
            )

    logger.info(f"Detected {len(chains)} resource chain(s)")
    return chains


def get_chained_endpoints(endpoints: list[dict]) -> dict[str, str]:
    """
    Returns a mapping of operation_id → fixture_name for endpoints
    that are part of a detected chain.

    e.g. {
        "getBooking": "created_booking",
        "updateBooking": "created_booking",
        "deleteBooking": "created_booking",
    }
    """
    chains = detect_chains(endpoints)
    mapping = {}

    for chain in chains:
        for ep_list in (chain.readers, chain.updaters, chain.deleters):
            for ep in ep_list:
                op_id = ep.get("operation_id", "")
                mapping[op_id] = chain.fixture_name
                logger.debug(
                    f"Mapped {op_id} → fixture '{chain.fixture_name}'"
                )

    return mapping