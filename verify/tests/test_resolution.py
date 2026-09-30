"""Tests for the exact minimum feedback-vertex-set stabilisation.

The required verdict is exact: minimise the number of revoked transactions
first, then break ties by the lexicographically smallest ascending
transaction-id set.  Deleting only the displayed cycle or any degree-based
greedy choice is explicitly forbidden.
"""

import itertools
import random
import unittest

import _bootstrap  # noqa: F401  (sys.path setup)
from mvscc import (
    PayloadError,
    build_analysis,
    build_resolution,
    minimum_feedback_vertex_set,
)


def analysis(payload):
    return build_analysis(payload)


def skew_pair(prefix, keys, start=1):
    """A write-skew 2-cycle on two independent keys.

    T reads keys[0] (initial) then writes keys[1]; U reads keys[1] then
    writes keys[0] -> rw edges both ways.
    """
    t_id, u_id = prefix + "a", prefix + "b"
    k1, k2 = keys
    initial = {k1: 0, k2: 0}
    txns = [
        {"id": t_id, "start": start, "commit": start + 4, "steps": [
            {"op": "read", "key": k1, "observed": "initial"},
            {"op": "write", "key": k2, "value": 1}]},
        {"id": u_id, "start": start + 1, "commit": start + 5, "steps": [
            {"op": "read", "key": k2, "observed": "initial"},
            {"op": "write", "key": k1, "value": 1}]},
    ]
    return initial, txns


def adjacency_from(verdict):
    adj = {t: set() for t in verdict["transactions"]}
    for edge in verdict["edges"]:
        adj[edge["from"]].add(edge["to"])
    return adj


def brute_force_fvs(adj):
    """Independent oracle: enumerate vertex subsets, minimum size then the
    lexicographically smallest ascending id tuple."""
    names = sorted(adj)
    out = {u: set(vs) for u, vs in adj.items()}

    def cyclic_after(removed):
        colour = {n: 0 for n in names}

        def visit(u):
            colour[u] = 1
            for v in out[u]:
                if v in removed:
                    continue
                if colour[v] == 1 or (colour[v] == 0 and visit(v)):
                    return True
            colour[u] = 2
            return False

        return any(colour[n] == 0 and n not in removed and visit(n) for n in names)

    best = None
    for size in range(len(names) + 1):
        for combo in itertools.combinations(names, size):
            if not cyclic_after(set(combo)):
                candidate = tuple(combo)
                if best is None or candidate < best:
                    best = candidate
        if best is not None:
            return list(best)
    raise AssertionError("unreachable")


