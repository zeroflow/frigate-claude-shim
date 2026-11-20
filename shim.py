#!/usr/bin/env python3
"""Tiny proxy between Frigate's OpenAI GenAI provider and Anthropic's OpenAI-compatible API.

Frigate sends a strict `json_schema` response_format with constraints Anthropic's strict mode
rejects (minItems > 1, maxItems, min/maxLength, minimum/maximum, ...). This shim moves those
constraints into the field description (so the model still sees them), adds the
`anthropic-version` header (makes /v1/models work) and forwards everything else unchanged.
For Frigate review-description prompts it also adds the active Frigate profile, today's date and
sunrise/sunset under "## Sequence Details" (Frigate's prompt only has weekday + time).
Frigate soft-validates the answer, so a response slightly outside the limits is still kept.

Stdlib only. The API key passes through in the Authorization header and is never logged.
"""
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

UPSTREAM = os.environ.get("UPSTREAM", "https://api.anthropic.com").rstrip("/")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "180"))
# review prompt enrichment: active profile from Frigate, sunrise/sunset for the site
FRIGATE_URL = os.environ.get("FRIGATE_URL", "").rstrip("/")  # e.g. http://frigate:5000 (internal port)
LAT = float(os.environ["LAT"]) if os.environ.get("LAT") else None
LON = float(os.environ["LON"]) if os.environ.get("LON") else None
FRIGATE_TZ = os.environ.get("FRIGATE_TZ", "UTC")  # zone Frigate formats prompt timestamps in
LOCAL_TZ = os.environ.get("LOCAL_TZ") or os.environ.get("TZ") or "UTC"  # IANA zone of the property

# keywords Anthropic strict structured outputs does not accept
UNSUPPORTED = (
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "pattern", "format",
    "minProperties", "maxProperties", "uniqueItems",
)
PASS_HEADERS = ("authorization", "content-type", "accept", "anthropic-beta", "x-api-key")
# only what Frigate's OpenAI provider calls; everything else -> 404 (no open proxy)
ALLOWED = {("POST", "/v1/chat/completions"), ("GET", "/v1/models")}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx comes back to the caller as an HTTPError, never followed


OPENER = urllib.request.build_opener(_NoRedirect)


def upstream_url(method: str, path: str) -> str | None:
    """Upstream URL for an allowed request, else None. Query strings are kept, the host is pinned."""
    route = path.split("?", 1)[0]
    if (method, route) not in ALLOWED:
        return None
    url = UPSTREAM + path
    if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(UPSTREAM).netloc:
        return None
    return url


