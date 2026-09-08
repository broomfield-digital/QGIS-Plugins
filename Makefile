# NASA POWER QGIS plugin.
#
# Two interpreters, on purpose:
#   PY       - any modern Python. Runs tests/unit, which import ring 1 only
#              (stdlib). If these need QGIS, ring 1 has leaked.
#   QGIS_PY  - QGIS's bundled interpreter, via scripts/qgis_python.sh. Runs
#              tests/qgis and tests/live.
#
# Runner is stdlib unittest: pytest is installed on none of the interpreters on
# this machine. The suites are plain TestCases, so pytest collects them
# unchanged if it ever appears.

PY      ?= /Users/Dfillmor/miniforge3/bin/python3
QGIS_PY ?= ./scripts/qgis_python.sh
QGIS_APP ?= /Users/Dfillmor/Applications/QGIS-final-4_2_2.app

.PHONY: help link unlink test test-qgis test-live test-all guard plugins clean

help:
	@echo "make link       symlink nasa_power into the QGIS4 default profile"
	@echo "make unlink     remove that symlink"
	@echo "make test       ring-1 unit tests, no QGIS, no network        (< 1 s)"
	@echo "make guard      prove ring 1 imports nothing but the stdlib"
	@echo "make test-qgis  headless QGIS tests, no network              (~10 s)"
	@echo "make test-live  live API contract tests, opt-in              (~30 s)"
	@echo "make test-all   test + guard + test-qgis"
	@echo "make plugins    ask qgis_process whether the plugin loads"

link:
	@./scripts/install_link.sh link

unlink:
	@./scripts/install_link.sh unlink

test:
	$(PY) -m unittest discover -s tests/unit -t . -v

# Ring 1 must import the stdlib and nothing else. Run in a subprocess so the
# test runner's own imports cannot mask a leak.
guard:
	@$(PY) scripts/check_core_isolation.py

test-qgis:
	$(QGIS_PY) -m unittest discover -s tests/qgis -t . -v

test-live:
	POWER_LIVE=1 $(QGIS_PY) -m unittest discover -s tests/live -t . -v

test-all: test guard test-qgis

# `*  nasa_power` means loaded AND providing a Processing provider -- the
# cheapest non-interactive proof that discovery, classFactory and
# initProcessing all work.
plugins:
	@$(QGIS_APP)/Contents/MacOS/qgis_process plugins | grep -i nasa_power || \
		(echo "nasa_power not listed -- run 'make link' first" && exit 1)

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