class MinimumFeedbackVertexSetTests(unittest.TestCase):
    def test_acyclic_graph_needs_no_removals(self):
        adj = {"A": {"B"}, "B": {"C"}, "C": set(), "D": {"A"}}
        self.assertEqual(minimum_feedback_vertex_set(adj), [])

    def test_empty_graph(self):
        self.assertEqual(minimum_feedback_vertex_set({}), [])

    def test_self_loop_forces_its_vertex(self):
        adj = {"A": {"A", "B"}, "B": set()}
        self.assertEqual(minimum_feedback_vertex_set(adj), ["A"])

    def test_simple_two_cycle_picks_smaller_id(self):
        adj = {"T1": {"T2"}, "T2": {"T1"}}
        self.assertEqual(minimum_feedback_vertex_set(adj), ["T1"])

    def test_two_cycles_sharing_a_node_need_only_the_shared_vertex(self):
        # T1 <-> T2 on (x, y) and T1 <-> T3 on (p, q), sharing T1.
        # A naive "delete the displayed cycle" or a per-cycle deletion would
        # revoke two transactions; the exact optimum revokes only T1.
        payload = {
            "audit_id": "shared-node",
            "initial": {"x": 100, "y": 100, "p": 100, "q": 100},
            "transactions": [
                {"id": "T1", "start": 1, "commit": 5, "steps": [
                    {"op": "read", "key": "x", "observed": "initial"},
                    {"op": "read", "key": "q", "observed": "initial"},
                    {"op": "write", "key": "y", "value": -100},
                    {"op": "write", "key": "p", "value": -100}]},
                {"id": "T2", "start": 2, "commit": 6, "steps": [
                    {"op": "read", "key": "y", "observed": "initial"},
                    {"op": "write", "key": "x", "value": 1}]},
                {"id": "T3", "start": 3, "commit": 7, "steps": [
                    {"op": "read", "key": "p", "observed": "initial"},
                    {"op": "write", "key": "q", "value": 1}]},
            ],
        }
        verdict = analysis(payload)
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")
        adj = adjacency_from(verdict)
        self.assertEqual(set(adj["T1"]), {"T2", "T3"})
        self.assertEqual(set(adj["T2"]), {"T1"})
        self.assertEqual(set(adj["T3"]), {"T1"})
        self.assertEqual(minimum_feedback_vertex_set(adj), ["T1"])

        plan = build_resolution(payload, verdict)
        self.assertEqual(plan["revoked_transactions"], ["T1"])
        self.assertEqual(plan["serial_order"], ["T2", "T3"])
        self.assertEqual(plan["residual_vertices"], ["T2", "T3"])
        self.assertEqual(plan["residual_edges"], [])
        # T2 then T3 replay against the initial snapshot:
        # T2 reads y=100 and writes x=1; T3 reads p=100 and writes q=1.
        self.assertEqual(
            plan["recomputation"]["final_state"],
            {"x": 1, "y": 100, "p": 100, "q": 1},
        )
        self.assertEqual(
            plan["recomputation"]["reads_in_order"]["T2"][0]["value"], 100
        )
        self.assertEqual(
            plan["recomputation"]["reads_in_order"]["T3"][0]["value"], 100
        )

    def test_tie_break_is_lexicographically_smallest_id_set(self):
        # Two disjoint symmetric 2-cycles: {T1,T2} and {T3,T4}.  Four
        # minimum sets exist ({T1,T3},{T1,T4},{T2,T3},{T2,T4}); the verdict
        # must be the lexicographically smallest ascending sequence.
        initial_1, pair_12 = skew_pair(("p1_"), ("k1", "k2"))
        initial_2, pair_34 = skew_pair(("p2_"), ("k3", "k4"))
        t1, t2 = pair_12
        t3, t4 = pair_34
        for t, new_id in ((t1, "T1"), (t2, "T2"), (t3, "T3"), (t4, "T4")):
            t["id"] = new_id
        payload = {
            "audit_id": "tie",
            "initial": {k: 0 for k in ("k1", "k2", "k3", "k4")},
            "transactions": [t1, t2, t3, t4],
        }
        verdict = analysis(payload)
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")
        fvs = minimum_feedback_vertex_set(adjacency_from(verdict))
        self.assertEqual(fvs, ["T1", "T3"])
        plan = build_resolution(payload, verdict)
        self.assertEqual(plan["revoked_transactions"], ["T1", "T3"])
        # the residual graph is acyclic and its order is id-stable
        self.assertEqual(plan["serial_order"], ["T2", "T4"])

    def test_must_break_every_cycle_not_only_the_displayed_one(self):
        # Two disjoint cycles: the shortest-cycle verdict displays only one,
        # yet the minimum FVS must cover both (two transactions).
        _, pair_12 = skew_pair(("p1_"), ("k1", "k2"))
        _, pair_34 = skew_pair(("p2_"), ("k3", "k4"))
        payload = {
            "audit_id": "two-cycles",
            "initial": {k: 0 for k in ("k1", "k2", "k3", "k4")},
            "transactions": pair_12 + pair_34,
        }
        verdict = analysis(payload)
        self.assertEqual(verdict["cycle"]["length"], 2)
        plan = build_resolution(payload, verdict)
        self.assertEqual(plan["revocation_count"], 2)
        # residual edges connect only surviving transactions
        revoked = set(plan["revoked_transactions"])
        for edge in plan["residual_edges"]:
            self.assertNotIn(edge["from"], revoked)
            self.assertNotIn(edge["to"], revoked)
        # all original edges are accounted for: either residual or incident
        # to a revoked transaction
        for edge in verdict["edges"]:
            self.assertTrue(
                edge in plan["residual_edges"]
                or edge["from"] in revoked or edge["to"] in revoked
            )

    def test_high_degree_vertex_off_the_cycles_is_a_greedy_trap(self):
        # D points at every cycle vertex (out-degree 4) but lies on no cycle.
        # A degree greedy picks D and breaks nothing; the exact optimum is one
        # vertex per 2-cycle.
        adj = {
            "D": {"A", "B", "C", "E"},
            "A": {"B"}, "B": {"A"},
            "C": {"E"}, "E": {"C"},
        }
        fvs = minimum_feedback_vertex_set(adj)
        self.assertEqual(sorted(fvs), ["A", "C"])
        self.assertNotIn("D", fvs)

    def test_triangle_plus_leaf(self):
        adj = {"A": {"B"}, "B": {"C"}, "C": {"A"}, "D": {"A"}, "E": set()}
        self.assertEqual(minimum_feedback_vertex_set(adj), ["A"])

    def test_residual_graph_is_acyclic_for_real_mvsg(self):
        payload = {
            "audit_id": "residual-check",
            "initial": {"x": 100, "y": 100},
            "transactions": [
                {"id": "T1", "start": 1, "commit": 5, "steps": [
                    {"op": "read", "key": "x", "observed": "initial"},
                    {"op": "write", "key": "y", "value": -100}]},
                {"id": "T2", "start": 2, "commit": 6, "steps": [
                    {"op": "read", "key": "y", "observed": "initial"},
                    {"op": "write", "key": "x", "value": -100}]},
            ],
        }
        verdict = analysis(payload)
        plan = build_resolution(payload, verdict)
        # serial order must respect every residual edge
        pos = {t: i for i, t in enumerate(plan["serial_order"])}
        for edge in plan["residual_edges"]:
            self.assertLess(pos[edge["from"]], pos[edge["to"]])

    def test_brute_force_cross_check_on_random_graphs(self):
        rng = random.Random(57321)
        for _ in range(400):
            n = rng.randint(1, 9)
            names = [f"T{i}" for i in range(n)]
            adj = {t: set() for t in names}
            for u in names:
                for v in names:
                    if rng.random() < 0.25:
                        adj[u].add(v)
            self.assertEqual(
                minimum_feedback_vertex_set(adj),
                brute_force_fvs(adj),
                msg=repr(adj),
            )


