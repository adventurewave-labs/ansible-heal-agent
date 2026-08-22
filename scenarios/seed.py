"""Seed / reset the demo baseline.

The "broken" baseline is the canonical initial state of the ansible/ tree
before the agent runs. ``reset_to_baseline()`` writes that state from scratch
to disk, so each demo / test run starts from a clean, deterministic broken
state — independent of what previous runs may have done.
"""

from __future__ import annotations

import subprocess

from agent.config import repo_root
from pipeline.git_helper import init_if_needed

INVENTORY_BROKEN = """\
---
# Ansible inventory for the demo web+db stack.
# NOTE: this file is intentionally seeded with a STALE hostname so the agent has
# something to heal. See scenarios/seed.py for the seed.

all:
  children:
    webservers:
      # NOTE: the first host below is stale. webservers.yml targets
      # 'web-server-01', which this inventory does not contain.
      hosts:
        web-01:
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

# (nginx_port is intentionally omitted — see scenarios/seed.py)
"""

WEBSERVERS_BROKEN = """\
---
# Webservers playbook
# Fails on three deliberately seeded issues:
#   1. Targets host `web-server-01` which does NOT exist in inventory.yml
#   2. Uses the deprecated `apt_key` module. The mock runner treats it as
#      unresolvable; real ansible-core 2.19 still resolves it, so against the
#      real binary this class is exercised with `docker` instead.
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


def reset_to_baseline(commit: bool = False) -> None:
    """Write the canonical broken baseline to the target repo's ``ansible/`` tree.

    ``commit`` is opt-in and defaults to False: resetting the fixture tree must
    never add commits to the caller's branch as a side effect. The demo passes
    ``commit=True`` so its ``git log`` starts from a clean marker.
    """
    init_if_needed()

    files = {
        "ansible/inventory.yml": INVENTORY_BROKEN,
        "ansible/group_vars/all.yml": GROUP_VARS_BROKEN,
        "ansible/playbooks/webservers.yml": WEBSERVERS_BROKEN,
        "ansible/playbooks/db.yml": DB_PLAYBOOK,
        "ansible/playbooks/site.yml": SITE_PLAYBOOK,
    }
    for rel, content in files.items():
        path = repo_root() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    # Clear any previous pipeline run logs so the demo starts clean.
    runs_dir = repo_root() / "pipeline" / "runs"
    if runs_dir.exists():
        for f in runs_dir.glob("run-*.log"):
            f.unlink()
        for f in runs_dir.glob("run-*.json"):
            f.unlink()

    # Committing is OPT-IN. Library and test callers must never add commits to
    # the caller's branch as a side effect of resetting the fixture tree.
    if commit:
        subprocess.run(["git", "add", "-A"], cwd=repo_root(),
                       check=False, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: reset to broken baseline"],
            cwd=repo_root(), check=False, capture_output=True,
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
