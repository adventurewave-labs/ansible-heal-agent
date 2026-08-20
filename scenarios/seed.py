"""Seed / reset the demo baseline.

The "broken" baseline is the canonical initial state of the ansible/ tree
before the agent runs. ``reset_to_baseline()`` writes that state from scratch
to disk and commits it, so each demo / test run starts from a clean,
deterministic broken state — independent of what previous runs may have
committed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.git_helper import REPO_ROOT, init_if_needed

INVENTORY_BROKEN = """\
---
# Ansible inventory for the demo web+db stack.
# NOTE: this file is intentionally seeded with a STALE hostname so the agent has
# something to heal. See scenarios/seed.py for the seed.

all:
  children:
    webservers:
      hosts:
        web-01:                     # <-- stale; playbook expects web-server-01
          ansible_host: 10.0.1.21
          ansible_user: ubuntu
        web-02:
          ansible_host: 10.0.1.22
          ansible_user: ubuntu
    dbservers:
      hosts:
        db-01:
          ansible_host: 10.0.2.11
          ansible_user: postgres
"""

GROUP_VARS_BROKEN = """\
---
# Group vars for the whole fleet.
# NOTE: nginx_port is intentionally MISSING from this file — the agent must
# discover that the playbook references an undefined var and add it here.

# Common settings
ansible_python_interpreter: /usr/bin/python3
env: production
log_level: info

# (nginx_port is intentionally omitted — see scenarios/missing_var.py)
"""

WEBSERVERS_BROKEN = """\
---
# Webservers playbook
# Fails on three deliberately seeded issues:
#   1. Targets host `web-server-01` which does NOT exist in inventory.yml
#   2. Uses the removed `apt_key` module (deprecated in ansible-core 2.18+)
#   3. References undefined var `nginx_port`

- name: Configure webservers
  hosts: web-server-01
  become: true
  tasks:
    - name: Add nginx signing key (DEPRECATED MODULE)
      ansible.builtin.apt_key:
        url: https://nginx.org/keys/nginx_signing.key
        state: present

    - name: Ensure nginx is installed
      ansible.builtin.apt:
        name: nginx
        state: present

    - name: Render nginx site config
      ansible.builtin.template:
        src: nginx.conf.j2
        dest: /etc/nginx/sites-available/default
        vars:
          listen_port: "{{ nginx_port }}"
      notify: reload nginx

  handlers:
    - name: reload nginx
      ansible.builtin.service:
        name: nginx
        state: reloaded
"""

DB_PLAYBOOK = """\
---
# DB playbook — clean, should pass once webservers.yml is healed.

- name: Configure db servers
  hosts: db-01
  become: true
  tasks:
    - name: Ensure postgresql is installed
      ansible.builtin.apt:
        name: postgresql
        state: present

    - name: Ensure postgresql is running
      ansible.builtin.service:
        name: postgresql
        state: started
        enabled: true
"""

SITE_PLAYBOOK = """\
---
# Master playbook — runs the web and db roles in sequence.
# Called by pipeline/runner.py as:  mock-ansible-playbook ansible/playbooks/site.yml

- import_playbook: webservers.yml
- import_playbook: db.yml
"""


def reset_to_baseline() -> None:
    """Write the canonical broken baseline to disk and commit it."""
    init_if_needed()

    files = {
        "ansible/inventory.yml": INVENTORY_BROKEN,
        "ansible/group_vars/all.yml": GROUP_VARS_BROKEN,
        "ansible/playbooks/webservers.yml": WEBSERVERS_BROKEN,
        "ansible/playbooks/db.yml": DB_PLAYBOOK,
        "ansible/playbooks/site.yml": SITE_PLAYBOOK,
    }
    for rel, content in files.items():
        path = REPO_ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    # Clear any previous pipeline run logs so the demo starts clean.
    runs_dir = REPO_ROOT / "pipeline" / "runs"
    if runs_dir.exists():
        for f in runs_dir.glob("run-*.log"):
            f.unlink()
        for f in runs_dir.glob("run-*.json"):
            f.unlink()

    # Commit the baseline so git log shows a clean starting point.
    subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, check=False, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: reset to broken baseline",
         "--allow-empty"],
        cwd=REPO_ROOT, check=False, capture_output=True,
    )


if __name__ == "__main__":
    import click

    @click.command()
    @click.option("--reset/--no-reset", default=True)
    def main(reset):
        if reset:
            reset_to_baseline()
            click.echo("Reset to broken baseline.")
        else:
            click.echo("No-op.")

    main()
