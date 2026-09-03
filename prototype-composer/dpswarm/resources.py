from __future__ import annotations

from typing import Dict

from .models import ModelLevel, ModelRoute, RootExecutionSpec


DEFAULT_POINT_WEIGHTS: Dict[ModelLevel, int] = {
    ModelLevel.S: 30,
    ModelLevel.A: 20,
    ModelLevel.B: 12,
    ModelLevel.C: 8,
    ModelLevel.D: 5,
}


def point_weight_for(route: ModelRoute, policy_version: str = "v1") -> int:
    _ = policy_version
    return DEFAULT_POINT_WEIGHTS.get(route.level, 10)


class ResourcePool:
    """Root-level worker slots and weighted node points."""

    def __init__(self, spec: RootExecutionSpec) -> None:
        self.spec = spec
        self.open_worker_slots = 0
        self.active_node_points = 0

    def can_acquire_worker_slot(self) -> bool:
        return self.open_worker_slots < self.spec.max_open_work_items

    def acquire_worker_slot(self) -> None:
        if not self.can_acquire_worker_slot():
            raise ValueError("worker slot capacity exceeded")
        self.open_worker_slots += 1

    def release_worker_slot(self) -> None:
        if self.open_worker_slots <= 0:
            raise ValueError("no worker slot to release")
        self.open_worker_slots -= 1

    def can_acquire_points(self, weight: int, team_caps: Dict[str, int], team_usage: Dict[str, int]) -> bool:
        if self.active_node_points + weight > self.spec.max_active_node_points:
            return False
        for team_id, cap in team_caps.items():
            used = team_usage.get(team_id, 0)
            if used + weight > cap:
                return False
        return True

    def acquire_points(self, weight: int) -> None:
        if self.active_node_points + weight > self.spec.max_active_node_points:
            raise ValueError("node point capacity exceeded")
        self.active_node_points += weight

    def release_points(self, weight: int) -> None:
        self.active_node_points -= weight
        if self.active_node_points < 0:
            raise ValueError("negative node points")

    def team_local_cap(self, parent_cap: int) -> int:
        return max(1, int(parent_cap * self.spec.sub_team_point_ratio))
