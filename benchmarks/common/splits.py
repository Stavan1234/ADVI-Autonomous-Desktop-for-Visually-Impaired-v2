"""Fixed dev/test splits, chosen by a seeded shuffle before any run.

dev tasks are for finding and fixing weaknesses; test tasks are only scored.
Writing the split to a committed file before the first run is what keeps the
reported score honest: nobody can pick the easy tasks afterwards.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


def stratified_split(
    items: list[dict[str, Any]],
    *,
    group_of: Callable[[dict[str, Any]], str],
    id_of: Callable[[dict[str, Any]], str],
    seed: int,
    dev_per_group: int | None = None,
    test_per_group: int | None = None,
    dev_fraction: float | None = None,
) -> dict[str, list[str]]:
    """Per group: a fixed count (WebVoyager: 2 dev + 3 test per site), or a
    fraction for dev with the rest as test (small suites). A group always keeps
    at least one test task."""
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        groups[group_of(item)].append(id_of(item))
    rng = random.Random(seed)
    dev: list[str] = []
    test: list[str] = []
    for name in sorted(groups):
        ids = sorted(groups[name])
        rng.shuffle(ids)
        if dev_fraction is not None:
            n_dev = min(round(len(ids) * dev_fraction), len(ids) - 1)
            n_test = len(ids) - n_dev
        else:
            n_dev, n_test = int(dev_per_group or 0), int(test_per_group or 0)
        dev += ids[:n_dev]
        test += ids[n_dev:n_dev + n_test]
    return {"seed": seed, "dev": dev, "test": test}


def load_or_create(path: Path, make: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """The split on disk wins; it is only created once."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    split = make()
    path.write_text(json.dumps(split, indent=2) + "\n", encoding="utf-8")
    return split
