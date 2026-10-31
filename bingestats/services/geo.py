"""IP geolocation from the free DB-IP Lite City database (CC BY 4.0).

The worker downloads the .mmdb monthly to /data/geo and fills `ip_geo` for
any IP seen in history. Private and LAN addresses are never looked up.
"""
from __future__ import annotations

import gzip
import ipaddress
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
FILENAME = "dbip-city-lite.mmdb"
REFRESH_DAYS = 32
LOOKUP_TTL_DAYS = 30
ATTRIBUTION = 'IP geolocation by <a href="https://db-ip.com" class="underline">DB-IP</a>'


def db_path(data_dir: Path) -> Path:
    return Path(data_dir) / "geo" / FILENAME


def needs_download(data_dir: Path, now: float | None = None) -> bool:
    p = db_path(data_dir)
    if not p.exists():
        return True
    return (now or time.time()) - p.stat().st_mtime > REFRESH_DAYS * 86400


def download(data_dir: Path, now: float | None = None, session: requests.Session | None = None) -> dict:
    """Fetch this month's file, falling back to last month's if it isn't out yet."""
    http = session or requests.Session()
    target = db_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    t = time.gmtime(now or time.time())
    months = [f"{t.tm_year}-{t.tm_mon:02d}"]
    prev_y, prev_m = (t.tm_year, t.tm_mon - 1) if t.tm_mon > 1 else (t.tm_year - 1, 12)
    months.append(f"{prev_y}-{prev_m:02d}")
    last_err = None
    for month in months:
        url = DBIP_URL.format(month=month)
        try:
            with http.get(url, stream=True, timeout=120) as r:
                if r.status_code == 404:
                    last_err = f"{month} not published yet"
                    continue
                r.raise_for_status()
                gz = target.with_suffix(".mmdb.gz.part")
                with open(gz, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            tmp = target.with_suffix(".mmdb.part")
            with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            gz.unlink(missing_ok=True)
            _check(tmp)
            os.replace(tmp, target)
            return {"month": month, "bytes": target.stat().st_size}
        except (requests.RequestException, OSError, ValueError) as e:
            last_err = f"{month}: {e}"
            log.warning("geo download failed: %s", last_err)
    raise RuntimeError(f"Couldn't download the DB-IP database ({last_err})")


def _check(path: Path) -> None:
    import maxminddb
    with maxminddb.open_database(str(path)) as reader:
        if not reader.metadata().database_type:
            raise ValueError("not an mmdb file")


def is_private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or \
        (a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"))   # CGNAT / Tailscale


def _name(rec: dict | None) -> str | None:
    if not rec:
        return None
    names = rec.get("names") or {}
    return names.get("en") or next(iter(names.values()), None)


def parse_record(rec: dict | None) -> dict:
    rec = rec or {}
    loc = rec.get("location") or {}
    subs = rec.get("subdivisions") or []
    country = rec.get("country") or {}
    return {"city": _name(rec.get("city")), "region": _name(subs[0]) if subs else None,
            "country_code": country.get("iso_code"), "country": _name(country),
            "lat": loc.get("latitude"), "lon": loc.get("longitude")}


def fill(conn: sqlite3.Connection, data_dir: Path, now: float | None = None, limit: int = 5000) -> dict:
    """Look up every IP in history that's missing or stale in ip_geo."""
    now = int(now or time.time())
    stale = now - LOOKUP_TTL_DAYS * 86400
    ips = [r[0] for r in conn.execute(
        "SELECT DISTINCT p.ip_address FROM plays p LEFT JOIN ip_geo g ON g.ip = p.ip_address "
        "WHERE p.ip_address IS NOT NULL AND p.ip_address != '' AND (g.ip IS NULL OR (g.is_private = 0 AND g.looked_up_at < ?)) "
        "LIMIT ?", (stale, limit))]
    if not ips:
        return {"looked_up": 0, "private": 0, "unknown": 0}
    reader = None
    path = db_path(data_dir)
    if path.exists():
        import maxminddb
        reader = maxminddb.open_database(str(path))
    found = private = unknown = 0
    try:
        for ip in ips:
            if is_private(ip):
                row = {"city": None, "region": None, "country_code": None, "country": None, "lat": None, "lon": None}
                priv = 1
                private += 1
            elif reader is None:
                continue
            else:
                try:
                    row = parse_record(reader.get(ip))
                except ValueError:
                    row = parse_record(None)
                priv = 0
                if row["country_code"]:
                    found += 1
                else:
                    unknown += 1
            conn.execute(
                "INSERT INTO ip_geo (ip, is_private, city, region, country_code, country, lat, lon, looked_up_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET is_private = excluded.is_private, "
                "city = excluded.city, region = excluded.region, country_code = excluded.country_code, "
                "country = excluded.country, lat = excluded.lat, lon = excluded.lon, looked_up_at = excluded.looked_up_at",
                (ip, priv, row["city"], row["region"], row["country_code"], row["country"], row["lat"], row["lon"], now))
    finally:
        if reader is not None:
            reader.close()
    return {"looked_up": found, "private": private, "unknown": unknown, "pending_no_db": reader is None}


def pending(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT p.ip_address) FROM plays p LEFT JOIN ip_geo g ON g.ip = p.ip_address "
        "WHERE p.ip_address IS NOT NULL AND p.ip_address != '' AND g.ip IS NULL").fetchone()[0]


def locations(conn: sqlite3.Connection, *, user_id: int | None = None, since: int | None = None) -> list[dict]:
    """Where plays came from, one row per place. LAN plays become one 'Home network' row."""
    where, params = ["p.ip_address IS NOT NULL"], []
    if user_id is not None:
        where.append("p.user_id = ?")
        params.append(user_id)
    if since is not None:
        where.append("p.started_at >= ?")
        params.append(since)
    rows = conn.execute(
        f"""SELECT CASE WHEN p.is_local = 1 THEN 'lan' WHEN g.is_private = 1 THEN 'private' ELSE
                     COALESCE(g.city, '') || '|' || COALESCE(g.country_code, '') END AS place,
                   MAX(g.city) AS city, MAX(g.region) AS region, MAX(g.country) AS country,
                   MAX(g.country_code) AS country_code, AVG(g.lat) AS lat, AVG(g.lon) AS lon,
                   MAX(CASE WHEN p.is_local = 1 THEN 1 ELSE 0 END) AS is_lan,
                   MAX(CASE WHEN p.is_local = 0 AND g.is_private = 1 THEN 1 ELSE 0 END) AS is_private,
                   SUM(CASE WHEN p.group_id IS NULL THEN 1 ELSE 0 END) AS plays,
                   COUNT(DISTINCT p.ip_address) AS ips, MAX(p.started_at) AS last_seen,
                   SUM(CASE WHEN g.ip IS NULL THEN 1 ELSE 0 END) AS unresolved
            FROM plays p LEFT JOIN ip_geo g ON g.ip = p.ip_address
            WHERE {' AND '.join(where)} GROUP BY place ORDER BY plays DESC""", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["is_lan"]:
            d.update(label="Home network", lat=None, lon=None)
        elif d["is_private"]:
            d.update(label="Private network or VPN", lat=None, lon=None)
        elif d["country"]:
            d["label"] = ", ".join(x for x in (d["city"], d["country"]) if x)
        else:
            d["label"] = "Not looked up yet" if d["unresolved"] else "Unknown location"
        out.append(d)
    return out
