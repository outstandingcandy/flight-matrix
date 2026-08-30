#!/usr/bin/env python3
"""Create + install a GitHub App via the App Manifest flow.

Automates the setup for the ``auto-update-pr`` workflow to trigger
downstream CI. The default ``GITHUB_TOKEN`` a workflow gets can't
trigger further workflow runs (GitHub's anti-loop safety), so
``update-branch`` pushes it makes don't fire CI on the PRs it
updated. A GitHub App has its own bot identity, so pushes made with
an install token *do* trigger CI.

Flow this script drives (three user clicks, everything else scripted):

1. Boot a local HTTP server on ``127.0.0.1:8964`` — that's the
   redirect target for the App Manifest flow. Any free port works;
   this one avoids the common 3000/8000/8080 dev-server clashes.
2. Write a one-page HTML form to ``/tmp/gh_app_setup.html`` that
   auto-submits a manifest to ``https://github.com/settings/apps/new``.
   Open it in the default browser.
3. GitHub renders a review page with all the App fields pre-filled
   from the manifest — you click **Create GitHub App**. GitHub then
   redirects to our local server at ``?code=<one-time>``.
4. Server exchanges the code via
   ``POST /app-manifests/{code}/conversions`` for the App's numeric
   ID, its RSA private key, and a webhook secret.
5. Server calls ``gh secret set`` to push ``APP_ID`` and
   ``APP_PRIVATE_KEY`` to the repo.
6. Server prints the install URL. You click through **Install** →
   pick this one repo → **Install**. Done.

Neither of the two remaining clicks can be scripted — GitHub
deliberately requires human confirmation for App creation and
installation. Everything in between is API.

Usage: ``python scripts/setup-github-app.py``
Prereqs: ``gh auth status`` OK; port 8964 free.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "outstandingcandy/flight-matrix")
PORT = 8964
APP_NAME = "flight-matrix-ci-bot"

# The manifest is the whole App spec in one JSON object — permissions,
# events, redirect URL. GitHub reads this to pre-fill the "Create App"
# review page. See:
# https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
MANIFEST = {
    "name": APP_NAME,
    "url": f"https://github.com/{REPO}",
    "hook_attributes": {"active": False},
    "redirect_url": f"http://127.0.0.1:{PORT}/callback",
    "public": False,
    "default_permissions": {
        "contents": "write",  # push merge commits from update-branch
        "pull_requests": "write",  # update PR metadata + branch
        "metadata": "read",  # auto-required for any repo access
    },
    "default_events": [],
}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the post-creation redirect from GitHub."""

    # Silence the default access log — it clutters the terminal.
    def log_message(self, format, *args):
        pass

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path != "/callback" or "code" not in params:
            self._respond(400, "Missing ?code — did GitHub redirect here?")
            return

        code = params["code"][0]

        try:
            data = self._exchange_code(code)
        except Exception as e:  # noqa: BLE001
            self._respond(500, f"Failed to exchange manifest code: {e}")
            self.server.error = e  # type: ignore[attr-defined]
            return

        try:
            self._push_secrets(data["id"], data["pem"])
        except Exception as e:  # noqa: BLE001
            self._respond(500, f"Got App details but failed to push secrets: {e}")
            self.server.error = e  # type: ignore[attr-defined]
            return

        install_url = f"https://github.com/apps/{data['slug']}/installations/new"
        self._respond(
            200,
            f"App created: {data['name']} (ID {data['id']}).\n"
            f"Secrets APP_ID + APP_PRIVATE_KEY pushed to {REPO}.\n\n"
            f"One more click — INSTALL the app on the repo:\n{install_url}",
        )
        self.server.result = {  # type: ignore[attr-defined]
            "app_id": data["id"],
            "app_slug": data["slug"],
            "install_url": install_url,
        }

    @staticmethod
    def _exchange_code(code: str) -> dict:
        """Trade the one-time manifest code for the App's real credentials."""
        req = urllib.request.Request(
            f"https://api.github.com/app-manifests/{code}/conversions",
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.load(resp)

    @staticmethod
    def _push_secrets(app_id: int, private_key: str) -> None:
        """``gh secret set`` for both values. Streams the key via stdin so
        it never lands on the process argv (visible to ``ps``)."""
        for name, value in (("APP_ID", str(app_id)), ("APP_PRIVATE_KEY", private_key)):
            subprocess.run(
                ["gh", "secret", "set", name, "--repo", REPO, "--body", "-"],
                input=value,
                text=True,
                check=True,
                # Silence gh's per-call "Set secret X" output — we
                # already print a summary at the end.
                stdout=subprocess.DEVNULL,
            )

    def _respond(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


def main() -> int:
    # Sanity checks — surface obvious problems before opening the browser.
    result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print("gh CLI not authenticated. Run: gh auth login", file=sys.stderr)
        return 1

    html_body = f"""<!doctype html>
<html>
<head><title>Create {APP_NAME}</title></head>
<body onload="document.forms[0].submit()">
  <form action="https://github.com/settings/apps/new" method="post">
    <input type="hidden" name="manifest" value='{json.dumps(MANIFEST)}'>
    <p>Redirecting to GitHub… if it doesn't happen automatically,
       press <button type="submit">Create GitHub App</button>.</p>
  </form>
</body>
</html>"""
    html_path = Path(tempfile.gettempdir()) / "gh_app_setup.html"
    html_path.write_text(html_body, encoding="utf-8")

    print(f"[1/3] Opening browser: file://{html_path}")
    print(f"      Server listening on http://127.0.0.1:{PORT}/callback")
    print()
    print("[2/3] In the browser, GitHub will show a review page with all")
    print(f'      fields pre-filled for the "{APP_NAME}" App. Click the')
    print("      big green **Create GitHub App** button.")
    print()

    webbrowser.open(f"file://{html_path}")

    # Reuse address for a clean re-run if a prior attempt crashed.
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), CallbackHandler) as httpd:
        httpd.result = None  # type: ignore[attr-defined]
        httpd.error = None  # type: ignore[attr-defined]
        # Handle one request — GitHub only redirects once. Any errored
        # response counts too, so this returns whether or not the flow
        # succeeded; ``httpd.result`` / ``httpd.error`` say which.
        httpd.timeout = 300  # 5 min for the click
        while httpd.result is None and httpd.error is None:
            httpd.handle_request()

        if httpd.error is not None:  # type: ignore[attr-defined]
            return 2

        info = httpd.result  # type: ignore[attr-defined]

    print()
    print(f"[3/3] App created — ID {info['app_id']}, slug {info['app_slug']}.")
    print("      Secrets APP_ID + APP_PRIVATE_KEY pushed to the repo.")
    print()
    print("      Last manual step — install the App on the repo:")
    print(f"      {info['install_url']}")
    print()
    print("      Pick 'Only select repositories' → outstandingcandy/flight-matrix → Install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
