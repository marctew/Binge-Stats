"""Rules shipped with Binge Stats. Seeded disabled with no channels by
migration 2; switch them on in Notifications once a channel exists.

Kept separate from the migration so a later change here doesn't alter what
an old migration did: migration 2 copies these values once, at upgrade time.
"""

DEFAULT_RULES = [
    {
        "name": "Someone started watching",
        "trigger": "play_start",
        "title": "▶ {{ user.name }} is watching {{ item.display_title }}",
        "body": ("{% if item.subtitle %}{{ item.subtitle }}\n{% endif %}"
                 "{{ player.name }} ({{ player.product }}) · {{ stream.decision_label }}"
                 "{% if not location.is_local %} · remote{% if location.place %} from {{ location.place }}{% endif %}{% endif %}"),
    },
    {
        "name": "Transcoding because of subtitles",
        "trigger": "transcode_start",
        "conditions": [{"field": "stream.reasons", "op": "contains", "value": "subtitle_burn"}],
        "title": "Subtitles are being burned in for {{ user.name }}",
        "body": ("{{ item.display_title }}{% if item.subtitle %} · {{ item.subtitle }}{% endif %}\n"
                 "{{ stream.quality }} on {{ player.product }}. Reasons: {{ stream.reasons_text }}"),
        "cooldown_s": 3600,
    },
    {
        "name": "Plex server down",
        "trigger": "server_down",
        "title": "⚠ {{ server.name }} is not responding",
        "body": "Binge Stats can't reach Plex: {{ error }}",
        "include_poster": False,
    },
    {
        "name": "Plex server back",
        "trigger": "server_up",
        "title": "✓ {{ server.name }} is back",
        "body": "Plex was unreachable for {{ down_for | duration }}.",
        "include_poster": False,
    },
    {
        "name": "Recently added",
        "trigger": "recently_added",
        "title": "New on {{ server.name }}: {{ headline }}",
        "body": ("{% for i in items[:10] %}• {{ i.display_title }}\n{% endfor %}"
                 "{% if items | length > 10 %}…and {{ items | length - 10 }} more{% endif %}"),
    },
]


# Added by migration 3 (milestone 5).
LIMIT_RULES = [
    {
        "name": "Stream limit reached",
        "trigger": "stream_limit",
        "title": "{{ user.name }} hit their stream limit ({{ count }}/{{ limit }})",
        "body": ("{% for s in sessions %}• {{ s.title }} on {{ s.player }}\n{% endfor %}"
                 "{% if stopped %}The newest stream was stopped.{% endif %}"),
        "include_poster": False,
    },
    {
        "name": "Possible account sharing",
        "trigger": "concurrent_ips",
        "title": "{{ user.name }} is streaming from two places at once",
        "body": "{{ places_text }}\n{% for s in sessions %}• {{ s.title }} on {{ s.player }} ({{ s.ip }})\n{% endfor %}",
        "include_poster": False,
        "cooldown_s": 21600,
    },
]
