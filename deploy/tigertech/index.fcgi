#!/home/USERNAME/emeetinghouse/venv/bin/python3
"""FastCGI entry point for TigerTech shared hosting.

Deploy layout:
  ~/html/index.fcgi         <- this file (the only app file inside the web root)
  ~/html/.htaccess          <- rewrites all requests to index.fcgi
  ~/emeetinghouse/          <- app.py, persistence.py, tick_runner.py,
                                templates/, static/, config.py, data/
                                (outside the web root: never downloadable)

Replace USERNAME in the shebang line above with the TigerTech account
username (APP_DIR resolves itself via the home directory), then:
  chmod 0755 index.fcgi
"""

import os
import sys

APP_DIR = os.path.expanduser("~/emeetinghouse")
sys.path.insert(0, APP_DIR)

import config  # noqa: E402  (sets os.environ; see config.example.py)
from app import app as flask_app  # noqa: E402
from flup.server.fcgi import WSGIServer  # noqa: E402


class ScriptNameStripper:
    """Flask needs SCRIPT_NAME empty for url_for() to build correct paths."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __call__(self, environ, start_response):
        environ["SCRIPT_NAME"] = ""
        return self.wrapped(environ, start_response)


if __name__ == "__main__":
    WSGIServer(ScriptNameStripper(flask_app)).run()
