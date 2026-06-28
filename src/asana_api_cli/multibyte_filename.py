"""RFC 5987 multipart filename support for the ``python-asana`` SDK.

``MultibyteFilenameSupport`` patches ``urllib3``'s multipart encoder to add the
RFC 5987 ``filename*=`` parameter so non-ASCII attachment filenames round-trip.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any

from urllib3.fields import RequestField


class MultibyteFilenameSupport:
    """Make multipart uploads round-trip filenames with non-ASCII characters.

    In ``python-asana`` 5.2.5 (the latest version checked, and likely later
    ones too), uploading a file whose name has characters outside ASCII
    stores a garbled (mojibake) name on Asana: the SDK's multipart encoder
    emits only ``filename="..."`` and omits the RFC 5987 ``filename*=``
    parameter the server needs to decode them. This context manager patches
    ``urllib3.fields.RequestField.make_multipart`` to add
    ``filename*=utf-8''<percent-encoded>`` for such names, scoped to the
    ``with`` block::

        with MultibyteFilenameSupport():
            # urllib3-based uploads in this block emit filename*=
            ...
    """

    def __init__(self) -> None:
        self._original: Callable[..., None] | None = None

    def install(self) -> None:
        if self._original is not None:
            return
        self._original = RequestField.make_multipart
        original = self._original

        def _patched(
            field: RequestField,
            content_disposition: str | None = None,
            content_type: str | None = None,
            content_location: str | None = None,
        ) -> None:
            original(field, content_disposition, content_type, content_location)
            filename = field._filename  # pyright: ignore[reportPrivateUsage]
            if filename and any(ord(c) > 127 for c in filename):
                encoded = urllib.parse.quote(filename, safe="")
                existing = field.headers.get("Content-Disposition") or ""
                field.headers["Content-Disposition"] = existing + f"; filename*=utf-8''{encoded}"

        RequestField.make_multipart = _patched  # pyright: ignore[reportAttributeAccessIssue]

    def uninstall(self) -> None:
        if self._original is None:
            return
        RequestField.make_multipart = self._original  # pyright: ignore[reportAttributeAccessIssue]
        self._original = None

    def __enter__(self) -> MultibyteFilenameSupport:
        self.install()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()
