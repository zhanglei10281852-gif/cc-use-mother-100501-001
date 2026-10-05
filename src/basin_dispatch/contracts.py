"""流域联合调度与指令追溯的基础领域契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Any, Iterable


def canonical_json(payload: Any) -> str:
    """把任意可 JSON 化的载荷序列化成稳定字符串。

    - 键排序、紧凑分隔，保证字段顺序不影响摘要；
    - 日期时间统一转 ISO 字符串，避免平台差异；
    - 禁止隐式 str() 兜底，遇到无法表达的类型直接报错。
    """

    def _default(value: Any) -> str:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        raise TypeError(f"不支持进入指纹的类型: {type(value)!r}")

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    )


def canonical_fingerprint(payload: Any) -> str:
    """对稳定载荷计算 sha256 摘要，供幂等、冻结校验与审计使用。"""
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DispatchScenario:
    """保存最小且可校验的业务对象。"""

    scenario_code: str
    basin_code: str
    forecast_revision: str
    state: str

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{key} 不能为空")
            if isinstance(value, int) and value < 1:
                raise ValueError(f"{key} 必须大于零")

    def evolve(self, **changes: object) -> "DispatchScenario":
        """返回新版本，避免就地改写历史对象。"""
        return replace(self, **changes)

    def fingerprint(self) -> str:
        """生成稳定摘要，供幂等和审计使用。"""
        return canonical_fingerprint(asdict(self))


def unique_by_identity(items: Iterable[DispatchScenario]) -> list[DispatchScenario]:
    """按业务标识去重，并拒绝同标识不同内容。"""
    found: dict[str, DispatchScenario] = {}
    for item in items:
        key = str(getattr(item, "scenario_code"))
        previous = found.get(key)
        if previous is not None and previous.fingerprint() != item.fingerprint():
            raise ValueError(f"业务标识 {key} 对应的内容发生冲突")
        found[key] = item
    return [found[key] for key in sorted(found)]
