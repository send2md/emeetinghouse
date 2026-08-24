"""Template for the private config TigerTech's index.fcgi and tick.py load.

Copy this to ~/emeetinghouse/config.py (i.e. next to app.py, OUTSIDE
~/html so it's never web-accessible) and fill in real values. It's
imported for its side effect of populating os.environ before app.py
is imported, so app.py itself needs no host-specific changes.

Do not commit the filled-in config.py to git.
"""

import os

# Outside ~/html — the SQLite file must never be reachable over HTTP.
os.environ["EMEETINGHOUSE_DB"] = "/home/USERNAME/emeetinghouse/data/emeetinghouse.db"

# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
os.environ["EMEETINGHOUSE_SECRET_KEY"] = "REPLACE-ME"

# A password for /admin, separate from any Participant's login.
os.environ["EMEETINGHOUSE_ADMIN_PASSWORD"] = "REPLACE-ME"
