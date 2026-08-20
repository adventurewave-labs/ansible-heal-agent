# Makefile for ansible-heal-agent

PY      := python3
PIP     := pip3
DEMO    := demo.py

.PHONY: help demo test lint clean reset-seed install

help:
	@echo "ansible-heal-agent — autonomous Ansible healer"
	@echo ""
	@echo "  make install    Install Python deps"
	@echo "  make demo       Run the end-to-end demo (writes a transcript)"
	@echo "  make reset-seed Reset the repo to the broken baseline state"
	@echo "  make test       Run pytest suite"
	@echo "  make clean      Remove transcripts, pipeline runs, caches"

install:
	$(PIP) install -r requirements.txt

reset-seed:
	$(PY) -m scenarios.seed --reset

demo:
	$(PY) $(DEMO)

test:
	$(PY) -m pytest -v

clean:
	rm -rf transcripts/*.md transcripts/*.json pipeline/runs/*.log pipeline/runs/*.json
	rm -rf .pytest_cache __pycache__ agent/__pycache__ pipeline/__pycache__ scenarios/__pycache__ tests/__pycache__
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
