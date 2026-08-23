# cutit

A URL shortener running on AWS Lambda, API Gateway, and DynamoDB.

Live at [cutit.app](https://cutit.app).

## What it does

Takes a long URL, returns a short one. Links expire on a TTL chosen at
creation time (1, 3, 7, or 30 days). Custom short codes are supported.

## Architecture

```
Browser ──> CloudFront ──> S3 (static frontend)
   │
   └──> API Gateway ──> Lambda ──> DynamoDB
                        ├─ create.py    POST /shorten
                        └─ redirect.py  GET  /r/{code}
```

Single region (us-east-1). CloudFront caches the static frontend at the
edge; the API itself is not edge-deployed, so dynamic requests travel to
the origin region.

DynamoDB holds one item per link, keyed on `code`, with `expiresAt` as the
TTL attribute.

## Design decisions

**Validation lives in the Lambda, not the frontend.** The API is public and
the client is fully editable by the user, so the server enforces the input
contract: hostname shape, URL length, and TTL values. The dropdown in the
UI offers four TTL options; the API accepts exactly those four.

**Expiry is enforced at read time, not just by DynamoDB's TTL sweep.**
DynamoDB deletes expired items in the background, and that sweep can lag
hours behind the timestamp. The redirect handler puts the expiry check in
the same conditional write that increments the hit counter, so an expired
link is treated as gone the moment it expires.

**Redirects are 302, not 301.** A 301 is cached indefinitely and cannot be
recalled. Since links expire and hits are counted, every request needs to
reach the origin.

**No outbound fetch of submitted URLs.** The service stores the URL string
and issues a redirect; it never requests the destination itself. The
browser makes that request from the user's own network, which keeps the
whole class of server-side request forgery off the table. Private and
loopback addresses are rejected at create time as scope enforcement — this
shortens links on the public web — rather than as a security control.

## Running the tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

ruff check .
pytest -q
```

121 tests, no AWS credentials or network access required — the DynamoDB
calls are stubbed. `conftest.py` sets the environment variables the Lambda
modules read at import time.

## CI

GitHub Actions runs ruff and pytest on every push and pull request.
See `.github/workflows/ci.yml`.

Deployment is currently manual.

## Layout

| File | |
|---|---|
| `create.py` | Lambda handler for `POST /shorten` |
| `redirect.py` | Lambda handler for `GET /r/{code}` |
| `index.html` | Static frontend |
| `test_create.py` | Tests for the create handler |
| `test_redirect.py` | Tests for the redirect handler |
| `conftest.py` | Test environment setup |
| `pyproject.toml` | Ruff configuration |
