"""Tests for the stable-disposition feature: exact minimum feedback vertex
set on a frozen MVSG, residual serial order and freeze/replay/conflict
semantics."""

import itertools
import json
import random
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import _bootstrap  # noqa: F401  (sys.path setup)
from mvscc import (
    analyze_normalized,
    build_resolution,
    minimum_feedback_vertex_set,
    validate_payload,
)
from server import make_server
from store import FrozenStore, ResolutionConflictError


def skew_payload(audit_id="cyc", ids=("T1", "T2")):
    """Classic write skew: a 2-cycle of rw edges between the two txns."""
    a, b = ids
    return {
        "audit_id": audit_id,
        "initial": {"x": 100, "y": 100},
        "transactions": [
            {"id": a, "start": 1, "commit": 5, "steps": [
                {"op": "read", "key": "x", "observed": "initial"},
                {"op": "write", "key": "y", "value": -100}]},
            {"id": b, "start": 2, "commit": 6, "steps": [
                {"op": "read", "key": "y", "observed": "initial"},
                {"op": "write", "key": "x", "value": -100}]},
        ],
    }


def double_cycle_payload(audit_id="double"):
    """Two 2-cycles sharing T2: T1<->T2 (keys x,y) and T2<->T3 (keys p,q).

    Revoking the shared T2 breaks both cycles with one transaction; revoking
    T1 and T3 needs two.  Every read observes the initial version, which is
    legal because every transaction starts at 1.
    """
    return {
        "audit_id": audit_id,
        "initial": {"x": 0, "y": 0, "p": 0, "q": 0},
        "transactions": [
            {"id": "T1", "start": 1, "commit": 5, "steps": [
                {"op": "read", "key": "x", "observed": "initial"},
                {"op": "write", "key": "y", "value": 1}]},
            {"id": "T2", "start": 1, "commit": 6, "steps": [
                {"op": "read", "key": "y", "observed": "initial"},
                {"op": "read", "key": "p", "observed": "initial"},
                {"op": "write", "key": "x", "value": 2},
                {"op": "write", "key": "q", "value": 2}]},
            {"id": "T3", "start": 1, "commit": 7, "steps": [
                {"op": "read", "key": "q", "observed": "initial"},
                {"op": "write", "key": "p", "value": 3}]},
        ],
    }


def disjoint_cycles_payload(audit_id="disjoint"):
    """Two disjoint 2-cycles (T1,T2) and (T3,T4) over four keys."""
    return {
        "audit_id": audit_id,
        "initial": {"k1": 0, "k2": 0, "k3": 0, "k4": 0},
        "transactions": [
            {"id": "T1", "start": 1, "commit": 5, "steps": [
                {"op": "read", "key": "k1", "observed": "initial"},
                {"op": "write", "key": "k2", "value": 1}]},
            {"id": "T2", "start": 1, "commit": 6, "steps": [
                {"op": "read", "key": "k2", "observed": "initial"},
                {"op": "write", "key": "k1", "value": 2}]},
            {"id": "T3", "start": 1, "commit": 7, "steps": [
                {"op": "read", "key": "k3", "observed": "initial"},
                {"op": "write", "key": "k4", "value": 3}]},
            {"id": "T4", "start": 1, "commit": 8, "steps": [
                {"op": "read", "key": "k4", "observed": "initial"},
                {"op": "write", "key": "k3", "value": 4}]},
        ],
    }


def analyzed(payload):
    norm = validate_payload(payload)
    verdict, adjacency = analyze_normalized(norm)
    return norm, verdict, adjacency


# ---------------------------------------------------------------------------
# Exact solver
# ---------------------------------------------------------------------------

def brute_force_fvs(adj):
    nodes = sorted(adj)

    def acyclic(removed):
        kept = [n for n in nodes if n not in removed]
        ks = set(kept)
        indeg = {n: 0 for n in kept}
        for u in kept:
            for v in adj[u]:
                if v in ks:
                    indeg[v] += 1
        queue = [n for n, d in indeg.items() if d == 0]
        seen = 0
        while queue:
            x = queue.pop()
            seen += 1
            for y in adj[x]:
                if y in ks:
                    indeg[y] -= 1
                    if indeg[y] == 0:
                        queue.append(y)
        return seen == len(kept)

    best = None
    for size in range(len(nodes) + 1):
        for combo in itertools.combinations(nodes, size):
            if acyclic(set(combo)):
                cand = list(combo)
                if best is None or cand < best:
                    best = cand
        if best is not None:
            return best
    return best  # pragma: no cover


