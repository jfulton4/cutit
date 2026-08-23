import os
import re
import time

import boto3

TABLE = os.environ["TABLE_NAME"]
ddb = boto3.resource("dynamodb").Table(TABLE)

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def _now_secs():
    return int(time.time())


def _get_code(event):
    # Try /r/{code}
    code = (event.get("pathParameters") or {}).get("code")
    if not code:
        # Optional legacy fallback: /resolve?code=abc123
        q = event.get("queryStringParameters") or {}
        code = q.get("code")
    return (code or "").strip()


def _not_found():
    return {
        "statusCode": 302,
        "headers": {
            "Location": "https://cutit.app?error=notfound",
            "Cache-Control": "no-store"
        },
        "body": ""
    }


def _redirect(url, permanent=False):
    return {
        "statusCode": 301 if permanent else 302,
        "headers": {
            "Location": url,
            # Conservative while iterating; raise if/when codes are immutable.
            "Cache-Control": "no-cache"
        },
        "body": ""
    }


def handler(event, context):
    code = _get_code(event)

    # Empty or root → send home
    if not code:
        return _redirect("https://cutit.app", permanent=False)

    if not CODE_RE.match(code):
        return _not_found()

    try:
        # Atomically increment hits ONLY if the item exists AND is unexpired.
        # The (#e > :now) clause enforces TTL at read time, so an expired link
        # is treated as not-found immediately — without waiting for DynamoDB's
        # background TTL delete (which can lag hours behind expiry).
        resp = ddb.update_item(
            Key={"code": code},
            UpdateExpression="ADD hits :inc",
            ExpressionAttributeValues={
                ":inc": 1,
                ":now": _now_secs()
            },
            ConditionExpression=(
                "attribute_exists(#c) "
                "AND attribute_exists(#u) "
                "AND (attribute_not_exists(#e) OR #e > :now)"
            ),
            ExpressionAttributeNames={
                "#c": "code",
                "#u": "longUrl",
                "#e": "expiresAt"
            },
            ReturnValues="ALL_NEW"
        )
        item = resp.get("Attributes") or {}
    except ddb.meta.client.exceptions.ConditionalCheckFailedException:
        # Item is missing OR expired (the condition above covers both cases).
        return _not_found()
    except Exception as e:
        print("ERROR_UPDATE:", e)
        # Fall back to a non-mutating read if the update failed for another reason.
        try:
            g = ddb.get_item(Key={"code": code}).get("Item")
            if not g:
                return _not_found()
            item = g
        except Exception as e2:
            print("ERROR_GET:", e2)
            return {
                "statusCode": 500,
                "headers": {"Cache-Control": "no-store"},
                "body": "Server error"
            }

    # Belt-and-suspenders TTL check. The condition expression above already
    # rejects expired items on the normal path; this only catches the
    # get_item fallback path, where the condition was not applied.
    exp = item.get("expiresAt")
    if exp is not None:
        try:
            expired = float(exp) <= _now_secs()
        except (TypeError, ValueError):
            print("ERROR_BAD_EXPIRY:", repr(exp))
            expired = True
    else:
        expired = False

    if expired:
        try:
            ddb.delete_item(Key={"code": code})
        except Exception as e3:
            print("ERROR_DELETE:", e3)
        return _not_found()

    long_url = item.get("longUrl")
    if not long_url:
        return _not_found()

    # 302 while codes may change; switch to 301 if codes become immutable.
    return _redirect(long_url, permanent=False)
