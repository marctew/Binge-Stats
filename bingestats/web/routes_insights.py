"""Insights pages (SPEC.md sections 11 and 12): binges, transcodes, library health."""
from __future__ import annotations

from flask import Blueprint, Response, current_app, redirect, render_template, request, url_for

from ..services import arr, binges, collection, insights, playback, stats

bp = Blueprint("insights", __name__, url_prefix="/insights")


def _db():
    return current_app.get_db()


def _range(default: str):
    conn = _db()
    args = request.args.to_dict()
    args.setdefault("range", default)
    preset = args["range"] if args["range"] in (*stats.PRESETS, "all") else default
    return stats.make_range(conn, preset=preset, date_from=args.get("from") or None,
                            date_to=args.get("to") or None), preset


@bp.route("")
def index():
    return redirect(url_for("insights.binges_page"))


@bp.route("/binges")
def binges_page():
    conn = _db()
    rng, preset = _range("1y")
    span = {"start": rng.start, "end": rng.end}
    return render_template("pages/insights_binges.html", rng=rng, preset=preset,
                           totals=binges.totals(conn, **span), longest=binges.longest(conn, **span, limit=12),
                           shows=binges.by_show(conn, **span, limit=10), people=binges.leaderboard(conn, **span))


@bp.route("/transcodes")
def transcodes_page():
    conn = _db()
    rng, preset = _range("90d")
    by = {f: insights.transcode_by(conn, rng, f) for f in ("user", "product", "player", "src_video_codec",
                                                            "src_audio_codec", "src_resolution")}
    return render_template("pages/insights_transcodes.html", rng=rng, preset=preset,
                           summary=insights.transcode_summary(conn, rng), by=by, labels=insights.BY_FIELDS,
                           candidates=insights.reencode_candidates(conn, None, limit=40))


def _health_args():
    lib = request.args.get("library", "")
    kind = request.args.get("type", "")
    tab = request.args.get("tab", "stale")
    return (int(lib) if lib.isdigit() else None, kind if kind in ("movie", "show") else None,
            tab if tab in insights.TABS else "stale")


@bp.route("/library-health")
def library_health_page():
    conn = _db()
    library_id, kind, tab = _health_args()
    data = insights.library_health(conn, library_id=library_id, kind=kind)
    libs = conn.execute("SELECT id, name, type FROM libraries WHERE is_active = 1 AND type IN ('movie', 'show') "
                        "ORDER BY name").fetchall()
    return render_template("pages/insights_health.html", data=data, tab=tab, tabs=insights.TABS, libs=libs,
                           library_id=library_id, kind=kind, rows=data[tab][:100], total_rows=len(data[tab]))


@bp.route("/library-health.csv")
def library_health_csv():
    conn = _db()
    library_id, kind, tab = _health_args()
    data = insights.library_health(conn, library_id=library_id, kind=kind)
    body = insights.health_csv(data[tab], stats._tz(conn))
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="binge-stats-{tab}.csv"'})


# -- collection ------------------------------------------------------------------------

COLLECTION_LISTS = ("reencode", "upgrades", "cutoff", "gaps", "collections", "missing_films")
ARR_LISTS = {"cutoff": ("sonarr", "radarr"), "collections": ("radarr",), "missing_films": ("radarr",)}


def _collection_rows(conn, lst: str, library_id, kind, sonarr: bool) -> list[dict]:
    if lst == "gaps":
        if kind == "movie":
            return []
        return arr.sonarr_gaps(conn) if sonarr and library_id is None else collection.gaps(conn, library_id)
    if lst == "cutoff":
        rows = arr.below_cutoff(conn)
        return [r for r in rows if not kind or (r["app"] == "radarr") == (kind == "movie")]
    if lst == "collections":
        return arr.collections(conn) if kind != "show" else []
    if lst == "missing_films":
        return arr.missing_films(conn) if kind != "show" else []
    return getattr(collection, lst)(conn, library_id, kind, limit=None)


@bp.route("/collection")
def collection_page():
    conn = _db()
    library_id, kind, _tab = _health_args()
    ready = {a: arr.ready(conn, a) for a in ("sonarr", "radarr")}
    available = [x for x in COLLECTION_LISTS if x not in ARR_LISTS or any(ready[a] for a in ARR_LISTS[x])]
    lst = request.args.get("list", "reencode")
    lst = lst if lst in available else "reencode"
    libs = conn.execute("SELECT id, name, type FROM libraries WHERE is_active = 1 AND type IN ('movie', 'show') "
                        "ORDER BY name").fetchall()
    rows = _collection_rows(conn, lst, library_id, kind, ready["sonarr"])
    gap_rows = _collection_rows(conn, "gaps", library_id, kind, ready["sonarr"]) if lst != "gaps" else rows
    return render_template("pages/insights_collection.html", m=collection.makeup(conn, library_id, kind), lst=lst,
                           rows=rows[:100], total_rows=len(rows), libs=libs, library_id=library_id, kind=kind,
                           gap_count=len(gap_rows), available=available, ready=ready,
                           sonarr_gaps=ready["sonarr"] and library_id is None,
                           series=arr.series_summary(conn) if ready["sonarr"] else None,
                           films=arr.radarr_summary(conn) if ready["radarr"] else None)


@bp.route("/collection.csv")
def collection_csv():
    conn = _db()
    library_id, kind, _tab = _health_args()
    lst = request.args.get("list", "reencode")
    if lst not in COLLECTION_LISTS:
        lst = "reencode"
    rows = _collection_rows(conn, lst, library_id, kind, arr.ready(conn, "sonarr"))
    if lst in ARR_LISTS or (lst == "gaps" and rows and "series_id" in rows[0]):
        body = arr.to_csv(lst, rows)
    else:
        body = collection.to_csv(lst, rows)
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="binge-stats-{lst}.csv"'})


# -- downloads ---------------------------------------------------------------------------

@bp.route("/downloads")
def downloads_page():
    conn = _db()
    return render_template("pages/insights_downloads.html", d=arr.downloads(conn),
                           ready=arr.ready(conn, "sabnzbd"), configured=arr.configured(conn)["sabnzbd"])


# -- playback ----------------------------------------------------------------------------

@bp.route("/playback")
def playback_page():
    conn = _db()
    rng, preset = _range("30d")
    return render_template("pages/insights_playback.html", rng=rng, preset=preset,
                           summary=playback.summary(conn, rng),
                           by={k: playback.breakdown(conn, rng, k) for k in ("user", "device", "place", "decision")},
                           worst=playback.worst(conn, rng))
