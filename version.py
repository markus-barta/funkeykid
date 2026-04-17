"""funkeykid version info."""
VERSION = "2.8.3"
BUILD = __import__("os").environ.get("FUNKEYKID_BUILD", "dev")
REPO = "https://github.com/markus-barta/funkeykid"

def build_time():
    """Return container build time from /app/.build_time, or None."""
    try:
        with open(__import__("os").path.join(__import__("os").path.dirname(__file__), ".build_time")) as f:
            return f.read().strip()
    except Exception:
        return None