class MinimumFeedbackVertexSetTests(unittest.TestCase):
    def test_acyclic_graph_returns_empty(self):
        adj = {"A": {"B": []}, "B": {"C": []}, "C": {}}
        self.assertEqual(minimum_feedback_vertex_set(adj), [])

    def test_simple_two_cycle_tie_breaks_to_smaller_id(self):
        adj = {"T1": {"T2": []}, "T2": {"T1": []}}
        # both {T1} and {T2} are optimal; set-lex prefers revoking T1
        self.assertEqual(minimum_feedback_vertex_set(adj), ["T1"])

    def test_double_cycle_sharing_a_node_revokes_the_shared_node(self):
        # T1<->T2 and T2<->T3: {T2} beats {T1,T3}
        adj = {
            "T1": {"T2": []},
            "T2": {"T1": [], "T3": []},
            "T3": {"T2": []},
        }
        self.assertEqual(minimum_feedback_vertex_set(adj), ["T2"])

    def test_disjoint_cycles_combine_independent_optima(self):
        adj = {
            "T1": {"T2": []}, "T2": {"T1": []},
            "T3": {"T4": []}, "T4": {"T3": []},
        }
        self.assertEqual(minimum_feedback_vertex_set(adj), ["T1", "T3"])

    def test_directed_triangle_needs_one_revocation(self):
        adj = {"A": {"B": []}, "B": {"C": []}, "C": {"A": []}}
        self.assertEqual(minimum_feedback_vertex_set(adj), ["A"])

    def test_two_disjoint_triangles(self):
        adj = {
            "A": {"B": []}, "B": {"C": []}, "C": {"A": []},
            "D": {"E": []}, "E": {"F": []}, "F": {"D": []},
        }
        self.assertEqual(minimum_feedback_vertex_set(adj), ["A", "D"])

    def test_exactness_against_brute_force_on_random_graphs(self):
        rng = random.Random(424242)
        for _ in range(200):
            n = rng.randint(1, 8)
            nodes = [f"v{i}" for i in range(n)]
            raw = {v: set() for v in nodes}
            for a, b in itertools.permutations(nodes, 2):
                if rng.random() < 0.25:
                    raw[a].add(b)
            adj = {a: {b: [] for b in outs} for a, outs in raw.items()}
            self.assertEqual(
                minimum_feedback_vertex_set(adj), brute_force_fvs(adj), adj
            )

    def test_handles_twenty_four_vertex_graph(self):
        # a single long directed cycle: exactly one revocation suffices
        n = 24
        nodes = [f"T{i:02d}" for i in range(n)]
        adj = {nodes[i]: {nodes[(i + 1) % n]: []} for i in range(n)}
        got = minimum_feedback_vertex_set(adj)
        self.assertEqual(got, ["T00"])


# ---------------------------------------------------------------------------
# Resolution built from a real frozen MVSG
# ---------------------------------------------------------------------------

