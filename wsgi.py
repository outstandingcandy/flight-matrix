"""WSGI entry point for gunicorn.

`web_app.py` builds the Flask `app` at import time but leaves `db_manager` and
`config` as `None` until `init_app()` runs, and every route reads those globals.
Each deployment target has to call it:

* AWS Lambda — `lambda_handler.py` calls it at cold start.
* Local dev — `web_app.py`'s `__main__` block calls it.
* gunicorn (the `gcp` and `local` VM targets) — this module.

Pointing the systemd unit straight at `web_app:app` skips that step, and the
result is not a startup failure but a healthy-looking service in which every
database-backed route answers 500 with `'NoneType' object has no attribute
'get_session'`. Only the routes that touch no table — the login redirect
included — keep working, so the deploy's health check passes and nginx serves
the site.

`init_app()` runs per worker rather than under `--preload`, which is deliberate:
a SQLAlchemy engine created before the fork would have its pooled sockets shared
by every worker.
"""

from __future__ import annotations

from web_app import app, init_app

init_app()

__all__ = ["app"]
