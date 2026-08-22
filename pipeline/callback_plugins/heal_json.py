"""Ansible callback plugin — emit structured failures the agent can consume.

Regex over human-readable Ansible output is the wrong parser: the strings are
not an API, they differ between ansible-core versions, and several of the
failure classes this agent cares about are not reliably expressible as a line
match. In particular a play whose ``hosts:`` pattern matches nothing does not
produce a failure line at all — Ansible logs a WARNING and exits **0**.

This callback hooks the events themselves and writes one JSON document per run
to ``ANSIBLE_HEAL_SIDECAR``. ``pipeline.runner.run_real`` reads it back.

Enabled by run_real via:

    ANSIBLE_CALLBACK_PLUGINS=<this dir>
    ANSIBLE_CALLBACKS_ENABLED=heal_json
    ANSIBLE_HEAL_SIDECAR=<path>.json
"""

from __future__ import annotations

import json
import os
import re

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: heal_json
    type: aggregate
    short_description: writes structured failure records for ansible-heal-agent
    description:
      - Records failures, unreachable hosts and unmatched host patterns as JSON.
    requirements:
      - set ANSIBLE_HEAL_SIDECAR to the output path
"""

#: Real ansible-core phrasing, e.g.
#: "Error while resolving value for 'msg': 'nginx_port' is undefined"
UNDEFINED_RE = re.compile(r"'([^']+)' is undefined")


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "heal_json"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures: list[dict] = []
        self.ok_hosts: list[str] = []
        self._path = os.environ.get("ANSIBLE_HEAL_SIDECAR")
        #: Host patterns of the play currently executing. Ansible does not pass
        #: the play to v2_playbook_on_no_hosts_matched, so they have to be
        #: remembered here — without them the "no hosts matched" record cannot
        #: name what failed to match, and the diagnoser has nothing to act on.
        self._current_patterns: list[str] = []

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _msg(result) -> str:
        res = getattr(result, "_result", {}) or {}
        return str(res.get("msg") or res.get("exception") or "")

    @staticmethod
    def _task_name(result) -> str:
        try:
            return result._task.get_name()
        except Exception:
            return "<unknown task>"

    @staticmethod
    def _patterns_of(play) -> list[str]:
        """The play's ``hosts:`` patterns, one entry per pattern.

        A list-valued ``hosts:`` used to be joined into ``"a,b"``. The
        diagnoser then had a host name that exists nowhere, and the joined
        string never de-duplicated against the text scan's per-pattern records
        — one event, three failures, two of them undiagnosable.
        """
        try:
            hosts = play.hosts
        except Exception:
            return []
        if isinstance(hosts, (list, tuple)):
            raw = [str(h) for h in hosts]
        else:
            raw = str(hosts).split(",")
        return [p.strip() for p in raw if p and p.strip()]

    @staticmethod
    def _playbook_of(result) -> str:
        try:
            return str(result._task.get_path()).split(":")[0]
        except Exception:
            return ""

    def _record(self, failure: dict) -> None:
        self.failures.append(failure)
        self._flush()

    def _flush(self) -> None:
        """Write after every event so a crashed run still leaves usable data."""
        if not self._path:
            return
        try:
            with open(self._path, "w") as fh:
                json.dump({"failures": self.failures,
                           "ok_hosts": sorted(set(self.ok_hosts))}, fh, indent=2)
        except OSError:
            pass

    # ── events ──────────────────────────────────────────────────────

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if ignore_errors:
            return
        msg = self._msg(result)
        host = result._host.get_name()
        m = UNDEFINED_RE.search(msg)
        if m:
            self._record({
                "type": "undefined_variable",
                "host": host,
                "variable": m.group(1),
                "task": self._task_name(result),
                "playbook": self._playbook_of(result),
                "message": msg,
            })
            return
        self._record({
            "type": "task_failed",
            "host": host,
            "task": self._task_name(result),
            "playbook": self._playbook_of(result),
            "message": msg,
        })

    def v2_runner_on_unreachable(self, result):
        self._record({
            "type": "unreachable_host",
            "host": result._host.get_name(),
            "task": self._task_name(result),
            "message": self._msg(result),
        })

    def v2_playbook_on_play_start(self, play):
        self._current_patterns = self._patterns_of(play)

    def v2_playbook_on_no_hosts_matched(self):
        # Real Ansible treats this as a warning and still exits 0. For an agent
        # watching a deployment pipeline it is the most important signal there
        # is: the play silently did nothing.
        # One record per pattern, matching how the text scan reports them, so
        # the two collapse instead of accumulating.
        for pattern in self._current_patterns or [None]:
            self._record({
                "type": "no_hosts_matched",
                # Both keys carry the pattern: `pattern` is what the diagnoser
                # reads, `host` is what de-duplication keys on, and the text
                # scan of the same event fills in both. Leaving either empty
                # produced a second, undiagnosable copy of every no-hosts
                # failure.
                "host": pattern,
                "pattern": pattern,
                "message": (f"host pattern {pattern!r} matched no hosts in inventory; "
                            f"the play was skipped" if pattern else
                            "host pattern matched no hosts in inventory; "
                            "the play was skipped"),
            })

    def v2_runner_on_ok(self, result):
        self.ok_hosts.append(result._host.get_name())
        self._flush()

    def v2_playbook_on_stats(self, stats):
        self._flush()
