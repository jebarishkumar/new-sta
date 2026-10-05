# GeoEnergy Tamil Nadu

The app serves its existing CNG and EV station finder from a small Python backend. The backend stores station details and community reports in a local SQLite database, so reports are still available after restarting the server.

## Requirements

- Python 3.9 or newer
- No third-party Python packages

On Windows, Python can be launched with the Python launcher (`py -3`).

## Start the app

Open PowerShell in this folder and run:

```powershell
py -3 server.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. Keep the terminal running while using the app. Stop the server with `Ctrl+C`.

The first start creates `geoenergy.db` in this folder and seeds its `stations` table from the station catalog in `index.html`. The SQLite database file is the persistent store; do not open the HTML file directly because browser `file://` pages cannot call the backend API.

## Database tables

| Table | Contents |
| --- | --- |
| `stations` | CNG and EV hub catalog, location, initial queue state and charging/refuelling metrics |
| `station_updates` | Append-only community reports, including the reported status, wait time, metric and timestamp |

The app displays the latest report for each station. Older reports remain in `station_updates`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Backend health check |
| `GET` | `/api/stations` | List stations with their latest reported state |
| `POST` | `/api/updates` | Save a station report |

Example update request:

```json
{
  "station_id": "tn-01",
  "status": "optimal",
  "metric": 225
}
```

For CNG stations, `metric` is dispenser pressure in bar (80–250). For EV stations, it is the number of free charging guns (0 up to that station's capacity). `status` must be `optimal`, `moderate`, or `critical`; the backend derives the estimated wait time from the selected status.

## Configuration

The default address is `127.0.0.1:8000`. Set `PORT` to use another port, or `GEOENERGY_DB_PATH` to store the SQLite database elsewhere.

PowerShell example:

```powershell
$env:PORT = "8080"
$env:GEOENERGY_DB_PATH = "C:\data\geoenergy.db"
py -3 server.py
```

The browser refreshes station data every 30 seconds. No Supabase account, API key, or additional setup is required.
