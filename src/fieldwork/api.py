"""Phase 1 API: the thinnest vertical slice that proves the UX.

Deliberately synchronous. One request holds one GPU slot for the duration of
inference, which is fine for a handful of users on your own box and wrong for
anything else. Phase 2 replaces this endpoint with enqueue + poll; the
extract() core underneath does not change.

Run it:
    flask --app fieldwork.api run --debug --port 8080          # development
    gunicorn -k gevent -w 4 -b 0.0.0.0:8080 fieldwork.api:app  # production
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import schemas
from .config import settings
from .extract import extract_bytes
from .llm import client
from .preprocess import UnsupportedFile

WEB = Path(__file__).resolve().parent.parent.parent / "web"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    # Werkzeug rejects anything larger before we read a byte of it.
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_mb * 1024 * 1024

    @app.errorhandler(HTTPException)
    def _json_errors(exc: HTTPException):
        """Never hand an HTML error page to something expecting JSON."""
        return jsonify(ok=False, error=exc.description, status=exc.code), exc.code

    @app.get("/")
    def index():
        return send_from_directory(WEB, "index.html")

    @app.get("/healthz")
    def healthz():
        try:
            served = [m.id for m in client().models.list().data]
        except Exception as exc:  # noqa: BLE001 - report, don't raise
            return jsonify(
                ok=False,
                llm_base_url=settings.llm_base_url,
                error=f"{type(exc).__name__}: {exc}",
            ), 503
        return jsonify(
            ok=True,
            llm_base_url=settings.llm_base_url,
            configured_model=settings.llm_model,
            served_models=served,
            model_available=settings.llm_model in served,
        )

    @app.get("/v1/schemas")
    def list_schemas():
        return jsonify([
            {
                "name": dt.name,
                "hint": dt.hint,
                "fields": list(dt.model.model_fields.keys()),
            }
            for dt in schemas.REGISTRY.values()
        ])

    @app.get("/v1/schemas/<name>")
    def get_schema(name: str):
        try:
            dt = schemas.get(name)
        except KeyError as exc:
            return jsonify(ok=False, error=str(exc)), 404
        return jsonify(
            name=dt.name,
            hint=dt.hint,
            json_schema=schemas.json_schema_for(dt.model),
        )

    @app.post("/v1/extractions")
    def create_extraction():
        upload = request.files.get("file")
        if upload is None:
            return jsonify(ok=False, error="no file part in the request"), 400

        schema = request.form.get("schema", "invoice")
        if schema not in schemas.REGISTRY:
            return jsonify(
                ok=False,
                error=f"unknown schema {schema!r}; known: {sorted(schemas.REGISTRY)}",
            ), 400

        raw_max_dim = request.form.get("max_dim")
        try:
            max_dim = int(raw_max_dim) if raw_max_dim else None
        except ValueError:
            return jsonify(ok=False, error="max_dim must be an integer"), 400

        data = upload.read()
        if not data:
            return jsonify(ok=False, error="empty upload"), 400

        try:
            result = extract_bytes(data, schema, max_dim=max_dim)
        except UnsupportedFile as exc:
            return jsonify(ok=False, error=str(exc)), 415

        payload = result.to_dict()
        payload["filename"] = upload.filename
        return jsonify(payload), (200 if result.ok else 502)

    return app


app = create_app()
