"""Minimal LIMS client used only to catalog accessioned data.

There is deliberately no search or free-form query method: Slack users cannot browse the
LIMS through the bot. The bot only

* checks the one tube a user names while accessioning files (``registry_fields``), and
* records an approved dataset against that tube (``link_dataset``).

Only an allowlist of non-PHI fields is kept from LIMS responses (``SAFE_FIELDS``); anything
the LIMS marks as PHI is dropped on arrival, and error messages never include response
bodies, which could contain PHI.

Expected LIMS endpoints (see CHANNELS.md):

    GET  /api/samples/<sample ID or LIMS number>/registry-fields      (read token)
         200 {"found": true, "lims_number": "000000042", "safe": {...}, "phi": {...}}
         404 {"found": false}
    POST /api/samples/<LIMS number>/files                              (write token)
         body {"registry_id", "instance", "paths", "accessioned_by", "approved_by"}
         201 {"status": "created"} | 200 {"status": "exists"}
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from instance import LimsConfig

# Registry fields the LIMS may fill in. Anything else in a response is ignored.
SAFE_FIELDS = ("participant_id", "sample_type", "analyte", "platform", "library_type", "species")
_LIMS_NUMBER_RE = re.compile(r"^\d{1,9}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")


class LimsError(RuntimeError):
    """The LIMS could not be reached or rejected the request (message is PHI-free)."""


@dataclass(frozen=True)
class LimsTube:
    found: bool
    lims_number: str = ""
    safe: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Channel-safe description, e.g. 'LIMS #000000042 · P101'."""
        parts = [f"LIMS #{self.lims_number}"] if self.lims_number else []
        if self.safe.get("participant_id"):
            parts.append(self.safe["participant_id"])
        return " · ".join(parts)


Opener = Callable[[urllib.request.Request, float], tuple[int, bytes]]


def _default_opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def normalize_lims_number(value: str) -> str:
    value = str(value or "").strip()
    if not _LIMS_NUMBER_RE.match(value):
        raise LimsError("The LIMS returned an invalid LIMS number.")
    return value.zfill(9)


class LimsClient:
    def __init__(self, config: LimsConfig, opener: Opener | None = None):
        self.config = config
        self._open = opener or _default_opener

    @property
    def can_read(self) -> bool:
        return bool(self.config.read_token)

    @property
    def can_write(self) -> bool:
        return bool(self.config.write_token)

    def _request(self, method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
        if not token:
            raise LimsError("No LIMS token is configured for this request.")
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.config.url + path, data=data, method=method,
            headers={"X-Api-Token": token, "Accept": "application/json",
                     **({"Content-Type": "application/json"} if data else {})},
        )
        try:
            status, body = self._open(request, self.config.timeout_seconds)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LimsError(f"The LIMS could not be reached ({type(exc).__name__}).") from None
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

    def registry_fields(self, key: str) -> LimsTube:
        """Look up the single tube a user is accessioning, by sample ID or LIMS number."""
        key = (key or "").strip()
        if not _KEY_RE.match(key):
            raise ValueError("Enter a sample ID or LIMS number (letters, digits and hyphens only).")
        status, body = self._request(
            "GET", f"/api/samples/{urllib.parse.quote(key, safe='')}/registry-fields", self.config.read_token
        )
        if status == 404 and body.get("found") is False:
            return LimsTube(found=False)
        if status in (401, 403):
            raise LimsError("The LIMS rejected the bot's read token.")
        if status != 200 or body.get("found") is not True:
            raise LimsError(f"The LIMS check is not available (HTTP {status}).")
        safe_in = body.get("safe") if isinstance(body.get("safe"), dict) else {}
        safe = {
            name: str(safe_in[name]).strip()
            for name in SAFE_FIELDS
            if isinstance(safe_in.get(name), (str, int)) and str(safe_in[name]).strip()
        }
        return LimsTube(found=True, lims_number=normalize_lims_number(body.get("lims_number")), safe=safe)

    def link_dataset(
        self, lims_number: str, *, registry_id: str, instance: str, paths: list[str],
        accessioned_by: str, approved_by: str,
    ) -> str:
        """Record an accessioned dataset on its tube. Returns 'created' or 'exists'."""
        lims_number = normalize_lims_number(lims_number)
        status, body = self._request(
            "POST", f"/api/samples/{lims_number}/files", self.config.write_token,
            {"registry_id": registry_id, "instance": instance, "paths": paths,
             "accessioned_by": accessioned_by, "approved_by": approved_by},
        )
        if status in (401, 403):
            raise LimsError("The LIMS rejected the bot's write token.")
        if status not in (200, 201):
            raise LimsError(f"The LIMS did not record the dataset (HTTP {status}).")
        return "created" if status == 201 else str(body.get("status") or "exists")


def lims_client(config: LimsConfig | None) -> LimsClient | None:
    return LimsClient(config) if config else None
