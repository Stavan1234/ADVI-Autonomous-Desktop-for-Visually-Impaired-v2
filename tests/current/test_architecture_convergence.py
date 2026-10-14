from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_primary_import_does_not_load_retired_or_competing_brains():
    code = """
import sys
import advi.app
names = ['advi.brain', 'advi.providers.router', 'advi.core.action_plan', 'advi.core.planner', 'advi.core.task_manager']
print({name: any(m == name or m.startswith(name + '.') for m in sys.modules) for name in names})
"""
    env = os.environ.copy()
    src = str(Path(__file__).resolve().parents[2] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.stdout.strip() == (
        "{'advi.brain': False, 'advi.providers.router': False, "
        "'advi.core.action_plan': False, 'advi.core.planner': False, "
        "'advi.core.task_manager': False}"
    )


def test_default_registry_is_lazy():
    code = """
import advi.capabilities.registry as registry
print(registry._default_registry is None)
registry.get_default_registry()
print(registry._default_registry is not None)
"""
    env = os.environ.copy()
    src = str(Path(__file__).resolve().parents[2] / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.stdout.strip().splitlines() == ["True", "True"]


def test_david_owns_the_legacy_action_plan_contract():
    from advi.fallback.action_plan_schema import Action, ActionPlan
    from advi.fallback.legacy_action_plan import Action as CanonicalAction
    from advi.fallback.legacy_action_plan import ActionPlan as CanonicalActionPlan

    assert Action is CanonicalAction
    assert ActionPlan is CanonicalActionPlan


def test_retired_source_directories_are_absent_from_product_runtime():
    root = Path(__file__).resolve().parents[2]
    assert not (root / "src/advi/brain").exists()
    assert not (root / "src/advi/providers/router.py").exists()
    assert not (root / "src/advi/core/planner.py").exists()
    assert not (root / "src/advi/core/task_manager.py").exists()
