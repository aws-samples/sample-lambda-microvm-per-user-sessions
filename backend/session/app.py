# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Session Lambda behind an HTTP API with a Cognito JWT authorizer.

POST   /api/session  (site host, Cognito JWT required)
                     -> find or launch the caller's own MicroVM and return a
                        one-time launch ticket, valid for 60 seconds.
DELETE /api/session  (site host, Cognito JWT required)
                     -> terminate the caller's MicroVM.
GET    /launch?t=... (IDE host, no JWT: the ticket is the credential)
                     -> redeem the ticket once, mint a short-lived MicroVM auth
                        token and set it, with the MicroVM endpoint, as HttpOnly
                        cookies on the IDE host.

The IDE runs on its own CloudFront hostname, so code inside a MicroVM can never
read the Cognito tokens of the start page (a different origin). The ticket
hands the session from the start page to the IDE host: it is random, single
use, short lived, and only minted for a caller whose JWT API Gateway already
validated. No browser JavaScript ever sees the MicroVM auth token.

The user's identity comes from the JWT claims that API Gateway already
validated. Each user maps to at most one MicroVM, tracked in DynamoDB. A
compare-and-swap on that item prevents two parallel requests from launching two
MicroVMs for the same user.
"""
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION = os.environ["AWS_REGION"]
_ddb = boto3.resource("dynamodb")
TABLE = _ddb.Table(os.environ["SESSIONS_TABLE"])
TICKETS = _ddb.Table(os.environ["TICKETS_TABLE"])
SITE_URL = os.environ["SITE_URL"]
IMAGE_ARN = os.environ["IMAGE_ARN"]
IMAGE_VERSION = os.environ["IMAGE_VERSION"]
EXECUTION_ROLE_ARN = os.environ["EXECUTION_ROLE_ARN"]
LOG_GROUP = os.environ.get("LOG_GROUP", "")
TOKEN_MINUTES = int(os.environ.get("TOKEN_MINUTES", "60"))
IDLE_SECONDS = int(os.environ.get("IDLE_SECONDS", "600"))
SUSPENDED_SECONDS = int(os.environ.get("SUSPENDED_SECONDS", "3600"))
MAX_LIFETIME_SECONDS = int(os.environ.get("MAX_LIFETIME_SECONDS", "14400"))

API_VERSION = "2025-09-09"
APP_PORT = 8080
# States in which an existing MicroVM can still serve this user. A SUSPENDED
# MicroVM resumes automatically on the next connection (autoResumeEnabled).
REUSABLE = {"PENDING", "RUNNING", "SUSPENDING", "SUSPENDED", "RESUMING"}
WAIT_SECONDS = 20  # stay well inside the 30s API Gateway integration timeout
TICKET_SECONDS = 60
TICKET_FORMAT = re.compile(r"^[A-Za-z0-9_-]{43}$")  # secrets.token_urlsafe(32)

_session = boto3.Session()


# ---- lambda-microvms control plane (SigV4-signed REST) ---------------------
class NotFound(Exception):
    pass


def _call(method, path, body=None):
    url = f"https://lambda.{REGION}.amazonaws.com/{API_VERSION}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    signed = AWSRequest(method=method, url=url, data=data or b"", headers=headers)
    SigV4Auth(_session.get_credentials().get_frozen_credentials(), "lambda", REGION).add_auth(signed)
    req = urllib.request.Request(url, data=data, headers=dict(signed.headers), method=method)
    try:
        # URL is always https://lambda.<region>.amazonaws.com/..., built above; never user input.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(path) from e
        raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')}") from e


def get_microvm(microvm_id):
    return _call("GET", f"/microvms/{microvm_id}")


def run_microvm():
    body = {
        "imageIdentifier": IMAGE_ARN,
        "imageVersion": IMAGE_VERSION,
        "executionRoleArn": EXECUTION_ROLE_ARN,
        "idlePolicy": {
            "maxIdleDurationSeconds": IDLE_SECONDS,
            "suspendedDurationSeconds": SUSPENDED_SECONDS,
            "autoResumeEnabled": True,
        },
        "maximumDurationInSeconds": MAX_LIFETIME_SECONDS,
    }
    if LOG_GROUP:
        body["logging"] = {"cloudWatch": {"logGroup": LOG_GROUP}}
    return _call("POST", "/microvms", body)


def terminate_microvm(microvm_id):
    try:
        _call("DELETE", f"/microvms/{microvm_id}")
    except NotFound:
        pass


def create_token(microvm_id):
    body = {"expirationInMinutes": TOKEN_MINUTES, "allowedPorts": [{"port": APP_PORT}]}
    return _call("POST", f"/microvms/{microvm_id}/auth-token", body)["authToken"]["X-aws-proxy-auth"]


# ---- session logic ---------------------------------------------------------
def _usable(microvm_id):
    """Return the MicroVM if it can still serve this user, else None."""
    try:
        vm = get_microvm(microvm_id)
    except NotFound:
        return None
    return vm if vm.get("state") in REUSABLE else None


def find_or_launch(user_id):
    item = TABLE.get_item(Key={"userId": user_id}).get("Item")
    if item:
        vm = _usable(item["microvmId"])
        if vm:
            return vm

    # Launch first, then claim the slot with a compare-and-swap against what we
    # read. If another request won the race, discard ours and use theirs.
    vm = run_microvm()
    condition = (Attr("microvmId").eq(item["microvmId"]) if item
                 else Attr("userId").not_exists())
    try:
        TABLE.put_item(
            Item={
                "userId": user_id,
                "microvmId": vm["microvmId"],
                "endpoint": vm["endpoint"],
                "createdAt": int(time.time()),
                # DynamoDB TTL cleans up rows after the MicroVM's hard lifetime.
                "expiresAt": int(time.time()) + MAX_LIFETIME_SECONDS,
            },
            ConditionExpression=condition,
        )
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        terminate_microvm(vm["microvmId"])
        winner = TABLE.get_item(Key={"userId": user_id}, ConsistentRead=True)["Item"]
        return get_microvm(winner["microvmId"])
    log.info("launched %s for %s", vm["microvmId"], user_id)
    return vm


def wait_until_ready(vm):
    deadline = time.time() + WAIT_SECONDS
    while vm.get("state") == "PENDING" and time.time() < deadline:
        time.sleep(1)
        vm = get_microvm(vm["microvmId"])
    return vm


def _cookie(name, value, max_age):
    # Set on the IDE host only (no Domain attribute). Path=/ide: only IDE
    # requests carry these, and CloudFront strips them before forwarding.
    # HttpOnly keeps them away from page JavaScript.
    return f"{name}={value}; Path=/ide; Max-Age={max_age}; Secure; HttpOnly; SameSite=Strict"


def _response(status, body, cookies=None):
    response = {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }
    if cookies:
        response["cookies"] = cookies
    return response


def _page(status, html, cookies=None):
    # The ticket is in this page's URL, so never send it on as a Referer.
    response = {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        },
        "body": html,
    }
    if cookies:
        response["cookies"] = cookies
    return response


def issue_ticket(user_id, vm):
    ticket = secrets.token_urlsafe(32)
    TICKETS.put_item(Item={
        "ticket": ticket,
        "userId": user_id,
        "microvmId": vm["microvmId"],
        "endpoint": vm["endpoint"],
        # Checked on redeem; DynamoDB TTL only removes expired items later.
        "expiresAt": int(time.time()) + TICKET_SECONDS,
    })
    return ticket


def redeem_ticket(ticket):
    """Consume a ticket exactly once. Returns its item, or None if unusable."""
    if not ticket or not TICKET_FORMAT.match(ticket):
        return None
    try:
        item = TICKETS.delete_item(
            Key={"ticket": ticket},
            ConditionExpression=Attr("ticket").exists(),
            ReturnValues="ALL_OLD",
        ).get("Attributes")
    except TICKETS.meta.client.exceptions.ConditionalCheckFailedException:
        return None
    if not item or int(item["expiresAt"]) < time.time():
        return None
    # The MicroVM must still be this user's current one (not terminated since).
    current = TABLE.get_item(Key={"userId": item["userId"]}, ConsistentRead=True).get("Item")
    if not current or current["microvmId"] != item["microvmId"]:
        return None
    return item


def launch(event):
    ticket = (event.get("queryStringParameters") or {}).get("t")
    item = redeem_ticket(ticket)
    if not item:
        return _page(403, f'<!doctype html><title>Launch link not valid</title>'
                          f'<p>This launch link is expired or was already used. '
                          f'<a href="{SITE_URL}/">Back to the start page</a>.</p>')
    lifetime = TOKEN_MINUTES * 60
    token = create_token(item["microvmId"])
    # Continue with a same-site navigation so the SameSite=Strict cookies are
    # sent: a redirect would still count as part of the cross-site navigation.
    return _page(
        200,
        '<!doctype html><title>Opening VS Code</title>'
        '<meta http-equiv="refresh" content="0;url=/ide/">'
        '<p>Opening VS Code… <a href="/ide/">continue</a></p>',
        cookies=[_cookie("mvm_ep", item["endpoint"], lifetime), _cookie("mvm_tok", token, lifetime)],
    )


def lambda_handler(event, context):
    if event.get("routeKey") == "GET /launch":
        return launch(event)

    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    user_id = claims["sub"]
    method = event["requestContext"]["http"]["method"]

    if method == "DELETE":
        item = TABLE.get_item(Key={"userId": user_id}).get("Item")
        if item:
            terminate_microvm(item["microvmId"])
            TABLE.delete_item(Key={"userId": user_id})
        return _response(200, {"terminated": bool(item)})

    vm = wait_until_ready(find_or_launch(user_id))
    state = vm.get("state")
    if state == "PENDING":
        # Still booting from snapshot; the browser retries shortly.
        return _response(202, {"state": state, "retryAfterSeconds": 2})
    if state not in REUSABLE:
        return _response(503, {"state": state, "reason": vm.get("stateReason")})

    return _response(200, {"microvmId": vm["microvmId"], "state": state,
                           "ticket": issue_ticket(user_id, vm)})