class BuildResolutionTests(unittest.TestCase):
    def test_write_skew_plan(self):
        norm, verdict, adjacency = analyzed(skew_payload())
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")
        plan = build_resolution(norm, adjacency, "cyc", "fix-1")
        self.assertEqual(plan["status"], "RESOLVED")
        self.assertEqual(plan["source_audit_id"], "cyc")
        self.assertEqual(plan["resolution_id"], "fix-1")
        self.assertEqual(plan["revoked_transactions"], ["T1"])
        self.assertEqual(plan["revoked_count"], 1)
        self.assertEqual(plan["retained_transactions"], ["T2"])
        self.assertEqual(plan["serial_order"], ["T2"])
        # nothing survives between the retained transactions (only T2 kept)
        self.assertEqual(plan["residual_edges"], [])
        # T2 writes x=-100 and leaves y at its initial value
        self.assertEqual(plan["recomputation"]["final_state"], {"x": -100, "y": 100})

    def test_shared_node_double_cycle_revokes_one_transaction(self):
        norm, verdict, adjacency = analyzed(double_cycle_payload())
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")
        plan = build_resolution(norm, adjacency, "double", "fix-d")
        self.assertEqual(
            plan["revoked_transactions"], ["T2"],
            "the shared transaction must be the unique minimum",
        )
        self.assertEqual(plan["retained_transactions"], ["T1", "T3"])
        # residual graph has no edges: the two cycles were all rw edges
        self.assertEqual(plan["residual_edges"], [])
        self.assertEqual(plan["serial_order"], ["T1", "T3"])
        final = plan["recomputation"]["final_state"]
        self.assertEqual(final, {"x": 0, "y": 1, "p": 3, "q": 0})

    def test_parallel_optima_adjudicated_by_id(self):
        norm, verdict, adjacency = analyzed(disjoint_cycles_payload())
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")
        plan = build_resolution(norm, adjacency, "disjoint", "fix-t")
        # one revocation per cycle; the smaller id wins each tie
        self.assertEqual(plan["revoked_transactions"], ["T1", "T3"])
        self.assertEqual(plan["retained_transactions"], ["T2", "T4"])
        self.assertEqual(plan["serial_order"], ["T2", "T4"])
        final = plan["recomputation"]["final_state"]
        self.assertEqual(final, {"k1": 2, "k2": 0, "k3": 4, "k4": 0})

    def test_residual_edges_only_span_retained_transactions(self):
        # add an acyclic tail T3 <- wr/witness - ... to the skew cycle so the
        # residual graph after revocation still carries an evidence edge
        payload = skew_payload()
        payload["transactions"].append(
            {"id": "T3", "start": 7, "commit": 9, "steps": [
                {"op": "read", "key": "x",
                 "observed": {"source": "txn", "writer": "T2"}}]}
        )
        norm, verdict, adjacency = analyzed(payload)
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")
        plan = build_resolution(norm, adjacency, payload["audit_id"], "fix")
        self.assertEqual(plan["revoked_transactions"], ["T1"])
        # the wr edge T2 -> T3 survives and must be reported
        self.assertEqual(
            [(e["from"], e["to"], e["type"]) for e in plan["residual_edges"]],
            [("T2", "T3", "wr")],
        )
        self.assertEqual(plan["serial_order"], ["T2", "T3"])

    def test_source_payload_is_not_mutated_by_resolution(self):
        norm, verdict, adjacency = analyzed(skew_payload())
        before = json.dumps(verdict, sort_keys=True)
        build_resolution(norm, adjacency, "cyc", "fix")
        after = json.dumps(verdict, sort_keys=True)
        self.assertEqual(before, after)
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")


# ---------------------------------------------------------------------------
# Store: resolution freeze / replay / source rebind conflict
# ---------------------------------------------------------------------------

class ResolutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = FrozenStore()
        _, verdict, adjacency = analyzed(skew_payload("src-a"))
        self.store.submit(skew_payload("src-a"), verdict,
                          validate_payload(skew_payload("src-a")), adjacency)
        self.plan = {"status": "RESOLVED", "revoked_transactions": ["T1"]}

    def test_first_freezes_identical_retransmission_replays(self):
        plan, replayed = self.store.submit_resolution("r1", "src-a", self.plan)
        self.assertFalse(replayed)
        self.assertEqual(plan["revoked_transactions"], ["T1"])
        plan2, replayed2 = self.store.submit_resolution("r1", "src-a", self.plan)
        self.assertTrue(replayed2)
        self.assertEqual(plan2, plan)

    def test_reusing_resolution_id_for_another_source_is_rejected(self):
        self.store.submit_resolution("r1", "src-a", self.plan)
        with self.assertRaises(ResolutionConflictError) as ctx:
            self.store.submit_resolution("r1", "src-b", {"status": "RESOLVED"})
        self.assertEqual(ctx.exception.old_source, "src-a")
        self.assertEqual(ctx.exception.new_source, "src-b")
        # the original binding is intact
        record = self.store.get_resolution("r1")
        self.assertEqual(record["source"], "src-a")
        self.assertEqual(record["plan"], self.plan)

    def test_distinct_resolution_ids_are_independent(self):
        self.store.submit_resolution("r1", "src-a", self.plan)
        _, replayed = self.store.submit_resolution("r2", "src-a", self.plan)
        self.assertFalse(replayed)

    def test_get_missing_resolution_is_none(self):
        self.assertIsNone(self.store.get_resolution("ghost"))


# ---------------------------------------------------------------------------
# HTTP end to end
# ---------------------------------------------------------------------------

