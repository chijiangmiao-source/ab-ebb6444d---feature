"""Frozen audit-record store.

An audit id, once frozen, is immutable for the lifetime of the service:

* re-submission of the *same* payload replays the stored verdict;
* submission of a *different* payload under the same id is rejected with
  ``409 CONFLICT`` and never overwrites the frozen record.

Payload identity is compared over a canonical JSON encoding so that
formatting differences do not matter.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading


def canonical_fingerprint(payload: dict) -> str:
    """Stable SHA-256 over the canonical JSON encoding of a payload."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class FrozenStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}

    def submit(self, payload: dict, verdict: dict) -> tuple[dict, bool]:
        """Freeze or replay.

        Returns ``(record, replayed)``.  ``replayed`` is True when an
        identical payload was already frozen.  Raises ``ConflictError`` when
        the id exists but the payload differs.
        """
        audit_id = payload["audit_id"]
        fingerprint = canonical_fingerprint(payload)
        with self._lock:
            existing = self._records.get(audit_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ConflictError(audit_id)
                return copy.deepcopy(existing["verdict"]), True
            record = {
                "audit_id": audit_id,
                "fingerprint": fingerprint,
                "payload": copy.deepcopy(payload),
                "verdict": copy.deepcopy(verdict),
            }
            self._records[audit_id] = record
            return copy.deepcopy(verdict), False

    def get(self, audit_id: str) -> dict | None:
        with self._lock:
            existing = self._records.get(audit_id)
            return copy.deepcopy(existing) if existing else None

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._records)


class ConflictError(Exception):
    def __init__(self, audit_id: str) -> None:
        super().__init__(f"audit_id {audit_id!r} is already frozen with a different payload")
        self.audit_id = audit_id


class ResolutionStore:
    """Stable-resolution plans keyed by an idempotent resolution marker.

    A resolution marker is permanently bound to its source audit:

    * re-submission against the *same* frozen source replays the stored plan;
    * re-submission of the same marker against a *different* source is
      rejected with ``ResolutionSourceError`` and never overwrites the plan.

    The source audit itself is never rewritten -- plans live in this separate
    store.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}

    def submit(self, resolution_id: str, audit_id: str, fingerprint: str,
               plan: dict) -> tuple[dict, bool]:
        with self._lock:
            existing = self._records.get(resolution_id)
            if existing is not None:
                if existing["audit_id"] != audit_id or existing["fingerprint"] != fingerprint:
                    raise ResolutionSourceError(
                        resolution_id, existing["audit_id"], audit_id
                    )
                return copy.deepcopy(existing["plan"]), True
            record = {
                "resolution_id": resolution_id,
                "audit_id": audit_id,
                "fingerprint": fingerprint,
                "plan": copy.deepcopy(plan),
            }
            self._records[resolution_id] = record
            return copy.deepcopy(plan), False

    def get(self, resolution_id: str) -> dict | None:
        with self._lock:
            existing = self._records.get(resolution_id)
            return copy.deepcopy(existing) if existing else None

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._records)


class ResolutionSourceError(Exception):
    def __init__(self, resolution_id: str, old_audit_id: str,
                 new_audit_id: str) -> None:
        super().__init__(
            f"resolution marker {resolution_id!r} is already bound to source "
            f"audit {old_audit_id!r}, cannot rebind it to {new_audit_id!r}"
        )
        self.resolution_id = resolution_id
        self.old_audit_id = old_audit_id
        self.new_audit_id = new_audit_id
