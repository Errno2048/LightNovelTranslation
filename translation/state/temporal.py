"""Small, conflict-preserving partial-order reasoning for story anchors."""
from __future__ import annotations

from collections import defaultdict, deque
from .schemas import P6Error


class TemporalIndex:
    def __init__(self):
        self.parent: dict[str, str] = {}
        self.edges: list[dict] = []
        self.conflicts: list[dict] = []

    def _find(self, x):
        self.parent.setdefault(x, x)
        if self.parent[x] != x:
            self.parent[x] = self._find(self.parent[x])
        return self.parent[x]

    def add(self, left: str, relation: str, right: str, *, branch="main", evidence=None):
        if not left or not right or not branch:
            raise P6Error("invalid_record", "temporal anchors and branch required")
        if relation == "same":
            a, b = self._find(branch + ":" + left), self._find(branch + ":" + right)
            if a != b:
                # Stable representative keeps union deterministic.
                root, child = sorted((a, b))
                self.parent[child] = root
            if any(self._reachable(e["right"], e["left"], e["branch"])
                   for e in self.edges if e["relation"] == "before"):
                self._add_cycle_conflict(branch)
            return {"status": "same", "left": left, "right": right}
        if relation == "after":
            left, right, relation = right, left, "before"
        if relation not in {"before", "during", "overlaps"}:
            raise P6Error("unsupported_relation", "only same/after/before/during/overlaps are supported")
        edge = {"left": left, "relation": relation, "right": right, "branch": branch, "evidence": evidence}
        self.edges.append(edge)
        if relation == "before" and self._reachable(right, left, branch):
            self._add_cycle_conflict(branch)
            conflict = next(c for c in reversed(self.conflicts)
                            if c["branch"] == branch and c["kind"] == "strict_order_cycle")
            return {"status": "conflicting", **conflict}
        return {"status": "recorded", **edge}

    def _add_cycle_conflict(self, branch):
        branch_edges = [e for e in self.edges if e["branch"] == branch and e["relation"] == "before"]
        conflict = {"kind": "strict_order_cycle", "branch": branch, "edges": branch_edges,
                    "anchors": sorted({a for e in branch_edges for a in (e["left"], e["right"])})}
        if conflict not in self.conflicts:
            self.conflicts.append(conflict)

    def _reachable(self, start, goal, branch):
        graph = defaultdict(list)
        for e in self.edges:
            if e["branch"] == branch and e["relation"] == "before":
                graph[self._find(branch + ":" + e["left"])].append(self._find(branch + ":" + e["right"]))
        start, goal = self._find(branch + ":" + start), self._find(branch + ":" + goal)
        todo, seen = [start], set()
        while todo:
            node = todo.pop()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                todo.extend(graph[node])
        return False

    def compare(self, left: str, right: str, *, branch="main") -> str:
        affected = set()
        for conflict in self.conflicts:
            if conflict["branch"] == branch:
                affected.update(conflict.get("anchors", []))
        if left in affected or right in affected:
            return "conflicting"
        if self._find(branch + ":" + left) == self._find(branch + ":" + right):
            return "same"
        lr = self._reachable(left, right, branch)
        rl = self._reachable(right, left, branch)
        if lr and not rl:
            return "before"
        if rl and not lr:
            return "after"
        if lr and rl:
            return "conflicting"
        direct = [e["relation"] for e in self.edges if e["branch"] == branch and
                  {e["left"], e["right"]} == {left, right} and e["relation"] in {"during", "overlaps"}]
        return direct[0] if len(set(direct)) == 1 else "uncertain"

    def explain(self):
        return {"edges": list(self.edges), "conflicts": list(self.conflicts),
                "rules": "same union; after normalized to before; strict-before transitive only within branch; during/overlaps direct only"}