class ResolutionHttpTests(unittest.TestCase):
    def setUp(self):
        self.server: ThreadingHTTPServer = make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path, body=None, expect_error=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if not expect_error:
                raise
            return exc.code, dict(exc.headers), json.loads(exc.read().decode())

    def freeze(self, payload):
        status, _, verdict = self.request("POST", "/api/audits", payload)
        self.assertIn(status, (200, 201))
        return verdict

    def test_full_shared_node_double_cycle_disposition(self):
        self.freeze(double_cycle_payload())
        status, headers, body = self.request(
            "POST", "/api/resolutions",
            {"source_audit_id": "double", "resolution_id": "fix-d"})
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("X-Resolution-Replayed"), "false")
        self.assertEqual(body["revoked_transactions"], ["T2"])
        self.assertEqual(body["serial_order"], ["T1", "T3"])
        self.assertEqual(body["residual_edges"], [])

        # replay
        status2, headers2, body2 = self.request(
            "POST", "/api/resolutions",
            {"source_audit_id": "double", "resolution_id": "fix-d"})
        self.assertEqual(status2, 200)
        self.assertEqual(headers2.get("X-Resolution-Replayed"), "true")
        self.assertEqual(body2, body)

        # fetch by resolution id
        status3, _, body3 = self.request("GET", "/api/resolutions/fix-d")
        self.assertEqual(status3, 200)
        self.assertEqual(body3, body)

    def test_parallel_optima_tie_break_over_http(self):
        self.freeze(disjoint_cycles_payload())
        status, _, body = self.request(
            "POST", "/api/resolutions",
            {"source_audit_id": "disjoint", "resolution_id": "fix-t"})
        self.assertEqual(status, 201)
        self.assertEqual(body["revoked_transactions"], ["T1", "T3"])
        self.assertEqual(body["revoked_count"], 2)

    def test_acyclic_source_is_rejected_but_remains_readable(self):
        serial = {
            "audit_id": "ser",
            "initial": {"x": 1},
            "transactions": [
                {"id": "T1", "start": 1, "commit": 3, "steps": [
                    {"op": "write", "key": "x", "value": 2}]}],
        }
        self.freeze(serial)
        status, _, body = self.request(
            "POST", "/api/resolutions",
            {"source_audit_id": "ser", "resolution_id": "fix-s"},
            expect_error=True)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "SOURCE_NOT_CYCLIC")
        self.assertEqual(body["source_status"], "SERIALIZABLE")
        # no resolution was frozen
        self.request("GET", "/api/resolutions/fix-s", expect_error=True)

    def test_missing_source_is_rejected(self):
        status, _, body = self.request(
            "POST", "/api/resolutions",
            {"source_audit_id": "never-frozen", "resolution_id": "r"},
            expect_error=True)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "SOURCE_AUDIT_NOT_FOUND")

    def test_resolution_id_rebind_to_other_source_conflicts(self):
        self.freeze(double_cycle_payload())
        self.freeze(skew_payload("other-skew"))
        req = {"resolution_id": "shared-rid"}
        status, _, _ = self.request(
            "POST", "/api/resolutions",
            {**req, "source_audit_id": "double"})
        self.assertEqual(status, 201)
        status2, _, body2 = self.request(
            "POST", "/api/resolutions",
            {**req, "source_audit_id": "other-skew"}, expect_error=True)
        self.assertEqual(status2, 409)
        self.assertEqual(body2["error"], "RESOLUTION_ID_CONFLICT")
        self.assertEqual(body2["existing_source_audit_id"], "double")
        self.assertEqual(body2["requested_source_audit_id"], "other-skew")
        # the first plan is still what the id replays
        status3, _, body3 = self.request(
            "POST", "/api/resolutions",
            {**req, "source_audit_id": "double"})
        self.assertEqual(status3, 200)
        self.assertEqual(body3["revoked_transactions"], ["T2"])

    def test_original_audit_verdict_still_readable_after_resolution(self):
        original = self.freeze(skew_payload())
        self.request("POST", "/api/resolutions",
                     {"source_audit_id": "cyc", "resolution_id": "fix-c"})
        status, _, fetched = self.request("GET", "/api/audits/cyc")
        self.assertEqual(status, 200)
        self.assertEqual(fetched, original)
        self.assertEqual(fetched["status"], "NOT_SERIALIZABLE")
        self.assertEqual(fetched["cycle"]["vertices"], ["T1", "T2"])
        # full edge evidence of the source is untouched
        self.assertTrue(fetched["edges"])

    def test_bad_resolution_request_shapes_are_400(self):
        for bad in ({}, {"source_audit_id": "x"}, {"resolution_id": "y"},
                    {"source_audit_id": 1, "resolution_id": "y"},
                    {"source_audit_id": "x", "resolution_id": ""}):
            status, _, body = self.request(
                "POST", "/api/resolutions", bad, expect_error=True)
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"], "INVALID_PAYLOAD")

    def test_unknown_resolution_fetch_is_404(self):
        status, _, body = self.request(
            "GET", "/api/resolutions/nope", expect_error=True)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "RESOLUTION_NOT_FOUND")


if __name__ == "__main__":
    unittest.main(verbosity=2)
