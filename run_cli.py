"""命令行冒烟入口：输出基础值对象与稳定摘要。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from basin_dispatch import DispatchScenario  # noqa: E402


def main() -> None:
    item = DispatchScenario(
        scenario_code="scenario-code-001",
        basin_code="basin-code-001",
        forecast_revision="forecast-revision-001",
        state="frozen",
    )
    print(json.dumps(
        {"item": asdict(item), "fingerprint": item.fingerprint()},
        ensure_ascii=False, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
