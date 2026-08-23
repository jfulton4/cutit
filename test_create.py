"""Tests for the create.py input contract.

These cover the boundary: what the API accepts and what it rejects. The
validators are pure functions, so most of this needs no AWS mocking at all.
Handler tests stub put_item to keep DynamoDB out of the picture.
"""
import json

import pytest

import create

# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("facebook.com", "https://facebook.com"),
    ("  example.com  ", "https://example.com"),
    ("https://example.com", "https://example.com"),
    ("http://example.com", "http://example.com"),
    ("HTTP://example.com", "HTTP://example.com"),   # scheme preserved, urlparse lowercases it
    ("", ""),
    (None, ""),
])
def test_normalize_url(raw, expected):
    assert create.normalize_url(raw) == expected


# ---------------------------------------------------------------------------
# valid_hostname
#
# Regression: before hostname validation existed, normalize_url would turn any
# bare token into "https://<token>", giving a non-empty netloc, and the only
# check was `if not parts.netloc`. So "asdf" and "hello world" were accepted.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "example.com",
    "sub.example.com",
    "a.b.c.example.com",
    "xn--80ak6aa92e.com",      # punycode IDN
    "8.8.8.8",                 # public IPv4 literal
])
def test_valid_hostname_accepts(host):
    assert create.valid_hostname(host) is True


@pytest.mark.parametrize("host", [
    "",
    "asdf",                    # no TLD
    "hello world",             # space
    "....",
    "-example.com",            # leading hyphen
    "example.c",               # TLD too short
    "example.123",             # numeric TLD
    "exam..ple.com",           # empty label
])
def test_valid_hostname_rejects(host):
    assert create.valid_hostname(host) is False


# ---------------------------------------------------------------------------
# valid_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://example.com/path?q=1#frag",
    "https://example.com:8443/x",
    "https://8.8.8.8",
])
def test_valid_url_accepts(url):
    assert create.valid_url(url) is True


@pytest.mark.parametrize("url", [
    "https://ftp://example.com",       # what normalize_url does to "ftp://..."
    "https://asdf",
    "https://hello world",
    "https://javascript:alert(1)",     # normalized form of a js: payload
    "https://data:text/html,x",
    "file:///etc/passwd",
    "https://192.168.1.1",             # RFC1918
    "https://10.0.0.1",
    "https://127.0.0.1",               # loopback
    "https://169.254.169.254",         # link-local (EC2 IMDS address)
    "https://[::1]/",                  # IPv6 loopback
    "https://[fe80::1]/",              # IPv6 link-local
])
def test_valid_url_rejects(url):
    assert create.valid_url(url) is False


# ---------------------------------------------------------------------------
# parse_ttl_hours
#
# Regression: the old put_item did float(raw) inside a bare try/except that
# swallowed failures, so a bad value produced an item with no expiresAt --
# a permanent link, silently, with a 201 response.
# ---------------------------------------------------------------------------

def test_ttl_omitted_uses_default():
    assert create.parse_ttl_hours(None) == (create.DEFAULT_TTL_HOURS, None)


@pytest.mark.parametrize("raw,expected", [
    (24, 24), (72, 72), (168, 168), (720, 720),
    ("24", 24), ("720", 720),        # form posts send strings
    (24.0, 24),                      # JSON numbers may deserialize as float
])
def test_ttl_accepts_dropdown_values(raw, expected):
    hours, err = create.parse_ttl_hours(raw)
    assert err is None
    assert hours == expected


@pytest.mark.parametrize("raw", [
    8760,           # old MAX_TTL_HOURS, no longer offered
    0, -24, 1, 25,
    24.5,           # must not silently truncate to 24
    "24h", "", "abc",
    True, False,    # int(True) == 1
    [], {}, [24],
])
def test_ttl_rejects_everything_else(raw):
    hours, err = create.parse_ttl_hours(raw)
    assert hours is None
    assert err


def test_ttl_allowlist_matches_documented_error():
    for value in create.ALLOWED_TTL_HOURS:
        assert str(value) in create.TTL_ERROR


# ---------------------------------------------------------------------------
# abuse_reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://paypal.com@evil.com",     # real host is evil.com
    "https://user:pw@evil.com",
    "https://localhost",
    "https://printer.local",
])
def test_abuse_reason_flags(url):
    assert create.abuse_reason(url) is not None


def test_abuse_reason_allows_normal_url():
    assert create.abuse_reason("https://example.com/a/b") is None


# ---------------------------------------------------------------------------
# gen_code
# ---------------------------------------------------------------------------

def test_gen_code_shape():
    codes = {create.gen_code() for _ in range(200)}
    assert len(codes) == 200, "collisions in 200 draws suggests a weak RNG"
    for c in codes:
        assert len(c) == create.CODE_LEN
        assert create.CODE_RE.match(c)


