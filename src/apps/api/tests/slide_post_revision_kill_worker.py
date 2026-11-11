"""Bounded subprocess fixture: local scratch DB only, no broker/provider access."""

import json
import os
import socket
import sys
from urllib.parse import urlparse

import psycopg2
import pydantic_settings

expected = urlparse(os.environ["DATABASE_URL"])
assert expected.hostname in {"localhost", "127.0.0.1", "::1"}
assert expected.path.endswith("_test")
connect = socket.socket.connect


def guarded(self, address):
    local = isinstance(address, tuple) and address[0] in {"localhost", "127.0.0.1", "::1"}
    assert local and address[1] == (expected.port or 5432), "External network forbidden"
    return connect(self, address)


socket.socket.connect = guarded
socket.socket.connect_ex = guarded
original = psycopg2.connect


def guarded_db(dsn=None, *args, **kwargs):
    values = psycopg2.extensions.parse_dsn(dsn or "")
    values.update(kwargs)
    assert values.get("dbname") == expected.path.lstrip("/")
    assert values.get("host") in {"localhost", "127.0.0.1", "::1"}
    assert int(values.get("port", 5432)) == (expected.port or 5432)
    return original(dsn, *args, **kwargs)


psycopg2.connect = guarded_db
initialize = pydantic_settings.BaseSettings.__init__


def isolated_settings(self, **values):
    values["_env_file"] = None
    initialize(self, **values)


pydantic_settings.BaseSettings.__init__ = isolated_settings
from app.services import slide_post_render_revisions as revisions  # noqa: E402

identifier, result = json.loads(sys.argv[1])
assert revisions.claim_revision(identifier)
if result is not None:
    assert revisions.stage_result(identifier, result)
print("COMMITTED", flush=True)
sys.stdin.read()
