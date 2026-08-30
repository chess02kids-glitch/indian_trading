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

# Dashboard commands
dashboard:
	streamlit run dashboard/main_dashboard.py

dashboard-research:
	streamlit run dashboard/research_dashboard.py

dashboard-paper:
	streamlit run dashboard/paper_dashboard.py

# Diagnostic commands
diagnostics:
	python diagnostics/run_diagnostics.py

diagnostics-verbose:
	python diagnostics/run_diagnostics.py --verbose

# Sample data generation
generate-sample-data:
	python scripts/generate_sample_data.py

# Server commands
server:
	python dashboard/server.py

# Clean
clean:
	rm -rf var/diagnostics/*.json
	rm -rf var/operational_status.json
	rm -rf __pycache__
	rm -rf *.pyc

.PHONY: install lint format test pre-commit dashboard dashboard-research dashboard-paper diagnostics diagnostics-verbose generate-sample-data server clean
