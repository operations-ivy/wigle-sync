from __future__ import annotations

import os
import threading
import time

from flask import Flask
from flask import jsonify
from flask import render_template
from flask import request
from opentelemetry.instrumentation.flask import FlaskInstrumentor

import radar
import stats
from observability import init_logging
from observability import init_tracing

init_logging()
init_tracing("wigle-console")

app = Flask(__name__)
# Build the first status in the background at start-up, so even the first
# visitor after a deploy gets a cached answer instead of waiting on Loki.
threading.Thread(target=stats.collect, daemon=True).start()
FlaskInstrumentor().instrument_app(app, excluded_urls="health")

# The radar (radar.brick.nozdormu.cloud): each finished upload's KML is fetched
# once, so after the first load a refresh is one cheap transactions call.
RADAR_REFRESH_SECONDS = 1800
_radar = radar.Radar(
    (os.environ.get("WIGLE_API_NAME", ""), os.environ.get("WIGLE_API_TOKEN", "")),
    radar.parse_center(os.environ.get("RADAR_CENTER", "")),
)


def _refresh_radar() -> None:
    while True:
        _radar.refresh_safely()
        time.sleep(RADAR_REFRESH_SECONDS)


threading.Thread(target=_refresh_radar, daemon=True).start()


@app.route("/")
def console():
    if request.host.startswith("radar."):
        return render_template("radar.html")
    return render_template("console.html")


@app.route("/radar")
def radar_page():
    return render_template("radar.html")


@app.route("/api/radar")
def api_radar():
    return jsonify(_radar.snapshot())


@app.route("/api/status")
def api_status():
    return jsonify(stats.collect())


@app.route("/health")
def health_check():
    return "OK"


# The readiness probe: not ready until the radar's first load has finished, so
# a new pod doesn't take traffic with an empty radar. A failed load counts as
# finished, so a WiGLE outage doesn't take the whole console down.
@app.route("/ready")
def ready_check():
    if _radar.first_load_done.is_set():
        return "OK"
    return "loading the radar", 503


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0")
