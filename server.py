import json
import os
import re
import sqlite3
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("GEOENERGY_DB_PATH", ROOT / "geoenergy.db"))
PORT = int(os.environ.get("PORT", "8000"))
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://new-sta.vercel.app",
).split(",")
VALID_STATUSES = {"optimal", "moderate", "critical"}
WAIT_MINUTES = {"optimal": 5, "moderate": 16, "critical": 38}


def connect_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database():
    connection = connect_database()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def read_seed_stations():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    stations = []

    for mode, variable in (("cng", "cngStationsData"), ("ev", "evStationsData")):
        match = re.search(
            rf"const\s+{variable}\s*=\s*\[(.*?)\];",
            html,
            flags=re.DOTALL,
        )
        if not match:
            raise RuntimeError(f"Could not find {variable} in index.html")

        for object_match in re.finditer(r"\{[^{}]*\}", match.group(1)):
            source = object_match.group(0)
            source = re.sub(
                r"(^|[{,])\s*([A-Za-z_]\w*)\s*:",
                lambda key: f'{key.group(1)}"{key.group(2)}":',
                source,
            )
            station = json.loads(source)
            station["mode"] = mode
            station["status"] = station["status"].lower()
            station.setdefault("free", None)
            station.setdefault("total", None)
            stations.append(station)

    if not stations:
        raise RuntimeError("The station catalog is empty; refusing to start.")
    return stations


def initialize_database():
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS stations (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK (mode IN ('cng', 'ev')),
                name TEXT NOT NULL,
                region TEXT NOT NULL,
                brand TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('optimal', 'moderate', 'critical')),
                wait_mins INTEGER NOT NULL CHECK (wait_mins >= 0),
                metric INTEGER NOT NULL CHECK (metric >= 0),
                free_guns INTEGER,
                total_guns INTEGER
            );

            CREATE TABLE IF NOT EXISTS station_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id TEXT NOT NULL REFERENCES stations(id),
                status TEXT NOT NULL CHECK (status IN ('optimal', 'moderate', 'critical')),
                wait_mins INTEGER NOT NULL CHECK (wait_mins >= 0),
                reported_metric INTEGER NOT NULL CHECK (reported_metric >= 0),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_station_updates_station_created
                ON station_updates(station_id, id DESC);
            """
        )
        station_count = connection.execute(
            "SELECT COUNT(*) FROM stations"
        ).fetchone()[0]
        if station_count == 0:
            stations = read_seed_stations()
            connection.executemany(
                """
                INSERT INTO stations (
                    id, mode, name, region, brand, latitude, longitude,
                    status, wait_mins, metric, free_guns, total_guns
                ) VALUES (
                    :id, :mode, :name, :region, :brand, :lat, :lon,
                    :status, :waitMins, :metric, :free, :total
                )
                """,
                stations,
            )


class GeoEnergyHandler(BaseHTTPRequestHandler):
    server_version = "GeoEnergy/1.0"

    def _get_cors_origin(self):
        """Return the request Origin if it is in ALLOWED_ORIGINS, else None."""
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            return origin
        return None

    def _add_cors_headers(self):
        """Append CORS headers when the request Origin is allowed."""
        origin = self._get_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(204)
        self._add_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"status": "ok"})
            return
        if path == "/api/stations":
            self.get_stations()
            return
        if path in ("/", "/index.html"):
            self.serve_index()
            return
        self.send_json(404, {"error": "Not found"})

    def get_stations(self):
        try:
            with database() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        s.id, s.mode, s.name, s.region, s.brand,
                        s.latitude AS lat, s.longitude AS lon,
                        COALESCE(u.status, s.status) AS status,
                        COALESCE(u.wait_mins, s.wait_mins) AS waitMins,
                        CASE
                            WHEN s.mode = 'cng' THEN COALESCE(u.reported_metric, s.metric)
                            ELSE s.metric
                        END AS metric,
                        CASE
                            WHEN s.mode = 'ev' THEN COALESCE(u.reported_metric, s.free_guns)
                            ELSE s.free_guns
                        END AS free,
                        s.total_guns AS total
                    FROM stations AS s
                    LEFT JOIN station_updates AS u
                        ON u.id = (
                            SELECT latest.id
                            FROM station_updates AS latest
                            WHERE latest.station_id = s.id
                            ORDER BY latest.id DESC
                            LIMIT 1
                        )
                    ORDER BY s.mode, s.id
                    """
                ).fetchall()
            self.send_json(200, [dict(row) for row in rows])
        except sqlite3.Error as error:
            self.log_error("Could not load stations: %s", error)
            self.send_json(500, {"error": "Unable to load stations."})

    def do_POST(self):
        if urlparse(self.path).path != "/api/updates":
            self.send_json(404, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 8192:
                self.send_json(400, {"error": "Request body must be between 1 and 8192 bytes."})
                return
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                self.send_json(400, {"error": "Request body must be a JSON object."})
                return
            station_id = payload.get("station_id")
            status = payload.get("status")
            metric = payload.get("metric")
            if not isinstance(station_id, str) or not station_id:
                self.send_json(400, {"error": "station_id is required."})
                return
            if not isinstance(status, str) or status not in VALID_STATUSES:
                self.send_json(400, {"error": "status must be optimal, moderate, or critical."})
                return
            if isinstance(metric, bool) or not isinstance(metric, int):
                self.send_json(400, {"error": "metric must be an integer."})
                return

            with database() as connection:
                station = connection.execute(
                    "SELECT mode, total_guns FROM stations WHERE id = ?",
                    (station_id,),
                ).fetchone()
                if station is None:
                    self.send_json(404, {"error": "Station not found."})
                    return

                if station["mode"] == "cng" and not 80 <= metric <= 250:
                    self.send_json(400, {"error": "CNG pressure must be between 80 and 250 bar."})
                    return
                if station["mode"] == "ev" and not 0 <= metric <= station["total_guns"]:
                    self.send_json(400, {"error": "Free charging guns must be within the station's capacity."})
                    return

                wait_mins = WAIT_MINUTES[status]
                cursor = connection.execute(
                    """
                    INSERT INTO station_updates (station_id, status, wait_mins, reported_metric)
                    VALUES (?, ?, ?, ?)
                    """,
                    (station_id, status, wait_mins, metric),
                )
                update_id = cursor.lastrowid
                created_at = connection.execute(
                    "SELECT created_at FROM station_updates WHERE id = ?",
                    (update_id,),
                ).fetchone()["created_at"]

            self.send_json(
                201,
                {
                    "id": update_id,
                    "station_id": station_id,
                    "status": status,
                    "wait_mins": wait_mins,
                    "metric": metric,
                    "created_at": created_at,
                },
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Request body must contain valid JSON."})
        except (TypeError, ValueError) as error:
            self.log_error("Invalid update request: %s", error)
            self.send_json(400, {"error": "Invalid update request."})
        except (OSError, sqlite3.Error) as error:
            self.log_error("Database or request failure: %s", error)
            self.send_json(500, {"error": "Unable to save the station update."})

    def serve_index(self):
        try:
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as error:
            self.log_error("Could not read index.html: %s", error)
            self.send_json(500, {"error": "Unable to load the application page."})


def main():
    initialize_database()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), GeoEnergyHandler)
    print(f"GeoEnergy backend running on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down GeoEnergy backend.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
