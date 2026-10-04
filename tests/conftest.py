import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeStore:
    """Stands in for the Cloud Storage bucket."""

    folder = "secret"

    def __init__(self):
        self.files = {}

    def list(self, prefix):
        return sorted(name for name in self.files if name.startswith(prefix))

    def read_bytes(self, name):
        data = self.files.get(name)
        return data.encode() if isinstance(data, str) else data

    def read_json(self, name):
        return json.loads(self.files[name]) if name in self.files else None

    def write(self, name, data, content_type, cache=False):
        self.files[name] = data

    def delete(self, name):
        self.files.pop(name, None)

    def url(self, name):
        return f"https://storage.googleapis.com/bucket/secret/{name}"
