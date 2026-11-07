"""POC: query Azure Databricks from an ECS Fargate task, authenticating with
Entra ID workload identity federation and a user-assigned managed identity.

Flow:
  1. Task role credentials -> sts:GetWebIdentityToken -> AWS-signed JWT
  2. AWS JWT used as client_assertion against Entra ID (client_id = managed identity)
     -> Entra ID access token for the Azure Databricks resource
  3. Token used as Bearer for the Databricks SQL Statement Execution API
     -> run a few hardcoded queries against samples.wanderbricks
"""

import base64
import json
import logging
import os
import sys
import time

import boto3
import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import ClientAssertionCredential
from botocore.exceptions import BotoCoreError, ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# Keep SDK noise down; our own logger tells the story.
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
log = logging.getLogger("fargate-azure-wif")

# Well-known application ID of the Azure Databricks resource (same in every tenant).
AZURE_DATABRICKS_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"

TENANT_ID = os.environ.get("AZURE_TENANT_ID")
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")
AZURE_SCOPE = os.environ.get("AZURE_SCOPE", AZURE_DATABRICKS_SCOPE)
AWS_TOKEN_AUDIENCE = os.environ.get("AWS_TOKEN_AUDIENCE", "api://AzureADTokenExchange")
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
KEEP_ALIVE_SECONDS = int(os.environ.get("KEEP_ALIVE_SECONDS", "0"))

if DATABRICKS_HOST and not DATABRICKS_HOST.startswith("https://"):
    DATABRICKS_HOST = "https://" + DATABRICKS_HOST.removeprefix("http://")

STATEMENT_POLL_SECONDS = 2
STATEMENT_TIMEOUT_SECONDS = 300  # covers a cold-starting (non-serverless) warehouse

QUERIES = [
    (
        "Who am I",
        "SELECT current_user() AS identity, current_catalog() AS catalog, current_timestamp() AS now",
    ),
    (
        "Bookings by status",
        """SELECT status, COUNT(*) AS bookings, ROUND(SUM(total_amount), 2) AS total_amount
           FROM samples.wanderbricks.bookings
           GROUP BY status
           ORDER BY bookings DESC""",
    ),
    (
        "Top clickstream events by device",
        """SELECT metadata.device AS device_type, event, COUNT(*) AS event_count
           FROM samples.wanderbricks.clickstream
           GROUP BY ALL
           ORDER BY event_count DESC
           LIMIT 10""",
    ),
    (
        "Most-reviewed properties",
        """SELECT p.title, COUNT(*) AS reviews, ROUND(AVG(r.rating), 2) AS avg_rating
           FROM samples.wanderbricks.properties p
           JOIN samples.wanderbricks.reviews r ON p.property_id = r.property_id
           WHERE r.is_deleted = false
           GROUP BY p.title
           ORDER BY reviews DESC
           LIMIT 5""",
    ),
]

sts = boto3.client("sts", region_name=AWS_REGION)


class StatementError(Exception):
    pass


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying it (for logging/debugging only)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def get_aws_web_identity_token() -> str:
    """Ask AWS STS to mint a JWT for this task's IAM role, scoped to Entra ID."""
    resp = sts.get_web_identity_token(
        Audience=[AWS_TOKEN_AUDIENCE],
        SigningAlgorithm="RS256",
        DurationSeconds=300,
    )
    return resp["WebIdentityToken"]


def databricks_call(session: requests.Session, method: str, path: str, **kwargs) -> dict:
    resp = session.request(method, f"{DATABRICKS_HOST}{path}", timeout=60, **kwargs)
    if resp.status_code in (401, 403):
        raise StatementError(
            f"HTTP {resp.status_code}: {resp.text[:300]} -- check that the managed identity is a "
            "service principal in the workspace, has the 'Databricks SQL access' entitlement "
            "and CAN USE on the warehouse"
        )
    if not resp.ok:
        raise StatementError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def run_statement(session: requests.Session, sql: str) -> tuple[list[str], list[list]]:
    """Run one statement on the SQL warehouse and return (columns, rows)."""
    body = databricks_call(session, "POST", "/api/2.0/sql/statements", json={
        "warehouse_id": DATABRICKS_WAREHOUSE_ID,
        "statement": sql,
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    })
    statement_id = body["statement_id"]
    deadline = time.monotonic() + STATEMENT_TIMEOUT_SECONDS

    # If the warehouse is starting or the query is slow, poll until it finishes.
    while body["status"]["state"] in ("PENDING", "RUNNING"):
        if time.monotonic() > deadline:
            databricks_call(session, "POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
            raise StatementError(f"timed out after {STATEMENT_TIMEOUT_SECONDS}s (warehouse stopped or starting?)")
        time.sleep(STATEMENT_POLL_SECONDS)
        body = databricks_call(session, "GET", f"/api/2.0/sql/statements/{statement_id}")

    status = body["status"]
    if status["state"] != "SUCCEEDED":
        error = status.get("error", {})
        raise StatementError(f"{status['state']}: {error.get('error_code', '')} {error.get('message', '')}".strip())

    columns = [c["name"] for c in body["manifest"]["schema"]["columns"]]
    rows = body.get("result", {}).get("data_array", [])
    return columns, rows


def format_table(columns: list[str], rows: list[list]) -> str:
    cells = [[("NULL" if v is None else str(v)) for v in row] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) if cells else len(c) for i, c in enumerate(columns)]
    line = lambda values: " | ".join(v.ljust(w) for v, w in zip(values, widths))
    return "\n".join([line(columns), "-+-".join("-" * w for w in widths), *(line(r) for r in cells)])


