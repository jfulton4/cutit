"""Tests for redirect.py.

The interesting behaviour here is the interaction with DynamoDB, so these
stub update_item/get_item rather than calling AWS. What is being tested is
the handler's decision-making: which responses it returns for a hit, a miss,
an expired link, and a failed write.

Note on types: DynamoDB returns numbers as decimal.Decimal, not int/float.
Several tests use Decimal deliberately -- passing plain ints would test a
situation that never occurs in production.
"""
from decimal import Decimal

import pytest

import redirect

# The real exception class, captured before ddb gets stubbed out.
CONDITION_FAILED = redirect.ddb.meta.client.exceptions.ConditionalCheckFailedException


def condition_failure():
    return CONDITION_FAILED(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


class FakeTable:
    """Stands in for the DynamoDB Table resource.

    Keeps the real .meta so the handler's `except ddb.meta.client.exceptions...`
    clause still refers to the same exception class.
    """

    def __init__(self, update=None, get=None):
        self._update = update
        self._get = get
        self.meta = redirect.ddb.meta
        self.deleted = []

    def update_item(self, **kwargs):
        if callable(self._update):
            return self._update(**kwargs)
        return self._update

    def get_item(self, **kwargs):
        if callable(self._get):
            return self._get(**kwargs)
        return self._get

    def delete_item(self, Key):
        self.deleted.append(Key)


@pytest.fixture
def table(monkeypatch):
    def install(update=None, get=None):
        t = FakeTable(update=update, get=get)
        monkeypatch.setattr(redirect, "ddb", t)
        return t
    return install


def path_event(code):
    return {"pathParameters": {"code": code}}


def query_event(code):
    return {"queryStringParameters": {"code": code}}


def location(resp):
    return resp["headers"]["Location"]


NOT_FOUND_URL = "https://cutit.app?error=notfound"


# ---------------------------------------------------------------------------
# _get_code
# ---------------------------------------------------------------------------

def test_get_code_from_path():
    assert redirect._get_code(path_event("abc123")) == "abc123"


def test_get_code_falls_back_to_query_string():
    assert redirect._get_code(query_event("abc123")) == "abc123"


def test_get_code_prefers_path_over_query():
    event = {"pathParameters": {"code": "frompath"},
             "queryStringParameters": {"code": "fromquery"}}
    assert redirect._get_code(event) == "frompath"


def test_get_code_strips_whitespace():
    assert redirect._get_code(path_event("  abc123  ")) == "abc123"


@pytest.mark.parametrize("event", [
    {},
    {"pathParameters": None},
    {"pathParameters": {}},
    {"pathParameters": None, "queryStringParameters": None},
    {"pathParameters": {"code": None}},
])
def test_get_code_missing_returns_empty(event):
    assert redirect._get_code(event) == ""


# ---------------------------------------------------------------------------
# Routing before DynamoDB is touched
# ---------------------------------------------------------------------------

def test_no_code_redirects_home(table):
    table()
    resp = redirect.handler({}, None)
    assert resp["statusCode"] == 302
    assert location(resp) == "https://cutit.app"


@pytest.mark.parametrize("code", [
    "ab",                    # too short
    "a" * 33,                # too long
    "has space",
    "bad/slash",
    "dots.not.allowed",
    "semi;colon",
])
def test_malformed_code_is_not_found_without_a_query(table, code):
    def explode(**kwargs):
        raise AssertionError("DynamoDB must not be queried for a malformed code")

    table(update=explode)
    resp = redirect.handler(path_event(code), None)
    assert location(resp) == NOT_FOUND_URL


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_valid_code_redirects(table):
    table(update={"Attributes": {
        "code": "abc123", "longUrl": "https://example.com", "hits": Decimal("5")}})
    resp = redirect.handler(path_event("abc123"), None)
    assert resp["statusCode"] == 302
    assert location(resp) == "https://example.com"


def test_redirect_is_302_not_301(table):
    """301s are cached indefinitely and cannot be recalled -- wrong for links
    that expire and for hit counting."""
    table(update={"Attributes": {"longUrl": "https://example.com"}})
    resp = redirect.handler(path_event("abc123"), None)
    assert resp["statusCode"] == 302


def test_hit_counter_is_incremented_atomically(table):
    """The increment, the existence check and the TTL check are one operation,
    so a link cannot be counted and then found to be expired."""
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return {"Attributes": {"longUrl": "https://example.com"}}

    table(update=capture)
    redirect.handler(path_event("abc123"), None)

    assert "ADD hits :inc" in seen["UpdateExpression"]
    assert seen["ExpressionAttributeValues"][":inc"] == 1
    cond = seen["ConditionExpression"]
    assert "attribute_exists(#c)" in cond
    assert "attribute_exists(#u)" in cond
    assert "#e > :now" in cond


def test_expired_items_are_rejected_by_the_condition(table):
    """The condition enforces TTL at read time rather than trusting DynamoDB's
    background sweep, which can lag well behind the expiry timestamp."""
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return {"Attributes": {"longUrl": "https://example.com"}}

    table(update=capture)
    redirect.handler(path_event("abc123"), None)

    now = seen["ExpressionAttributeValues"][":now"]
    assert isinstance(now, int)
    assert "attribute_not_exists(#e) OR #e > :now" in seen["ConditionExpression"]


# ---------------------------------------------------------------------------
# Misses and expiry
# ---------------------------------------------------------------------------

def test_missing_or_expired_item_is_not_found(table):
    """The condition covers both cases, so they are indistinguishable here --
    which is also what we want to expose to the caller."""
    def fail(**kwargs):
        raise condition_failure()

    table(update=fail)
    resp = redirect.handler(path_event("abc123"), None)
    assert location(resp) == NOT_FOUND_URL
    assert resp["headers"]["Cache-Control"] == "no-store"


def test_not_found_is_never_cached(table):
    def fail(**kwargs):
        raise condition_failure()

    table(update=fail)
    resp = redirect.handler(path_event("abc123"), None)
    assert resp["headers"]["Cache-Control"] == "no-store"


def test_item_without_long_url_is_not_found(table):
    table(update={"Attributes": {"code": "abc123", "hits": Decimal("1")}})
    resp = redirect.handler(path_event("abc123"), None)
    assert location(resp) == NOT_FOUND_URL


# ---------------------------------------------------------------------------
# The fallback path
#
# If update_item fails for a reason other than the condition (throttling, a
# permissions problem, a network blip) the handler falls back to a plain read.
# That read does NOT apply the TTL condition, so the expiry check after it is
# the only thing standing between an expired link and a working redirect.
# ---------------------------------------------------------------------------

def test_fallback_read_serves_a_live_link(table):
    def boom(**kwargs):
        raise RuntimeError("throttled")

    table(update=boom,
              get={"Item": {"longUrl": "https://example.com",
                            "expiresAt": Decimal(str(redirect._now_secs() + 3600))}})
    resp = redirect.handler(path_event("abc123"), None)
    assert location(resp) == "https://example.com"


def test_fallback_read_on_missing_item_is_not_found(table):
    def boom(**kwargs):
        raise RuntimeError("throttled")

    table(update=boom, get={})
    resp = redirect.handler(path_event("abc123"), None)
    assert location(resp) == NOT_FOUND_URL


def test_fallback_rejects_an_expired_link(table):
    """Regression: DynamoDB returns numbers as Decimal, so an isinstance check
    against (int, float) alone silently never fires and the expired link is
    served."""
    def boom(**kwargs):
        raise RuntimeError("throttled")

    expired = Decimal(str(redirect._now_secs() - 3600))
    table(update=boom, get={"Item": {"longUrl": "https://example.com",
                                         "expiresAt": expired}})
    resp = redirect.handler(path_event("abc123"), None)
    assert location(resp) == NOT_FOUND_URL, "expired link served on the fallback path"


def test_fallback_deletes_the_expired_item(table):
    def boom(**kwargs):
        raise RuntimeError("throttled")

    expired = Decimal(str(redirect._now_secs() - 3600))
    t = table(update=boom, get={"Item": {"longUrl": "https://example.com",
                                         "expiresAt": expired}})
    redirect.handler(path_event("abc123"), None)
    assert t.deleted == [{"code": "abc123"}]


def test_both_reads_failing_returns_500(table):
    def boom(**kwargs):
        raise RuntimeError("dynamo is down")

    table(update=boom, get=boom)
    resp = redirect.handler(path_event("abc123"), None)
    assert resp["statusCode"] == 500
    assert resp["headers"]["Cache-Control"] == "no-store"


def test_item_with_no_expiry_is_served(table):
    """Items created before TTL was mandatory have no expiresAt at all."""
    def boom(**kwargs):
        raise RuntimeError("throttled")

    table(update=boom, get={"Item": {"longUrl": "https://example.com"}})
    resp = redirect.handler(path_event("abc123"), None)
    assert location(resp) == "https://example.com"


def test_fallback_treats_an_unreadable_expiry_as_expired(table):
    """A value that cannot be read as a timestamp means we cannot prove the
    link is live. Fail closed rather than serving it, and never let the
    conversion escape the handler as a 502."""
    def boom(**kwargs):
        raise RuntimeError("throttled")

    t = table(update=boom, get={"Item": {"longUrl": "https://example.com",
                                         "expiresAt": "corrupt"}})
    resp = redirect.handler(path_event("abc123"), None)
    assert location(resp) == NOT_FOUND_URL
    assert t.deleted == [{"code": "abc123"}]


@pytest.mark.parametrize("exp", ["corrupt", {}, [], object()])
def test_unreadable_expiry_never_raises(table, exp):
    def boom(**kwargs):
        raise RuntimeError("throttled")

    table(update=boom, get={"Item": {"longUrl": "https://example.com",
                                     "expiresAt": exp}})
    resp = redirect.handler(path_event("abc123"), None)
    assert resp["statusCode"] == 302
    assert location(resp) == NOT_FOUND_URL
