"""Plain helpers shared across test modules.

Not fixtures — ``conftest.py`` is pytest's own entry point, and importing it a
second time as ``tests.conftest`` would build a duplicate module object.
"""

from __future__ import annotations

from functools import cache


@cache
def replacement_resolves(module: str = "docker") -> bool:
    """Does this environment resolve the seeded module's replacement?

    The module-class fixtures swap `ansible.builtin.docker` for
    `community.docker.docker_container`, and that name only resolves when the
    community.docker collection is installed. The stock test legs do not
    install it; the ``pytest (community.docker installed)`` CI leg and a
    developer machine that has it must not see the suite flip red over that
    difference. Tests that would otherwise hardcode either outcome branch on
    this instead, consulting the same authority (ansible-doc) the agent
    itself uses. Cached: the answer cannot change during a pytest run, and
    several tests ask.
    """
    from agent import diagnoser

    replacement = diagnoser.MODULE_REPLACEMENTS.get(module, {}).get("module")
    return bool(replacement and diagnoser.module_resolves(replacement) is True)
