"""Live-eval fixture URI normalizer.

Eval fixtures store `input.file_uri` as a bucket-relative GCS path
(`clips/<id>.mp4`, `templates/<uuid>/reference.mp4`). The Studio
`GEMINI_API_KEY` cannot resolve those — Gemini's File API requires a
`files/<id>` ID, a `gs://` URI (Vertex auth only), or an HTTPS URL.
Production never sees this because `template_orchestrate` /
`orchestrate` call `gemini_upload_and_wait()` to convert local paths to
`files/<id>` IDs before invoking the agent.

This module mirrors that exact step for the eval harness's live path:
given a bucket-relative path, download from GCS to a temp file, upload
to Gemini File API, return the `files/<id>` URI. Cached per process so
the same fixture used by multiple tests is uploaded once.

Cassette / replay mode never calls this — `CassetteModelClient.invoke`
ignores `media_uri` entirely.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Protocol


class _DownloadFn(Protocol):
    def __call__(self, object_path: str, local_path: str) -> None: ...


class _UploadFn(Protocol):
    def __call__(self, path: str, timeout: int = ...) -> Any: ...


def is_bucket_relative(file_uri: str) -> bool:
    """True if `file_uri` is a bucket-relative path that Gemini cannot resolve.

    Gemini's File API accepts three URI shapes:
      - `files/<id>`             — Files API resource name
      - `gs://<bucket>/<path>`   — requires Vertex AI service-account auth
      - `https://...`            — public HTTPS URL

    Anything else (`clips/<id>.mp4`, `templates/<uuid>/foo.mp4`) is a
    bucket-relative path that needs an upload step.
    """
    if not file_uri:
        return False
    return not (
        file_uri.startswith("files/")
        or file_uri.startswith("gs://")
        or file_uri.startswith("http://")
        or file_uri.startswith("https://")
    )


class FixtureUploader:
    """Downloads a GCS object and uploads it to Gemini File API.

    Cache lives for the pytest session — Files API IDs survive ~48hr,
    plenty of headroom for one test run. Multiple fixtures pointing at
    the same bucket path share a single upload.
    """

    def __init__(
        self,
        *,
        download_fn: _DownloadFn,
        upload_fn: _UploadFn,
        upload_timeout_s: int = 120,
    ) -> None:
        self._download = download_fn
        self._upload = upload_fn
        self._timeout = upload_timeout_s
        self._cache: dict[str, str] = {}

    def normalize(self, file_uri: str) -> str:
        """Return a Gemini-resolvable URI for `file_uri`.

        Pass-through for `files/<id>`, `gs://`, and HTTPS. For
        bucket-relative paths: download → upload → cache → return
        `files/<id>`.
        """
        if not is_bucket_relative(file_uri):
            return file_uri
        if file_uri in self._cache:
            return self._cache[file_uri]

        suffix = Path(file_uri).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            local_path = tmp.name
        try:
            self._download(file_uri, local_path)
            file_ref = self._upload(local_path, timeout=self._timeout)
            uri = file_ref.name  # "files/<id>"
        finally:
            Path(local_path).unlink(missing_ok=True)

        self._cache[file_uri] = uri
        return uri

    def normalize_input(self, input_dict: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of `input_dict` with `file_uri` normalized in place.

        Leaves all other fields untouched. No-op if `file_uri` is absent
        or already in a Gemini-resolvable shape.
        """
        if "file_uri" not in input_dict:
            return input_dict
        file_uri = input_dict["file_uri"]
        if not isinstance(file_uri, str):
            return input_dict
        normalized = self.normalize(file_uri)
        if normalized == file_uri:
            return input_dict
        return {**input_dict, "file_uri": normalized}


def build_default_uploader() -> FixtureUploader:
    """Construct a FixtureUploader wired to the prod GCS + Gemini helpers.

    Imported lazily so importing this module doesn't drag in google.cloud
    at collection time.
    """
    from app.pipeline.agents.gemini_analyzer import gemini_upload_and_wait
    from app.storage import download_to_file

    return FixtureUploader(
        download_fn=download_to_file,
        upload_fn=gemini_upload_and_wait,
    )