def relax_schema(node):
    """Return a copy of a JSON schema with unsupported constraints moved into `description`.

    minItems 0/1 is supported and kept.
    """
    if isinstance(node, list):
        return [relax_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out, notes = {}, []
    for key, value in node.items():
        if key in UNSUPPORTED and not (key == "minItems" and value in (0, 1)):
            notes.append(f"{key}={value}")
        elif key in ("properties", "$defs", "definitions"):
            out[key] = {k: relax_schema(v) for k, v in value.items()} if isinstance(value, dict) else value
        else:
            out[key] = relax_schema(value)
    if notes:
        hint = "Constraints: " + ", ".join(notes) + "."
        # strict decoding accepts "" / [] once the minimum is gone - say it in words (seen: empty `scene`)
        if isinstance(node.get("minLength"), int) and node["minLength"] > 0:
            hint += f" REQUIRED: never empty, write at least {node['minLength']} characters."
        if isinstance(node.get("minItems"), int) and node["minItems"] > 1:
            hint += f" REQUIRED: at least {node['minItems']} items."
        out["description"] = f'{out["description"]} {hint}' if out.get("description") else hint
    return out


def sun_times(day: datetime.date, lat: float, lon: float) -> tuple[float, float] | None:
    """Sunrise and sunset (unix timestamps) for `day` - sunrise equation, accurate to ~1-2 min.

    None in polar day/night (not relevant for Austria, but keeps the math total).
    """
    rad, deg = math.radians, math.degrees
    jd_noon = day.toordinal() + 1721425.0  # Julian day at 12:00 UTC of `day`
    n = round(jd_noon - 2451545.0 + 0.0008)
    j_star = n - lon / 360.0
    m = (357.5291 + 0.98560028 * j_star) % 360
    c = 1.9148 * math.sin(rad(m)) + 0.02 * math.sin(rad(2 * m)) + 0.0003 * math.sin(rad(3 * m))
    lam = (m + c + 180 + 102.9372) % 360
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(rad(m)) - 0.0069 * math.sin(rad(2 * lam))
    sin_d = math.sin(rad(lam)) * math.sin(rad(23.4397))
    cos_d = math.cos(math.asin(sin_d))
    cos_w = (math.sin(rad(-0.833)) - math.sin(rad(lat)) * sin_d) / (math.cos(rad(lat)) * cos_d)
    if not -1 <= cos_w <= 1:
        return None
    w = deg(math.acos(cos_w))

    def unix(jd):
        return (jd - 2440587.5) * 86400

    return unix(j_transit - w / 360), unix(j_transit + w / 360)


def active_profile() -> str:
    """Active Frigate profile name (+ friendly name), 'unknown' if Frigate does not answer."""
    if not FRIGATE_URL:
        return "unknown"
    try:
        with OPENER.open(FRIGATE_URL + "/api/profiles", timeout=3) as resp:
            doc = json.loads(resp.read())
        name = doc.get("active_profile")
        friendly = {p.get("name"): p.get("friendly_name") for p in doc.get("profiles", [])}.get(name)
        if not name:
            return "none"
        return f"{name} ({friendly})" if friendly and friendly != name else name
    except Exception:
        return "unknown"


def context_lines(now: float, profile: str) -> str:
    """Extra 'Sequence Details' lines: active profile, clock zones, today's date, sunrise/sunset.

    Frigate formats "Activity started at" in its own zone (FRIGATE_TZ, UTC unless TZ is set for Frigate);
    every time here is given in that zone and in LOCAL_TZ so the model never mixes them.
    """
    fz, lz = ZoneInfo(FRIGATE_TZ), ZoneInfo(LOCAL_TZ)

    def both(ts):
        f, loc = datetime.datetime.fromtimestamp(ts, fz), datetime.datetime.fromtimestamp(ts, lz)
        return f"{f:%H:%M} {FRIGATE_TZ} ({f:%I:%M %p}) = {loc:%H:%M} local"

    loc_now = datetime.datetime.fromtimestamp(now, lz)
    offset = loc_now.strftime("%z")
    lines = [
        f"- Active Frigate profile: {profile}",
        f'- Clock: "Activity started at" is in {FRIGATE_TZ}. Local time ({LOCAL_TZ}) is '
        f"UTC{offset[:3]}:{offset[3:]} today. Times in the property description are local time.",
        f"- Today: {loc_now:%A %Y-%m-%d}, now {both(now)}",
    ]
    if LAT is not None and LON is not None:
        sun = sun_times(loc_now.date(), LAT, LON)
        if sun:
            lines.append(f"- Sunrise today {both(sun[0])}; sunset today {both(sun[1])}")
    return "\n".join(lines)


def enrich_messages(body: dict, extra: str) -> bool:
    """Insert `extra` after the '## Sequence Details' heading of Frigate's review prompt."""
    marker = "## Sequence Details\n"
    for msg in body.get("messages") or []:
        parts = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(parts, str) and marker in parts:
            msg["content"] = parts.replace(marker, marker + "\n" + extra + "\n", 1)
            return True
        for part in parts if isinstance(parts, list) else []:
            if isinstance(part, dict) and part.get("type") == "text" and marker in part.get("text", ""):
                part["text"] = part["text"].replace(marker, marker + "\n" + extra + "\n", 1)
                return True
    return False


def nonempty_fields(raw: bytes) -> list[str]:
    """Top-level string properties the *original* response schema requires to be non-empty."""
    try:
        schema = json.loads(raw)["response_format"]["json_schema"]["schema"]
        return [k for k, v in schema.get("properties", {}).items()
                if isinstance(v, dict) and v.get("type") == "string" and (v.get("minLength") or 0) > 0]
    except (ValueError, KeyError, TypeError, AttributeError):
        return []


def empty_fields(payload: bytes, fields: list[str]) -> list[str]:
    """Which of `fields` came back empty in a chat.completions JSON answer (unparseable -> [])."""
    try:
        content = json.loads(json.loads(payload)["choices"][0]["message"]["content"])
        return [f for f in fields if isinstance(content.get(f), str) and not content[f].strip()]
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return []


def rewrite_body(raw: bytes, extra=None) -> bytes:
    """Relax the response_format schema of a chat.completions body; for Frigate review prompts
    also add the context lines (`extra()` is called only then). Other bodies pass unchanged."""
    if not raw:
        return raw
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(body, dict):
        return raw
    changed = False
    rf = body.get("response_format")
    if isinstance(rf, dict) and isinstance(rf.get("json_schema"), dict) and "schema" in rf["json_schema"]:
        rf["json_schema"]["schema"] = relax_schema(rf["json_schema"]["schema"])
        changed = True
    if extra is not None and "## Sequence Details" in raw.decode("utf-8", "replace"):
        changed = enrich_messages(body, extra()) or changed
    return json.dumps(body).encode() if changed else raw


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self):
        start = time.monotonic()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        url = upstream_url(self.command, self.path)
        if url is None:
            payload = json.dumps({"error": {"type": "shim_not_allowed", "message": "path not allowed"}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self._log(404, payload, time.monotonic() - start)
            return
        data = rewrite_body(raw, lambda: context_lines(time.time(), active_profile())) if raw else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in PASS_HEADERS}
        headers["anthropic-version"] = ANTHROPIC_VERSION
        status, payload, ctype = self._upstream(url, data, headers)
        required = nonempty_fields(raw) if status == 200 else []
        empty = empty_fields(payload, required)
        if empty:  # once: strict decoding without minLength sometimes returns "" for e.g. `scene`
            print(f"retry: empty {empty}", flush=True)
            status, payload, ctype = self._upstream(url, data, headers)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self._log(status, payload, time.monotonic() - start)

    def _upstream(self, url, data, headers):
        req = urllib.request.Request(url, data=data, headers=headers, method=self.command)
        try:
            with OPENER.open(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")
        except Exception as e:  # network error / timeout
            return 502, json.dumps({"error": {"type": "shim_upstream_error", "message": str(e)}}).encode(), "application/json"

    def _log(self, status, payload, secs):
        usage = ""
        try:
            doc = json.loads(payload)
            if isinstance(doc.get("usage"), dict):
                u = doc["usage"]
                usage = f' in={u.get("prompt_tokens")} out={u.get("completion_tokens")}'
            if status >= 400:
                usage += f' error={json.dumps(doc.get("error"))[:300]}'
        except ValueError:
            pass
        print(f"{self.command} {self.path[:120]!r} {status} {secs:.1f}s{usage}", flush=True)

    do_GET = do_POST = _forward

    def log_message(self, format, *args):  # noqa: A002 - silence default access log (own line in _log)
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"claude-shim on :{port} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
