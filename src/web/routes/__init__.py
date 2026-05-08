"""Flask blueprints.

Each blueprint owns one slice of the URL tree. New routes go into an
existing blueprint (or a new one); do not register routes directly on the
Flask app any more.

Migration guide: docs/web-blueprints.md.
"""