def main() -> int:
    missing = [name for name, value in (
        ("AZURE_TENANT_ID", TENANT_ID),
        ("AZURE_CLIENT_ID", CLIENT_ID),
        ("DATABRICKS_HOST", DATABRICKS_HOST),
        ("DATABRICKS_WAREHOUSE_ID", DATABRICKS_WAREHOUSE_ID),
    ) if not value]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        return 1

    # Step 1: get the AWS-issued token once up front so we can show its claims.
    try:
        log.info("AWS caller identity: %s", sts.get_caller_identity()["Arn"])
        aws_token = get_aws_web_identity_token()
    except (ClientError, BotoCoreError) as e:
        log.error("sts:GetWebIdentityToken failed: %s", e)
        log.error("Check: outbound federation enabled on the account, task role policy, audience condition")
        return 1

    claims = decode_jwt_claims(aws_token)
    log.info("AWS token claims -> iss=%s", claims.get("iss"))
    log.info("AWS token claims -> sub=%s", claims.get("sub"))
    log.info("AWS token claims -> aud=%s", claims.get("aud"))
    log.info("AWS token claims -> exp=%s", claims.get("exp"))

    # Step 2: exchange it at Entra ID. The callback is re-invoked whenever a
    # fresh assertion is needed, so we never reuse an expired AWS token.
    credential = ClientAssertionCredential(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        func=get_aws_web_identity_token,
    )
    try:
        azure_token = credential.get_token(AZURE_SCOPE)
    except ClientAuthenticationError as e:
        log.error("Entra ID token exchange failed: %s", e.message)
        log.error(
            "The managed identity's federated credential must match exactly: "
            "issuer=%s subject=%s audience=%s",
            claims.get("iss"), claims.get("sub"), AWS_TOKEN_AUDIENCE,
        )
        return 1

    azure_claims = decode_jwt_claims(azure_token.token)
    log.info(
        "Azure token acquired for scope %s (expires %s), aud=%s oid=%s appid=%s",
        AZURE_SCOPE,
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(azure_token.expires_on)),
        azure_claims.get("aud"),
        azure_claims.get("oid"),
        azure_claims.get("appid"),
    )

    # Step 3: run the sample queries on the Databricks SQL warehouse.
    log.info("Databricks workspace %s, warehouse %s", DATABRICKS_HOST, DATABRICKS_WAREHOUSE_ID)
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {azure_token.token}"

    succeeded = 0
    for title, sql in QUERIES:
        started = time.monotonic()
        try:
            columns, rows = run_statement(session, sql)
        except (StatementError, requests.RequestException) as e:
            log.error("Query '%s' failed: %s", title, e)
            continue
        succeeded += 1
        log.info(
            "Query '%s' -> %d rows in %.1fs\n%s",
            title, len(rows), time.monotonic() - started, format_table(columns, rows),
        )

    if succeeded != len(QUERIES):
        log.error("FAILED: %d/%d queries succeeded", succeeded, len(QUERIES))
        return 1

    log.info("SUCCESS: %d/%d queries ran on Databricks via workload identity federation", succeeded, len(QUERIES))
    return 0


if __name__ == "__main__":
    rc = main()
    if KEEP_ALIVE_SECONDS > 0:
        log.info("Sleeping %ss before exit (KEEP_ALIVE_SECONDS)", KEEP_ALIVE_SECONDS)
        time.sleep(KEEP_ALIVE_SECONDS)
    sys.exit(rc)
