"""Sonarr, Radarr and SABnzbd: the only code that knows their APIs.

Read-only by construction: the clients have a GET method and nothing else,
so Binge Stats can never delete, unmonitor or change anything in them.
"""
