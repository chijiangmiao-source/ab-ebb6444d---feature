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
        # resolution_id -> {"source": audit_id, "fingerprint": str}
        self._resolutions: dict[str, dict] = {}

    def submit(self, payload: dict, verdict: dict, norm: dict | None = None,
               adjacency: dict | None = None) -> tuple[dict, bool]:
        """Freeze or replay.

        Returns ``(record, replayed)``.  ``replayed`` is True when an
        identical payload was already frozen.  Raises ``ConflictError`` when
        the id exists but the payload differs.

        On first freeze, ``norm`` (the normalised payload) and ``adjacency``
        (the canonical MVSG with per-pair edge evidence, ``None`` for an
        INVALID_READ verdict) are frozen alongside the verdict so later
        dispositions work purely from frozen evidence.
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
                "verdict": copy.deepcopy(verdict),
                "norm": copy.deepcopy(norm) if norm is not None else None,
                "adjacency": copy.deepcopy(adjacency),
            }
            self._records[audit_id] = record
            return copy.deepcopy(verdict), False

    def get(self, audit_id: str) -> dict | None:
        with self._lock:
            existing = self._records.get(audit_id)
            return copy.deepcopy(existing) if existing else None

    def frozen_evidence(self, audit_id: str) -> dict | None:
        """Return ``(verdict, norm, adjacency)`` for a frozen audit, or
        ``None`` when the id was never frozen."""
        with self._lock:
            existing = self._records.get(audit_id)
            if existing is None:
                return None
            return {
                "verdict": copy.deepcopy(existing["verdict"]),
                "norm": copy.deepcopy(existing["norm"]),
                "adjacency": copy.deepcopy(existing["adjacency"]),
            }

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._records)

    def submit_resolution(self, resolution_id: str, source_audit_id: str,
                          plan: dict) -> tuple[dict, bool]:
        """Freeze (or replay) a stable-disposition plan.

        Returns ``(plan, replayed)``.  Re-transmission of the same
        ``resolution_id`` for the same source replays; reusing the id for a
        *different* source raises :class:`ResolutionConflictError` and never
        overwrites the frozen plan.
        """
        with self._lock:
            existing = self._resolutions.get(resolution_id)
            if existing is not None:
                if existing["source"] != source_audit_id:
                    raise ResolutionConflictError(
                        resolution_id, existing["source"], source_audit_id
                    )
                return copy.deepcopy(existing["plan"]), True
            self._resolutions[resolution_id] = {
                "source": source_audit_id,
                "plan": copy.deepcopy(plan),
            }
            return copy.deepcopy(plan), False

    def get_resolution(self, resolution_id: str) -> dict | None:
        with self._lock:
            existing = self._resolutions.get(resolution_id)
            return copy.deepcopy(existing) if existing else None


class ConflictError(Exception):
    def __init__(self, audit_id: str) -> None:
        super().__init__(f"audit_id {audit_id!r} is already frozen with a different payload")
        self.audit_id = audit_id


class ResolutionConflictError(Exception):
    def __init__(self, resolution_id: str, old_source: str, new_source: str) -> None:
        super().__init__(
            f"resolution_id {resolution_id!r} is already bound to source audit "
            f"{old_source!r} and cannot be rebound to {new_source!r}"
        )
        self.resolution_id = resolution_id
        self.old_source = old_source
        self.new_source = new_source
