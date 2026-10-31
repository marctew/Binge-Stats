"""Insights pages (SPEC.md sections 11 and 12): binges, transcodes, library health."""
from __future__ import annotations

from flask import Blueprint, Response, current_app, redirect, render_template, request, url_for

from ..services import binges, insights, stats

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