# ---------------------------------------------------------------------------
# cors_headers
# ---------------------------------------------------------------------------

def test_cors_echoes_allowed_origin():
    h = create.cors_headers({"headers": {"origin": "https://cutit.app"}})
    assert h["Access-Control-Allow-Origin"] == "https://cutit.app"


def test_cors_omits_disallowed_origin():
    h = create.cors_headers({"headers": {"origin": "https://evil.com"}})
    assert "Access-Control-Allow-Origin" not in h


# ---------------------------------------------------------------------------
# handler
#
# Regression: after the TTL refactor, both put_item call sites still referenced
# the deleted ttl_hours_raw. That NameError escaped the inner except (which
# only caught ConditionalCheckFailedException), hit the outer catch-all, and
# turned every create into a 500. These tests fail loudly on that.
# ---------------------------------------------------------------------------

@pytest.fixture
def captured(monkeypatch):
    """Replace put_item so no DynamoDB call happens; record its arguments."""
    calls = []
    monkeypatch.setattr(create, "put_item",
                        lambda code, url, ttl_hours: calls.append((code, url, ttl_hours)))
    return calls


def post(body):
    return {"httpMethod": "POST", "headers": {"origin": "https://cutit.app"},
            "body": json.dumps(body)}


def body_of(resp):
    return json.loads(resp["body"])


def test_handler_creates_link(captured):
    resp = create.handler(post({"url": "https://example.com", "ttlHours": 72}), None)
    assert resp["statusCode"] == 201
    assert body_of(resp)["shortUrl"].startswith("https://cutit.app/r/")
    assert len(captured) == 1
    code, url, ttl = captured[0]
    assert (url, ttl) == ("https://example.com", 72)


def test_handler_applies_default_ttl(captured):
    resp = create.handler(post({"url": "https://example.com"}), None)
    assert resp["statusCode"] == 201
    assert captured[0][2] == create.DEFAULT_TTL_HOURS


def test_handler_honors_custom_code(captured):
    resp = create.handler(post({"url": "https://example.com", "code": "my-link"}), None)
    assert resp["statusCode"] == 201
    assert body_of(resp)["code"] == "my-link"


@pytest.mark.parametrize("body,status", [
    ({"url": "https://example.com", "ttlHours": 8760}, 400),   # not in dropdown
    ({"url": "https://example.com", "ttlHours": "24h"}, 400),
    ({"url": "asdf"}, 400),                                    # not a hostname
    ({"url": ""}, 400),
    ({}, 400),
    ({"url": "https://192.168.1.1"}, 400),
    ({"url": "https://paypal.com@evil.com"}, 400),
    ({"url": "https://example.com", "code": "ab"}, 400),        # code too short
    ({"url": "https://example.com", "code": "bad code"}, 400),
    ({"url": "https://example.com", "code": "admin"}, 400),     # reserved
    ({"url": "https://example.com", "code": "ADMIN"}, 400),     # reserved, any case
    ({"url": "h" * 5000}, 400),                                 # over MAX_URL_LEN
])
def test_handler_rejects(body, status, captured):
    resp = create.handler(post(body), None)
    assert resp["statusCode"] == status
    assert "error" in body_of(resp)
    assert captured == [], "rejected request must not write to DynamoDB"

def test_error_messages_are_specific(captured):
    too_long = create.handler(post({"url": "https://example.com/" + "a" * 3000}), None)
    bad_host = create.handler(post({"url": "face"}), None)
    assert body_of(too_long)["error"] != body_of(bad_host)["error"]

def test_handler_preflight():
    resp = create.handler({"httpMethod": "OPTIONS", "headers": {}}, None)
    assert resp["statusCode"] == 204


def test_handler_malformed_json_does_not_leak(captured):
    resp = create.handler(
        {"httpMethod": "POST", "headers": {}, "body": "{not json"}, None)
    assert resp["statusCode"] == 500
    assert body_of(resp) == {"error": "Server error"}


def test_handler_reports_code_conflict(monkeypatch):
    exc = create.ddb.meta.client.exceptions.ConditionalCheckFailedException

    def always_taken(code, url, ttl_hours):
        raise exc({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")

    monkeypatch.setattr(create, "put_item", always_taken)
    resp = create.handler(post({"url": "https://example.com", "code": "taken"}), None)
    assert resp["statusCode"] == 409


def test_handler_gives_up_after_code_collisions(monkeypatch):
    exc = create.ddb.meta.client.exceptions.ConditionalCheckFailedException

    def always_taken(code, url, ttl_hours):
        raise exc({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")

    monkeypatch.setattr(create, "put_item", always_taken)
    resp = create.handler(post({"url": "https://example.com"}), None)
    assert resp["statusCode"] == 503
