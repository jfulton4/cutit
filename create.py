# create.py
import ipaddress
import json
import os
import random
import re
import string
import time
import urllib.parse

import boto3

TABLE = os.environ["TABLE_NAME"]
ddb = boto3.resource("dynamodb").Table(TABLE)

CODE_LEN = 7
CODE_ALPHABET = string.ascii_letters + string.digits
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"                     # total length
    r"(?!-)"                              # no leading hyphen
    r"[A-Za-z0-9-]{1,63}"                 # first label
    r"(\.[A-Za-z0-9-]{1,63})*"            # middle labels
    r"\.[A-Za-z]{2,63}$"                  # TLD must be alphabetic
)
ALLOWED_ORIGINS = {"https://cutit.app", "https://www.cutit.app", "https://api.cutit.app"}

MAX_URL_LEN = 2048
# urlparse silently drops tab/CR/LF before parsing, so a URL containing them
# would pass validation but be stored (and later sent as a Location header) raw.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# Mirrors the TTL dropdown in the UI. The client only offers these values,
# but the API is public and must not trust that.
ALLOWED_TTL_HOURS = {24, 72, 168, 720}  # 1d, 3d, 7d, 30d
DEFAULT_TTL_HOURS = 72
TTL_ERROR = "ttlHours must be one of 24, 72, 168, 720."
RANDOM = random.SystemRandom()  # better RNG for codes

RESERVED_CODES = {
    "shorten",
    "api",
    "assets",
    "asset",
    "static",
    "index",
    "home",
    "admin",
    "login",
    "signup",
    "contact",
    "report",
    "robots",
    "favicon",
}

BLOCKED_HOSTS = {
    "localhost",
}

def normalize_url(u: str) -> str:
    u = (u or "").strip()
    # Default schemeless input to https (e.g. "facebook.com" -> "https://facebook.com").
    # Note: this forces https, so http-only destinations will not resolve.
    if u and not re.match(r"^https?://", u, re.IGNORECASE):
        u = "https://" + u
    return u

def valid_hostname(host: str) -> bool:
    if not host or ".." in host or host.endswith("-"):
        return False
    if HOSTNAME_RE.match(host):
        return True
    # Allow bare IP literals (v4 and v6); private ranges handled separately.
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False

def valid_url(u: str) -> bool:
    try:
        parts = urllib.parse.urlparse(u)
        if parts.scheme not in ("http", "https"):
            return False
        host = parts.hostname or ""
        if not valid_hostname(host):
            return False
        # Rejects obviously local destinations to keep junk out of the table.
        try:
            ip = ipaddress.ip_address(host)  # covers IPv4 and IPv6 literals
        except ValueError:
            ip = None
        if ip and (ip.is_private or ip.is_link_local or ip.is_loopback):
            return False
        return True
    except Exception:
        return False

def parse_ttl_hours(raw):
    """Returns (hours, error). Omitted -> default; anything else must match
    a dropdown option exactly."""
    if raw is None:
        return DEFAULT_TTL_HOURS, None
    if isinstance(raw, bool):  # JSON true would otherwise int() to 1
        return None, TTL_ERROR
    if isinstance(raw, float) and not raw.is_integer():
        return None, TTL_ERROR  # int() would silently truncate 24.5 -> 24
    try:
        h = int(raw)
    except (TypeError, ValueError):
        return None, TTL_ERROR
    if h not in ALLOWED_TTL_HOURS:
        return None, TTL_ERROR
    return h, None

def abuse_reason(u: str):
    try:
        parts = urllib.parse.urlparse(u)
        host = (parts.hostname or "").lower()

        # Block credentials embedded in URL, e.g. https://paypal.com@evil.com
        # (the real host is evil.com — a classic phishing disguise).
        if parts.username or parts.password:
            return "URLs with embedded credentials are not allowed."

        # Block localhost-style destinations.
        if host in BLOCKED_HOSTS or host.endswith(".local"):
            return "Local/private destinations are not allowed."

        return None
    except Exception:
        return "Could not inspect URL."


