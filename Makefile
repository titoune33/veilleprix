.PHONY: install test lint check digest report demo clean

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[extract,dev]"

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check .

fmt:
	.venv/bin/ruff check --fix .

check:
	.venv/bin/python veilleprix.py check

digest:
	.venv/bin/python veilleprix.py digest

report:
	.venv/bin/python veilleprix.py report 1

demo:
	.venv/bin/python veilleprix.py test "https://www.joybuy.de/dp/beelink-mini-pc-eq7-16-gb/100007480071829"

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
