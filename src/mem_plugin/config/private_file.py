"""Write private JSON files atomically."""

import json
import os
from uuid import uuid4


def write_private_json(path, data):
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with os.fdopen(
            os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
        ) as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
