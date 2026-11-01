from __future__ import annotations

import gzip
import os
import threading
import time
from urllib.parse import urlsplit

import brick_observe
from flask import Flask
from flask import Response
from flask import jsonify
from flask import render_template
from flask import request

import console_metrics
import radar
import stats
brick_observe.init("wigle-console")
log = brick_observe.get_logger(__name__)
brick_observe.instrument_requests(
    {"wigle.net": "wigle", urlsplit(stats.LOKI_URL).hostname: "loki", urlsplit(stats.PROMETHEUS_URL).hostname: "prometheus"}
)

app = Flask(__name__)
# Build the first status in the background at start-up, so even the first
# visitor after a deploy gets a cached answer instead of waiting on Loki.
threading.Thread(target=stats.collect, daemon=True).start()
brick_observe.flask.instrument(app)

# The drive replay (radar.brick.nozdormu.cloud) and the Almanac panel: each finished upload's KML is fetched
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
console_metrics.register_live(_radar, stats.wigle_cached)


@app.route("/")
def console():
    if request.host.startswith("radar."):
        return render_template("replay.html")
    return render_template("console.html")


@app.route("/radar")
def replay_page():
    return render_template("replay.html")


@app.route("/api/atlas")
def api_atlas():
    page = _radar.page_gz()
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        return Response(page, mimetype="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(gzip.decompress(page), mimetype="application/json", headers={"Vary": "Accept-Encoding"})


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
