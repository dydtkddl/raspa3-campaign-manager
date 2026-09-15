from __future__ import annotations

from copy import deepcopy
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "single_h2_pilot": {
        "gas_priority": ["H2"],
        "conditions": [
            {"gas": "H2", "temperatures_K": [293.0], "pressures_bar": [5.0], "seeds": [42], "replicates": 1}
        ],
    },
    "ispt_multigas_293K": {
        "gas_priority": ["H2", "CO2", "N2", "O2", "He"],
        "conditions": [
            {"gas": "H2", "temperatures_K": [293.0], "pressures_bar": [1.0, 15.0], "seeds": [42], "replicates": 1},
            {"gas": "CO2", "temperatures_K": [293.0], "pressures_bar": [0.01, 1.0, 15.0], "seeds": [42], "replicates": 1},
            {"gas": "N2", "temperatures_K": [293.0], "pressures_bar": [1.0, 15.0], "seeds": [42], "replicates": 1},
            {"gas": "O2", "temperatures_K": [293.0], "pressures_bar": [1.0, 15.0], "seeds": [42], "replicates": 1},
            {"gas": "He", "temperatures_K": [293.0], "pressures_bar": [1.0, 15.0], "seeds": [42], "replicates": 1},
        ],
    },
    "multigas_qualification_short": {
        "gas_priority": ["H2", "CO2", "N2", "O2", "He"],
        "conditions": [
            {"gas": gas, "temperatures_K": [293.0], "pressures_bar": pressures, "seeds": [16993], "replicates": 1}
            for gas, pressures in {
                "H2": [1.0, 15.0],
                "CO2": [0.01, 1.0, 15.0],
                "N2": [1.0, 15.0],
                "O2": [1.0, 15.0],
                "He": [1.0, 15.0],
            }.items()
        ],
    },
}


def get_preset(name: str) -> dict[str, Any]:
    if name not in PRESETS:
        raise KeyError(f"Unknown preset {name!r}; choices: {', '.join(sorted(PRESETS))}")
    return deepcopy(PRESETS[name])
