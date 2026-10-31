"""Notifications UI: rules, channels, delivery log, rule editor with preview."""
from __future__ import annotations

import json

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from ..services import notify

bp = Blueprint("notify", __name__, url_prefix="/notifications")


def _db():
    return current_app.get_db()


@bp.route("")
def index():
    conn = _db()
    channels = notify.list_channels(conn)
    names = {c["id"]: c["name"] for c in channels}
    return render_template("pages/notifications.html", rules=notify.list_rules(conn), channels=channels,
                           channel_names=names, log=notify.recent_log(conn, 40), triggers=notify.TRIGGERS)


# -- channels -------------------------------------------------------------------------

@bp.route("/channels", methods=["POST"])
def channel_create():
    try:
        notify.save_channel(_db(), request.form.get("name", ""), request.form.get("url", ""))
        flash("Channel added. Send it a test to check it works.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("notify.index") + "#channels")


@bp.route("/channels/<int:channel_id>", methods=["POST"])
def channel_update(channel_id: int):
    try:
        notify.save_channel(_db(), request.form.get("name", ""), request.form.get("url", ""), channel_id,
                            enabled=request.form.get("enabled") == "on")
        flash("Channel saved", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("notify.index") + "#channels")


@bp.route("/channels/<int:channel_id>/delete", methods=["POST"])
def channel_delete(channel_id: int):
    notify.delete_channel(_db(), channel_id)
    flash("Channel deleted and removed from its rules", "ok")
    return redirect(url_for("notify.index") + "#channels")


@bp.route("/channels/<int:channel_id>/test", methods=["POST"])
def channel_test(channel_id: int):
    # A single short HTTP call to the notification service, like stopping a
    # stream: done in the request so the result shows straight away.
    row = _db().execute("SELECT name, apprise_url FROM notify_channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        abort(404)
    ok, err = notify.send_one(row["apprise_url"], "Binge Stats test",
                              f"If you can read this, the '{row['name']}' channel works.")
    _db().execute("INSERT INTO notify_log (rule_id, channel_id, sent_at, dedupe_key, ok, error, title, body) "
                  "VALUES (NULL, ?, strftime('%s','now'), 'test', ?, ?, 'Binge Stats test', '')",
                  (channel_id, int(ok), err))
    msg = f"Test sent to {row['name']}" if ok else f"{row['name']} failed: {err}"
    return render_template("partials/toast.html", message=msg, kind="ok" if ok else "error")


# -- rules ----------------------------------------------------------------------------

def _editor(rule: dict, error: str | None = None):
    conn = _db()
    fields = {t: notify.fields_for(t) for t in notify.TRIGGERS}
    variables = {}
    for t in notify.TRIGGERS:
        ctx, real = notify.preview_context(conn, t)
        variables[t] = {"real": real, "vars": [[k, v if isinstance(v, (int, float, bool)) or v is None else str(v)[:80]]
                                               for k, v in notify.variables(ctx)]}
    return render_template("pages/notification_rule.html", rule=rule, error=error, triggers=notify.TRIGGERS,
                           fields=fields, choices=notify.choices(conn), op_labels=notify.OP_LABELS,
                           variables=variables, channels=notify.list_channels(conn))


def _form_rule() -> dict:
    try:
        conditions = json.loads(request.form.get("conditions") or "[]")
    except ValueError:
        conditions = []
    return {"name": request.form.get("name", ""), "trigger": request.form.get("trigger", ""),
            "conditions": conditions, "title_template": request.form.get("title_template", ""),
            "body_template": request.form.get("body_template", ""),
            "channel_ids": [int(c) for c in request.form.getlist("channel_ids") if c.isdigit()],
            "enabled": request.form.get("enabled") == "on", "cooldown_s": request.form.get("cooldown_s") or 0,
            "include_poster": request.form.get("include_poster") == "on"}


@bp.route("/rules/new", methods=["GET", "POST"])
def rule_new():
    if request.method == "POST":
        data = _form_rule()
        try:
            notify.save_rule(_db(), data)
        except ValueError as e:
            return _editor({**data, "id": None}, str(e)), 400
        flash("Rule saved", "ok")
        return redirect(url_for("notify.index"))
    trigger = request.args.get("trigger") if request.args.get("trigger") in notify.TRIGGERS else "play_start"
    return _editor({"id": None, "name": "", "trigger": trigger, "conditions": [], "title_template": "",
                    "body_template": "", "channel_ids": [], "enabled": True, "cooldown_s": 0, "include_poster": True})


@bp.route("/rules/<int:rule_id>", methods=["GET", "POST"])
def rule_edit(rule_id: int):
    conn = _db()
    rule = notify.get_rule(conn, rule_id)
    if rule is None:
        abort(404)
    if request.method == "POST":
        data = _form_rule()
        try:
            notify.save_rule(conn, data, rule_id)
        except ValueError as e:
            return _editor({**data, "id": rule_id}, str(e)), 400
        flash("Rule saved", "ok")
        return redirect(url_for("notify.index"))
    return _editor(rule)


@bp.route("/rules/<int:rule_id>/toggle", methods=["POST"])
def rule_toggle(rule_id: int):
    try:
        notify.set_rule_enabled(_db(), rule_id, request.form.get("enabled") == "1")
    except (LookupError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("notify.index"))


@bp.route("/rules/<int:rule_id>/delete", methods=["POST"])
def rule_delete(rule_id: int):
    notify.delete_rule(_db(), rule_id)
    flash("Rule deleted", "ok")
    return redirect(url_for("notify.index"))


@bp.route("/preview", methods=["POST"])
def preview():
    data = _form_rule()
    if data["trigger"] not in notify.TRIGGERS:
        return ""
    out = notify.preview(_db(), data["trigger"], data["title_template"], data["body_template"], data["conditions"])
    return render_template("partials/notify_preview.html", p=out)
