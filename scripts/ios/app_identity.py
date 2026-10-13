"""Identity for Kria's executable and Debug code dylibs (stdlib only)."""

import hashlib


def app_code_sha256(app):
    digest = hashlib.sha256()
    for path in sorted([app / "Kria", *app.glob("*.dylib")]):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
