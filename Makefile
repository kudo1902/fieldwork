.PHONY: install api serve eval check test

install:
	python -m venv .venv && . .venv/bin/activate && pip install -e .

# Development server: auto-reload, single process.
api:
	flask --app fieldwork.api run --debug --port 8080

# Production server: gevent workers so SSE and slow inference calls do not
# each pin an OS thread.
serve:
	gunicorn -k gevent -w 4 -b 0.0.0.0:8080 fieldwork.api:app

eval:
	python eval/run_eval.py

check:
	python eval/run_eval.py --check

test:
	python tests/test_score.py