class BuildResolutionGuardTests(unittest.TestCase):
    def test_acyclic_source_is_rejected(self):
        payload = {
            "audit_id": "serial-source",
            "initial": {"x": 1},
            "transactions": [
                {"id": "T1", "start": 1, "commit": 3, "steps": [
                    {"op": "write", "key": "x", "value": 2}]},
            ],
        }
        verdict = analysis(payload)
        self.assertEqual(verdict["status"], "SERIALIZABLE")
        with self.assertRaises(PayloadError):
            build_resolution(payload, verdict)

    def test_source_audit_is_not_mutated(self):
        payload = {
            "audit_id": "immutable-source",
            "initial": {"x": 100, "y": 100},
            "transactions": [
                {"id": "T1", "start": 1, "commit": 5, "steps": [
                    {"op": "read", "key": "x", "observed": "initial"},
                    {"op": "write", "key": "y", "value": -100}]},
                {"id": "T2", "start": 2, "commit": 6, "steps": [
                    {"op": "read", "key": "y", "observed": "initial"},
                    {"op": "write", "key": "x", "value": -100}]},
            ],
        }
        verdict = analysis(payload)
        before_edges = [dict(e) for e in verdict["edges"]]
        build_resolution(payload, verdict)
        self.assertEqual(verdict["status"], "NOT_SERIALIZABLE")
        self.assertEqual(verdict["edges"], before_edges)
        self.assertEqual(verdict["cycle"]["vertices"], ["T1", "T2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
