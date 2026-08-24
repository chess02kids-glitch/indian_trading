install:
	uv pip install -e .[dev] || pip install -e .[dev]

lint:
	ruff check .

format:
	ruff format .

test:
	pytest

pre-commit:
	pre-commit run --all-files
