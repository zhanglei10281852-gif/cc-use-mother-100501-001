"""流域联合调度与指令追溯命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from basin_dispatch import DispatchScenario


def main() -> None:
    item = DispatchScenario(scenario_code='scenario-code-001', basin_code='basin-code-001', forecast_revision='forecast-revision-001', state='state-001')
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