def gen_code(n=CODE_LEN) -> str:
    return "".join(RANDOM.choice(CODE_ALPHABET) for _ in range(n))


def cors_headers(event):
    origin = None
    if isinstance(event, dict):
        h = event.get("headers") or {}
        origin = h.get("origin") or h.get("Origin")
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Methods": "OPTIONS,POST",
        "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token,Origin",
        "Vary": "Origin",
    }
    if origin in ALLOWED_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def handler(event, context):
    # Handle preflight cleanly
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "").upper()
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers(event), "body": ""}

    try:
        try:
            body = json.loads(event.get("body") or "{}")
        except ValueError:
            return respond(event, 400, {"error": "Request body must be valid JSON."})
        if not isinstance(body, dict):
            return respond(event, 400, {"error": "Request body must be a JSON object."})

        raw_url = body.get("url")
        requested = body.get("code")
        if raw_url is not None and not isinstance(raw_url, str):
            return respond(event, 400, {"error": "url must be a string."})
        if requested is not None and not isinstance(requested, str):
            return respond(event, 400, {"error": "code must be a string."})

        url = normalize_url(raw_url or "")

        if not url:
            return respond(event, 400, {"error": "Provide a URL."})
        if len(url) > MAX_URL_LEN:
            return respond(event, 400,
                           {"error": f"URL is too long. Use one shorter than {MAX_URL_LEN} characters."})
        if CONTROL_CHARS_RE.search(url):
            return respond(event, 400, {"error": "URL contains invalid characters."})
        if not valid_url(url):
            return respond(event, 400,
                           {"error": "Provide a valid http(s) URL, e.g. example.com/page."})

        reason = abuse_reason(url)
        if reason:
            return respond(event, 400, {"error": reason})

        ttl_hours, ttl_err = parse_ttl_hours(body.get("ttlHours"))
        if ttl_err:
            return respond(event, 400, {"error": ttl_err})

        if requested:
            requested = requested.strip()

            if not CODE_RE.match(requested):
                return respond(event, 400, {"error": "Invalid code format. Use 3-32 chars [A-Za-z0-9_-]."})

            if requested.lower() in RESERVED_CODES:
                return respond(event, 400, {"error": "That code is reserved. Try another."})

            code = requested
            # Single attempt for custom codes; if exists, 409 is appropriate
            try:
                put_item(code, url, ttl_hours)
            except ddb.meta.client.exceptions.ConditionalCheckFailedException:
                return respond(event, 409, {"error": "Code already exists. Try another."})
        else:
            # Auto-generate with a few retries on collision
            for _ in range(5):
                code = gen_code()
                try:
                    put_item(code, url, ttl_hours)
                    break
                except ddb.meta.client.exceptions.ConditionalCheckFailedException:
                    continue
            else:
                return respond(event, 503, {"error": "Please retry; code generation collision."})

        return respond(event, 201, {"code": code, "shortUrl": f"https://cutit.app/r/{code}"})

    except Exception as e:
        print("ERROR:", repr(e))
        return respond(event, 500, {"error": "Server error"})


def put_item(code: str, url: str, ttl_hours: int):
    now = time.time()
    item = {
        "code": code,
        "longUrl": url,
        "createdAt": int(now * 1000),
        "hits": 0,
        "expiresAt": int(now + ttl_hours * 3600),  # epoch seconds
    }
    # An expired item counts as free: the redirect handler already treats it
    # as gone, so its code can be reused before DynamoDB's TTL sweep runs.
    ddb.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(#c) OR #e <= :now",
        ExpressionAttributeNames={"#c": "code", "#e": "expiresAt"},
        ExpressionAttributeValues={":now": int(now)},
    )


def respond(event, status, body):
    return {"statusCode": status, "headers": cors_headers(event), "body": json.dumps(body)}
