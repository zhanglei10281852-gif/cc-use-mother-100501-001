"""流域设施拓扑：河段、水库、闸站、取水口及其上下游关系。

拓扑是一张有向图：边方向与水流方向一致（上游 -> 下游）。
每个设施节点携带调度所需的静态边界，动态边界（库水位-库容曲线、
闸门开度-泄量曲线等）随情景冻结，不在拓扑内变化。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable

from .contracts import canonical_fingerprint


class FacilityType(str, Enum):
    REACH = "reach"        # 河段：仅输送，有安全流量上限
    RESERVOIR = "reservoir"  # 水库：有库容与下泄能力
    GATE = "gate"          # 闸站：可调闸门，有过流能力
    INTAKE = "intake"      # 取水口：引水给城市/灌区，不回流


# 各类设施必须提供的边界键
BOUNDARY_KEYS: dict[FacilityType, tuple[str, ...]] = {
    FacilityType.REACH: ("max_flow",),
    FacilityType.RESERVOIR: (
        "capacity",          # 总库容（万 m³）
        "dead_storage",      # 死库容（万 m³）
        "flood_storage",     # 防洪库容（万 m³，汛限以上可用）
        "max_release",       # 最大下泄流量（m³/s）
        "max_level_rate",    # 单时段水位/库容最大变幅（万 m³/时段），可为 0 表示不限
    ),
    FacilityType.GATE: (
        "max_flow",          # 最大过流（m³/s）
        "min_opening",       # 开度下限（0-100）
        "max_opening",       # 开度上限（0-100）
    ),
    FacilityType.INTAKE: (
        "max_take",          # 最大取水流量（m³/s）
        "min_guarantee",     # 旱情保供最小流量（m³/s），可为 0
    ),
}


@dataclass(frozen=True, slots=True)
class Facility:
    """拓扑中的一个设施节点。

    flow_sequence 是该设施在干流上的里程序（公里或相对桩号），
    仅用于检查上下游方向是否矛盾，不参与水量计算。
    """

    code: str
    name: str
    facility_type: FacilityType
    boundaries: dict[str, float] = field(default_factory=dict)
    flow_sequence: float = 0.0
    # 取水口专用：所服务的用水户标识（城市饮水 / 灌区 / 生态口）
    serves: str | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.code.strip():
            raise ValueError("设施编码不能为空")
        if not self.name or not self.name.strip():
            raise ValueError("设施名称不能为空")
        required = BOUNDARY_KEYS[self.facility_type]
        missing = [key for key in required if key not in self.boundaries]
        if missing:
            raise ValueError(f"设施 {self.code} 缺少边界参数: {', '.join(missing)}")
        for key in required:
            value = self.boundaries[key]
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"设施 {self.code} 的边界 {key} 必须是非负数")
        if self.facility_type is FacilityType.INTAKE and not (self.serves or "").strip():
            raise ValueError(f"取水口 {self.code} 必须声明所服务用水户(serves)")
        if self.facility_type is FacilityType.GATE:
            lo, hi = self.boundaries["min_opening"], self.boundaries["max_opening"]
            if lo > hi:
                raise ValueError(f"闸站 {self.code} 的开度下限大于上限")

    def boundary(self, key: str) -> float:
        return float(self.boundaries[key])

    def with_boundaries(self, **changes: float) -> "Facility":
        """返回边界被替换后的副本（拓扑订正时使用，历史拓扑不受影响）。"""
        merged = dict(self.boundaries)
        merged.update(changes)
        return replace(self, boundaries=merged)


@dataclass(frozen=True, slots=True)
class Topology:
    """不可变设施拓扑：节点集合 + 水流方向的有向边。"""

    basin_code: str
    facilities: dict[str, Facility]
    # (upstream_code -> [downstream_code, ...])
    downstream_of: dict[str, tuple[str, ...]]
    revision: str

    def __post_init__(self) -> None:
        if not self.revision or not self.revision.strip():
            raise ValueError("拓扑修订号不能为空")
        for code in list(self.downstream_of) + [
            dst for targets in self.downstream_of.values() for dst in targets
        ]:
            if code not in self.facilities:
                raise ValueError(f"边引用了不存在的设施: {code}")
        # 有向无环、无重边、无自环
        for upstream, targets in self.downstream_of.items():
            if len(set(targets)) != len(targets):
                raise ValueError(f"设施 {upstream} 存在重复的下游连接")
            for target in targets:
                if target == upstream:
                    raise ValueError(f"设施 {upstream} 不能与自身相连")
                up = self.facilities[upstream]
                down = self.facilities[target]
                if up.flow_sequence >= down.flow_sequence:
                    raise ValueError(
                        f"水流方向与里程矛盾: {upstream}({up.flow_sequence}) "
                        f"-> {target}({down.flow_sequence})"
                    )
        self._assert_acyclic()
        # 取水口只能是汇点：水引出流域后不回流
        for facility in self.facilities.values():
            if facility.facility_type is FacilityType.INTAKE and self.downstream_of.get(facility.code):
                raise ValueError(f"取水口 {facility.code} 不能再有下游设施")

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        done: set[str] = set()

        def walk(node: str) -> None:
            if node in done:
                return
            if node in visiting:
                raise ValueError(f"拓扑中存在环，涉及设施: {node}")
            visiting.add(node)
            for nxt in self.downstream_of.get(node, ()):  # pragma: no branch - 防御
                walk(nxt)
            visiting.discard(node)
            done.add(node)

        for code in self.facilities:
            walk(code)

    @property
    def fingerprint(self) -> str:
        payload = {
            "basin_code": self.basin_code,
            "revision": self.revision,
            "facilities": [
                {
                    "code": f.code,
                    "type": f.facility_type.value,
                    "boundaries": f.boundaries,
                    "serves": f.serves,
                    "sequence": f.flow_sequence,
                }
                for f in sorted(self.facilities.values(), key=lambda x: x.code)
            ],
            "edges": sorted(
                (up, down)
                for up, targets in self.downstream_of.items()
                for down in targets
            ),
        }
        return canonical_fingerprint(payload)

    # ---- 图查询 ----

    def upstream_of(self, code: str) -> tuple[str, ...]:
        return tuple(up for up, targets in self.downstream_of.items() if code in targets)

    def roots(self) -> list[Facility]:
        """干流/支流最上游（无来水连接的非取水口设施）。"""
        return [
            f
            for code, f in self.facilities.items()
            if not self.upstream_of(code) and f.facility_type is not FacilityType.INTAKE
        ]

    def topological_order(self) -> list[Facility]:
        """按水流方向的拓扑序（Kahn 算法，同层按编码排序保证确定性）。"""
        indegree = {code: len(self.upstream_of(code)) for code in self.facilities}
        ready = sorted(code for code, deg in indegree.items() if deg == 0)
        ordered: list[str] = []
        while ready:
            code = ready.pop(0)
            ordered.append(code)
            for nxt in self.downstream_of.get(code, ()):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        if len(ordered) != len(self.facilities):
            raise ValueError("拓扑中存在环")  # 构造时已拦截，此处为防御
        return [self.facilities[code] for code in ordered]

    def affected_range(self, code: str) -> tuple[frozenset[str], frozenset[str]]:
        """返回某设施调度所影响的范围：(上游集合, 下游集合)，均不含自身。

        上游：沿来水方向递归；下游：沿水流方向递归（含被供水的取水口）。
        """
        upstream: set[str] = set()
        stack = list(self.upstream_of(code))
        while stack:
            node = stack.pop()
            if node in upstream:
                continue
            upstream.add(node)
            stack.extend(self.upstream_of(node))

        downstream: set[str] = set()
        stack = list(self.downstream_of.get(code, ()))
        while stack:
            node = stack.pop()
            if node in downstream:
                continue
            downstream.add(node)
            stack.extend(self.downstream_of.get(node, ()))
        return frozenset(upstream), frozenset(downstream)


def build_topology(
    basin_code: str,
    revision: str,
    facilities: Iterable[Facility],
    edges: Iterable[tuple[str, str]],
) -> Topology:
    """由设施列表与 (上游, 下游) 边列表构造拓扑，并做基本结构校验。"""
    facility_list = list(facilities)
    index = {f.code: f for f in facility_list}
    if len(index) != len(facility_list):
        raise ValueError("设施编码重复")
    links: dict[str, list[str]] = {code: [] for code in index}
    for upstream, downstream in edges:
        if upstream not in index:
            raise ValueError(f"边的上游设施不存在: {upstream}")
        if downstream not in index:
            raise ValueError(f"边的下游设施不存在: {downstream}")
        links[upstream].append(downstream)
    return Topology(
        basin_code=basin_code,
        facilities=index,
        downstream_of={code: tuple(targets) for code, targets in links.items()},
        revision=revision,
    )
