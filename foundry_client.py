"""Minimal authenticated client for the Tyler Foundry API.

Reads credentials from the Foundry plugin's foundry.local.md (same source
foundry-api.sh uses) so there is exactly one place to rotate the key.
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

PLUGIN_CANDIDATES = [
    # First: outside OneDrive. ~/Library/CloudStorage is TCC-protected, so a scheduled
    # launchd job cannot read anything under it -- including the plugin's copy below.
    # This path is in the home root (mode 600), so unattended refreshes can authenticate.
    # Rotate the key here AND in the plugin copy, or refreshes start failing silently.
    os.path.expanduser("~/.config/foundry/foundry.local.md"),
    os.path.expanduser(
        "~/Library/CloudStorage/OneDrive-TylerTechnologies,Inc/Documents PC/"
        "AI_Busyness/tyler-ai/plugins/foundry/foundry.local.md"
    ),
    # NB: ~/Documents is a symlink into the CloudStorage path above (OneDrive Known
    # Folder Move), so this is the same file, not an independent fallback.
    os.path.expanduser("~/Documents/AI_Busyness/tyler-ai/plugins/foundry/foundry.local.md"),
]


# Foundry throttles aggressively (HTTP 429) once a fan-out gets wide. Waiting these
# out is always better than dropping the row, so they get their own generous budget.
RATE_LIMIT_RETRIES = 8


class FoundryError(RuntimeError):
    """API failure. `status` carries the HTTP code when there was one, so callers
    can tell throttling and 5xx apart from a genuinely missing resource."""

    status = None


def _retry_after(exc):
    """Seconds the server asked us to wait, if it said."""
    try:
        value = exc.headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return max(1.0, min(120.0, float(value)))
    except (TypeError, ValueError):
        return None


def _ssl_context():
    """Python's default trust store does not resolve on this machine (every request
    fails CERTIFICATE_VERIFY_FAILED while macOS curl succeeds), so pin an explicit
    CA bundle: certifi if installed, else the system bundle curl uses."""
    for cafile in (_certifi_path(), "/etc/ssl/cert.pem"):
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _certifi_path():
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


def _parse_local_md(path):
    cfg = {}
    with open(path) as fh:
        for line in fh:
            m = re.match(r"^-\s*(Base URL|API Key|Tenant Slug)\s*:\s*(.+?)\s*$", line, re.I)
            if m:
                cfg[m.group(1).lower()] = m.group(2).strip()
    return cfg


class FoundryClient:
    def __init__(self, base_url=None, api_key=None, tenant_slug=None):
        cfg = {}
        for path in PLUGIN_CANDIDATES:
            if os.path.exists(path):
                cfg = _parse_local_md(path)
                break

        self.base_url = (
            base_url or os.environ.get("FOUNDRY_BASE_URL") or cfg.get("base url", "")
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.environ.get("FOUNDRY_API_KEY")
            or os.environ.get("FOUNDRY_AUTH_TOKEN")
            or cfg.get("api key", "")
        )
        self.tenant_slug = (
            tenant_slug or os.environ.get("FOUNDRY_TENANT_SLUG") or cfg.get("tenant slug", "")
        )

        self.ssl_context = _ssl_context()

        if not self.base_url or not self.api_key:
            raise FoundryError(
                "Could not resolve Foundry base URL / API key. Checked env vars and:\n  "
                + "\n  ".join(PLUGIN_CANDIDATES)
            )

    def post(self, path, body, retries=4, timeout=300):
        return self._request("POST", path, body=body, retries=retries, timeout=timeout)

    def get(self, path, params=None, retries=4, timeout=180):
        return self._request("GET", path, params=params, retries=retries, timeout=timeout)

    def _request(self, method, path, params=None, body=None, retries=4, timeout=180):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.tenant_slug:
            headers["X-Tenant-Slug"] = self.tenant_slug

        data = json.dumps(body).encode("utf-8") if body is not None else None

        last = None
        attempt = 0
        throttle_waits = 0
        while True:
            try:
                req = urllib.request.Request(url, data=data, headers=headers,
                                             method=method)
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=self.ssl_context) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", "replace")[:400]
                last = FoundryError(f"HTTP {exc.code} on {path}: {err_body}")
                last.status = exc.code
                # Client errors other than rate-limiting will not fix themselves.
                if exc.code in (400, 401, 403, 404):
                    raise last
                if exc.code == 429:
                    # Throttling is the one failure worth waiting out properly. It
                    # does not consume the retry budget: a fan-out that gave up here
                    # would report throttled rows as missing data, which reads as
                    # "these conversations were deleted" rather than "slow down".
                    throttle_waits += 1
                    if throttle_waits > RATE_LIMIT_RETRIES:
                        raise last
                    # Deliberately gentle: Foundry's limiter clears in a couple of
                    # seconds, so a steep curve just parks every worker in a long
                    # sleep long after the throttle lifted -- measured as total
                    # throughput collapse (all workers asleep, zero open sockets)
                    # while a single serial caller sailed through.
                    time.sleep(_retry_after(exc)
                               or min(20, 1.5 * 2 ** min(throttle_waits - 1, 3)))
                    continue
            except Exception as exc:  # timeouts, connection resets, bad JSON
                last = FoundryError(f"{type(exc).__name__} on {path}: {exc}")
            attempt += 1
            if attempt >= retries:
                raise last
            time.sleep(2 ** attempt)

    def get_async_job(self, path, params=None, poll_seconds=3, max_polls=40):
        """Some analytics endpoints return {status:'computing', jobId} and are
        populated by re-requesting the same URL."""
        for _ in range(max_polls):
            payload = self.get(path, params)
            if isinstance(payload, dict) and payload.get("status") == "computing":
                time.sleep(poll_seconds)
                continue
            return payload
        raise FoundryError(f"{path} still computing after {max_polls} polls")

    def paginate(self, path, rows_key, params=None, page_size=1000, hard_cap=None):
        """Yield rows from a {<rows_key>: [...], total, limit, offset} endpoint."""
        offset = 0
        total = None
        while True:
            page_params = dict(params or {})
            page_params.update({"limit": page_size, "offset": offset})
            payload = self.get(path, page_params)

            rows = payload.get(rows_key) if isinstance(payload, dict) else payload
            if rows is None:
                rows = []
            if total is None:
                total = payload.get("total") if isinstance(payload, dict) else len(rows)

            for row in rows:
                yield row

            offset += len(rows)
            if not rows or (total is not None and offset >= total):
                break
            if hard_cap and offset >= hard_cap:
                break
