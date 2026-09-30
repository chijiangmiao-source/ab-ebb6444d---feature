"""HTTP API for the on-board parameter multi-version serialisation audit.

Endpoints
---------
GET  /healthz                     -> trivial health probe (Compose healthcheck)
GET  /                        -> the audit console page (static/index.html)
POST /api/audits              -> freeze + analyse a payload
GET  /api/audits/<audit_id>   -> fetch a frozen verdict
GET  /api/audits              -> list frozen audit ids
POST /api/resolutions         -> submit a stable disposition for a frozen
                                 cyclic audit (exact minimum feedback vertex
                                 set); replays / conflicts by resolution id
GET  /api/resolutions/<id>    -> fetch a frozen disposition plan
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mvscc import (  # noqa: E402
    PayloadError,
    analyze_normalized,
    build_resolution,
    validate_payload,
)
from store import (  # noqa: E402
    ConflictError,
    FrozenStore,
    ResolutionConflictError,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class AuditHandler(BaseHTTPRequestHandler):
    server_version = "MVSCC-Audit/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ------------------------------------------------------------------
    def _send_json(self, status, body, extra_headers=None):
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, text):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/api/audits":
            self._send_json(200, {"audit_ids": self.server.store.ids()})
            return
        if path.startswith("/api/audits/"):
            audit_id = path[len("/api/audits/") :]
            if "/" in audit_id or not audit_id:
                self._send_json(404, {"error": "NOT_FOUND"})
                return
            record = self.server.store.get(audit_id)
            if record is None:
                self._send_json(404, {"error": "AUDIT_NOT_FOUND", "audit_id": audit_id})
                return
            self._send_json(200, record["verdict"])
            return
        if path == "/api/resolutions":
            self._send_json(404, {"error": "NOT_FOUND", "message": "use POST to submit a resolution"})
            return
        if path.startswith("/api/resolutions/"):
            resolution_id = path[len("/api/resolutions/") :]
            if "/" in resolution_id or not resolution_id:
                self._send_json(404, {"error": "NOT_FOUND"})
                return
            record = self.server.store.get_resolution(resolution_id)
            if record is None:
                self._send_json(
                    404,
                    {"error": "RESOLUTION_NOT_FOUND", "resolution_id": resolution_id},
                )
                return
            self._send_json(200, record["plan"])
            return
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        self._send_json(404, {"error": "NOT_FOUND"})

    def _serve_index(self):
        index_path = STATIC_DIR / "index.html"
        try:
            text = index_path.read_text(encoding="utf-8")
        except OSError:
            self._send_json(500, {"error": "CONSOLE_MISSING"})
            return
        self._send_html(text)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/resolutions":
            self._handle_resolution()
            return
        if path != "/api/audits":
            self._send_json(404, {"error": "NOT_FOUND"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "BAD_JSON", "message": "request body must be valid JSON"})
            return

        try:
            norm = validate_payload(payload)
            verdict, adjacency = analyze_normalized(norm)
        except PayloadError as exc:
            self._send_json(400, {"error": "INVALID_PAYLOAD", "message": str(exc)})
            return
        except Exception as exc:  # never leak a stack trace as a 200
            sys.stderr.write("analysis failure: %r\n" % (exc,))
            self._send_json(500, {"error": "ANALYSIS_FAILED", "message": str(exc)})
            return

        # A read of a stale/impossible version is an input error: it is
        # reported (deterministically, so an identical retry yields the
        # identical diagnosis) but never frozen as an audit verdict.
        if verdict["status"] == "INVALID_READ":
            self._send_json(
                422,
                {
                    "error": "STALE_VERSION_READ",
                    "message": (
                        "at least one read did not observe the latest version "
                        "committed before its transaction started"
                    ),
                    "verdict": verdict,
                },
            )
            return

        # The normalised payload and the canonical MVSG (with its edge
        # evidence) are frozen with the verdict so dispositions never rewrite
        # or recompute the source audit.
        try:
            stored, replayed = self.server.store.submit(payload, verdict, norm, adjacency)
        except ConflictError as exc:
            self._send_json(
                409,
                {
                    "error": "AUDIT_ID_CONFLICT",
                    "message": str(exc),
                    "audit_id": exc.audit_id,
                },
            )
            return

        self._send_json(
            200 if replayed else 201,
            stored,
            [("X-Audit-Replayed", "true" if replayed else "false")],
        )

    # ------------------------------------------------------------------
    def _handle_resolution(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "BAD_JSON", "message": "request body must be valid JSON"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "INVALID_PAYLOAD", "message": "request body must be a JSON object"})
            return
        source_audit_id = body.get("source_audit_id")
        resolution_id = body.get("resolution_id")
        if not isinstance(source_audit_id, str) or not source_audit_id:
            self._send_json(400, {"error": "INVALID_PAYLOAD",
                                  "message": "source_audit_id must be a non-empty string"})
            return
        if not isinstance(resolution_id, str) or not resolution_id:
            self._send_json(400, {"error": "INVALID_PAYLOAD",
                                  "message": "resolution_id must be a non-empty string"})
            return
        if len(resolution_id) > 128:
            self._send_json(400, {"error": "INVALID_PAYLOAD",
                                  "message": "resolution_id must be at most 128 characters"})
            return

        # A resolution id, once bound to a source, is immutable: an identical
        # retransmission replays the frozen plan without recomputing; reusing
        # the id for another source is rejected before touching the evidence.
        existing_resolution = self.server.store.get_resolution(resolution_id)
        if existing_resolution is not None:
            if existing_resolution["source"] != source_audit_id:
                self._send_json(
                    409,
                    {"error": "RESOLUTION_ID_CONFLICT",
                     "message": (
                         f"resolution_id {resolution_id!r} is already bound to "
                         f"source audit {existing_resolution['source']!r} and "
                         f"cannot be rebound to {source_audit_id!r}"
                     ),
                     "resolution_id": resolution_id,
                     "existing_source_audit_id": existing_resolution["source"],
                     "requested_source_audit_id": source_audit_id},
                )
                return
            self._send_json(
                200,
                existing_resolution["plan"],
                [("X-Resolution-Replayed", "true")],
            )
            return

        record = self.server.store.frozen_evidence(source_audit_id)
        if record is None:
            self._send_json(
                404,
                {"error": "SOURCE_AUDIT_NOT_FOUND",
                 "message": "the source audit must be frozen before a disposition",
                 "source_audit_id": source_audit_id},
            )
            return

        source_status = record["verdict"]["status"]
        if source_status != "NOT_SERIALIZABLE":
            self._send_json(
                409,
                {"error": "SOURCE_NOT_CYCLIC",
                 "message": (
                     "a stable disposition requires a frozen MVSG with a closed "
                     f"cycle; source status is {source_status}"
                 ),
                 "source_audit_id": source_audit_id,
                 "source_status": source_status},
            )
            return

        plan = build_resolution(
            record["norm"], record["adjacency"], source_audit_id, resolution_id
        )
        try:
            stored, replayed = self.server.store.submit_resolution(
                resolution_id, source_audit_id, plan
            )
        except ResolutionConflictError as exc:
            self._send_json(
                409,
                {"error": "RESOLUTION_ID_CONFLICT",
                 "message": str(exc),
                 "resolution_id": exc.resolution_id,
                 "existing_source_audit_id": exc.old_source,
                 "requested_source_audit_id": exc.new_source},
            )
            return

        self._send_json(
            200 if replayed else 201,
            stored,
            [("X-Resolution-Replayed", "true" if replayed else "false")],
        )


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), AuditHandler)
    server.store = FrozenStore()
    return server


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    if argv:
        port = int(argv[0])
    server = make_server(host, port)
    sys.stderr.write("mvscc audit listening on http://%s:%d\n" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
