from __future__ import annotations

from flask import Flask
from flask import jsonify
from flask import render_template
from opentelemetry.instrumentation.flask import FlaskInstrumentor

import stats
from observability import init_logging
from observability import init_tracing

init_logging()
init_tracing("wigle-console")

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app, excluded_urls="health")


@app.route("/")
def console():
    return render_template("console.html")


@app.route("/api/status")
def api_status():
    return jsonify(stats.collect())


@app.route("/health")
def health_check():
    return "OK"


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0")
