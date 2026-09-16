import os
import time
import json
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import base64
import socket
import logging

import boto3


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# Existing Terraform/environment variable name
HOSP_BASE_URL = os.environ["HOSP_BACKEND_URL"].rstrip("/")

CACHE_TABLE_NAME = os.environ["CACHE_TABLE_NAME"]

# Simple TTL for now.
# Keep this boring until the proxy is stable.
CACHE_TTL_SECONDS = int(
    os.environ.get("CACHE_TTL_SECONDS", "900")
)

# Disabled by default.
# Only enable if the team has evidence that retrying failed writes is safe.
RETRY_WRITES_ON_5XX = (
    os.environ.get("RETRY_WRITES_ON_5XX", "false").lower() == "true"
)

# Original attempt + one retry
MAX_ATTEMPTS = 2

# HOSP has already been observed taking several seconds.
UPSTREAM_TIMEOUT_SECONDS = 25

RETRY_DELAY_SECONDS = 0.2

SHARED_ENDPOINTS = {"/hospitals", "/staffs"}


# Create DynamoDB resource outside handler so warm Lambda invocations
# can reuse the connection.
dynamodb = boto3.resource("dynamodb")
cache_table = dynamodb.Table(CACHE_TABLE_NAME)


# ---------------------------------------------------------------------------
# MAIN HANDLER
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """
    Main entry point for requests from the Application Load Balancer.

    Behaviour:
    GET:
        - check DynamoDB
        - return cache HIT if available
        - otherwise call HOSP
        - retry transient GET failures once
        - cache successful 200 response

    POST / PUT / PATCH / DELETE:
        - forward directly to HOSP
        - optionally retry 500/503 if explicitly enabled
        - invalidate related GET cache entries after success
    """

    method = event.get("httpMethod", "GET").upper()
    path = event.get("path", "/")
    query_params = event.get("queryStringParameters") or {}
    incoming_headers = event.get("headers") or {}
    body = event.get("body")

    # -----------------------------------------------------------------------
    # AUTHENTICATION
    # -----------------------------------------------------------------------

    authorization = (
        incoming_headers.get("authorization")
        or incoming_headers.get("Authorization")
    )

    if not authorization:
        return build_response(
            status_code=401,
            body=json.dumps({
                "error": "Authentication required"
            }),
            cache_status="BYPASS"
        )

    # -----------------------------------------------------------------------
    # GET: CHECK CACHE
    # -----------------------------------------------------------------------

    cache_key = None
    resource_group = None

    if method == "GET":

        cache_key, resource_group = build_cache_key(
            authorization=authorization,
            path=path,
            query_params=query_params
        )

        cached_body = get_from_cache(cache_key)

        if cached_body is not None:

            logger.info(
                "CACHE HIT method=%s path=%s key=%s",
                method,
                path,
                cache_key
            )

            return build_response(
                status_code=200,
                body=cached_body,
                cache_status="HIT"
            )

        logger.info(
            "CACHE MISS method=%s path=%s key=%s",
            method,
            path,
            cache_key
        )

    # -----------------------------------------------------------------------
    # CALL HOSP
    # -----------------------------------------------------------------------

    response_body, status_code = fetch_from_hosp(
        method=method,
        path=path,
        query_params=query_params,
        incoming_headers=incoming_headers,
        authorization=authorization,
        body=body,
        is_base64_encoded=event.get("isBase64Encoded", False)
    )

    # -----------------------------------------------------------------------
    # CACHE SUCCESSFUL GET RESPONSES
    # -----------------------------------------------------------------------

    if method == "GET" and status_code == 200:

        save_to_cache(
            key=cache_key,
            resource_group=resource_group,
            body=response_body,
            ttl_seconds=CACHE_TTL_SECONDS
        )

    # -----------------------------------------------------------------------
    # INVALIDATE CACHE AFTER SUCCESSFUL WRITE
    # -----------------------------------------------------------------------

    if (
        method in {"POST", "PUT", "PATCH", "DELETE"}
        and status_code in {200, 201, 204}
    ):
        invalidate_related_cache(path)

    return build_response(
        status_code=status_code,
        body=response_body,
        cache_status="MISS" if method == "GET" else "BYPASS"
    )


# ---------------------------------------------------------------------------
# CACHE KEY
# ---------------------------------------------------------------------------

def build_cache_key(authorization, path, query_params):
    """
    Creates a deterministic cache key.

    Cache is scoped to:
    - authenticated caller
    - exact path
    - query parameters

    The Authorization value itself is never stored.
    It is hashed first.

    Example conceptual resource:

        /notes?patient_id=3

    Example cache key:

        <caller-hash>:/notes?patient_id=3
    """

    normalized_path = path.rstrip("/") or "/"

    caller_hash = hashlib.sha256(
        authorization.encode("utf-8")
    ).hexdigest()

    query_string = urllib.parse.urlencode(
        sorted(query_params.items())
    )

    resource = normalized_path

    if query_string:
        resource = f"{normalized_path}?{query_string}"

    # Used by ResourceIndex so all variants of the same collection
    # can be invalidated together.
    resource_group = get_resource_group(normalized_path)

    # Check if the request is a non-secure endpoint like hospitals or staffs,
    # then strip 'caller hash' if it is - this is only needed for sensitive endpoints
    # like patients and notes. Should improve caching functionality.
    if resource_group in SHARED_ENDPOINTS:
        cache_key = f"global:{resource}"

    else:
        cache_key = f"{caller_hash}:{resource}"

    return cache_key, resource_group


def get_resource_group(path):
    """
    Maps a path to its top-level API resource.

    Examples:

        /notes             -> /notes
        /notes/12          -> /notes
        /patients/3        -> /patients
        /hospitals/4       -> /hospitals
    """

    parts = path.strip("/").split("/")

    if not parts or not parts[0]:
        return "/"

    return f"/{parts[0]}"


# ---------------------------------------------------------------------------
# CACHE READ
# ---------------------------------------------------------------------------

def get_from_cache(key):
    """
    Returns cached response body if present and not expired.

    DynamoDB TTL deletion is asynchronous, so expiry is also checked
    manually before returning an item.
    """

    try:

        response = cache_table.get_item(
            Key={
                "cache_key": key
            }
        )

        item = response.get("Item")

        if not item:
            return None

        expires_at = int(item["expires_at"])
        now = int(time.time())

        if expires_at <= now:

            logger.info(
                "CACHE EXPIRED key=%s",
                key
            )

            return None

        return item["data"]

    except Exception as error:

        # Cache failure must not make HOSP unavailable.
        logger.error(
            "CACHE READ ERROR type=%s message=%s",
            type(error).__name__,
            str(error)
        )

        return None


# ---------------------------------------------------------------------------
# CACHE WRITE
# ---------------------------------------------------------------------------

def save_to_cache(
    key,
    resource_group,
    body,
    ttl_seconds
):
    """
    Stores a successful GET response in DynamoDB.
    """

    try:

        expires_at = int(time.time()) + ttl_seconds

        cache_table.put_item(
            Item={
                "cache_key": key,
                "resource_path": resource_group,
                "data": body,
                "expires_at": expires_at
            }
        )

        logger.info(
            "CACHE STORED key=%s resource=%s ttl=%ss",
            key,
            resource_group,
            ttl_seconds
        )

    except Exception as error:

        # The user already has a valid HOSP response.
        # Do not fail it because caching failed.
        logger.error(
            "CACHE WRITE ERROR type=%s message=%s",
            type(error).__name__,
            str(error)
        )


# ---------------------------------------------------------------------------
# CACHE INVALIDATION
# ---------------------------------------------------------------------------

def invalidate_related_cache(path):
    """
    Invalidates cached GET responses belonging to the resource changed
    by a successful write.

    Example:

        POST /notes

    invalidates cached variants such as:

        GET /notes
        GET /notes?patient_id=3
        GET /notes/12

    This expects the DynamoDB GSI:

        ResourceIndex

    with partition key:

        resource_path
    """

    resource_group = get_resource_group(path)

    try:

        response = cache_table.query(
            IndexName="ResourceIndex",
            KeyConditionExpression="resource_path = :path",
            ExpressionAttributeValues={
                ":path": resource_group
            },
            ProjectionExpression="cache_key"
        )

        deleted = 0

        for item in response.get("Items", []):

            cache_table.delete_item(
                Key={
                    "cache_key": item["cache_key"]
                }
            )

            deleted += 1

        logger.info(
            "CACHE INVALIDATED resource=%s deleted=%s",
            resource_group,
            deleted
        )

    except Exception as error:

        # A failed invalidation should be visible,
        # but should not turn the successful write into an error.
        logger.error(
            "CACHE INVALIDATION ERROR type=%s message=%s",
            type(error).__name__,
            str(error)
        )


# ---------------------------------------------------------------------------
# HOSP REQUEST
# ---------------------------------------------------------------------------

def fetch_from_hosp(
    method,
    path,
    query_params,
    incoming_headers,
    authorization,
    body=None,
    is_base64_encoded=False
):
    """
    Sends the request to the legacy HOSP backend.

    Retry policy:

    GET
        Retry once on:
        - 500
        - 503
        - connection failure
        - timeout

    POST / PUT / PATCH / DELETE
        No retry by default.

        If RETRY_WRITES_ON_5XX=true:
        - retry 500 / 503 once

        Writes are NOT retried after ambiguous network failures/timeouts.
    """

    url = f"{HOSP_BASE_URL}{path}"

    if query_params:

        query_string = urllib.parse.urlencode(
            query_params
        )

        url += f"?{query_string}"

    # -----------------------------------------------------------------------
    # HEADERS
    # -----------------------------------------------------------------------

    outgoing_headers = {
        "Authorization": authorization,
        "Accept": "application/json"
    }

    content_type = (
        incoming_headers.get("content-type")
        or incoming_headers.get("Content-Type")
    )

    if content_type:
        outgoing_headers["Content-Type"] = content_type

    # -----------------------------------------------------------------------
    # BODY
    # -----------------------------------------------------------------------

    request_body = None

    if body is not None:

        if is_base64_encoded:
            request_body = base64.b64decode(body)
        else:
            request_body = body.encode("utf-8")

    # -----------------------------------------------------------------------
    # REQUEST ATTEMPTS
    # -----------------------------------------------------------------------

    for attempt in range(1, MAX_ATTEMPTS + 1):

        attempts_remaining = attempt < MAX_ATTEMPTS

        request = urllib.request.Request(
            url=url,
            data=request_body,
            headers=outgoing_headers,
            method=method
        )

        try:

            logger.info(
                "HOSP REQUEST method=%s path=%s attempt=%s",
                method,
                path,
                attempt
            )

            with urllib.request.urlopen(
                request,
                timeout=UPSTREAM_TIMEOUT_SECONDS
            ) as response:

                response_body = (
                    response.read().decode("utf-8")
                )

                logger.info(
                    "HOSP RESPONSE method=%s path=%s status=%s attempt=%s",
                    method,
                    path,
                    response.status,
                    attempt
                )

                return response_body, response.status

        # -------------------------------------------------------------------
        # HTTP ERROR
        # -------------------------------------------------------------------

        except urllib.error.HTTPError as error:

            status_code = error.code
            error_body = error.read().decode(
                "utf-8",
                errors="replace"
            )

            logger.warning(
                "HOSP HTTP ERROR method=%s path=%s status=%s attempt=%s",
                method,
                path,
                status_code,
                attempt
            )

            transient_error = (
                status_code in {500, 503}
            )

            # GET retry
            if (
                method == "GET"
                and transient_error
                and attempts_remaining
            ):

                logger.info(
                    "RETRYING GET path=%s",
                    path
                )

                time.sleep(RETRY_DELAY_SECONDS)
                continue

            # Optional write retry
            if (
                method in {"POST", "PUT", "PATCH", "DELETE"}
                and RETRY_WRITES_ON_5XX
                and transient_error
                and attempts_remaining
            ):

                logger.info(
                    "RETRYING WRITE method=%s path=%s",
                    method,
                    path
                )

                time.sleep(RETRY_DELAY_SECONDS)
                continue

            return error_body, status_code

        # -------------------------------------------------------------------
        # TIMEOUT
        # -------------------------------------------------------------------

        except (TimeoutError, socket.timeout):

            logger.error(
                "HOSP TIMEOUT method=%s path=%s attempt=%s",
                method,
                path,
                attempt
            )

            if method == "GET" and attempts_remaining:

                logger.info(
                    "RETRYING GET AFTER TIMEOUT path=%s",
                    path
                )

                time.sleep(RETRY_DELAY_SECONDS)
                continue

            # Do not retry writes after timeout:
            # outcome of the write may be unknown.
            return (
                json.dumps({
                    "error": "HOSP timed out"
                }),
                504
            )

        # -------------------------------------------------------------------
        # NETWORK / DNS / CONNECTION FAILURE
        # -------------------------------------------------------------------

        except urllib.error.URLError as error:

            logger.error(
                "HOSP CONNECTION ERROR method=%s path=%s "
                "attempt=%s message=%s",
                method,
                path,
                attempt,
                str(error.reason)
            )

            if method == "GET" and attempts_remaining:

                logger.info(
                    "RETRYING GET AFTER CONNECTION ERROR path=%s",
                    path
                )

                time.sleep(RETRY_DELAY_SECONDS)
                continue

            return (
                json.dumps({
                    "error": "Unable to contact HOSP"
                }),
                502
            )

        # -------------------------------------------------------------------
        # UNEXPECTED ERROR
        # -------------------------------------------------------------------

        except Exception as error:

            logger.exception(
                "UNEXPECTED PROXY ERROR method=%s path=%s type=%s",
                method,
                path,
                type(error).__name__
            )

            return (
                json.dumps({
                    "error": "Unexpected proxy failure"
                }),
                502
            )

    return (
        json.dumps({
            "error": "Unexpected proxy failure"
        }),
        502
    )


# ---------------------------------------------------------------------------
# ALB RESPONSE FORMATTER
# ---------------------------------------------------------------------------

def build_response(
    status_code,
    body,
    cache_status
):
    """
    Returns the response format expected by an ALB Lambda target.

    X-Proxy-Cache values:

    HIT
        DynamoDB returned the response.

    MISS
        HOSP was contacted.

    BYPASS
        Request is not cacheable, normally a write.
    """

    if body is None:
        body = ""

    elif not isinstance(body, str):
        body = json.dumps(body)

    return {
        "isBase64Encoded": False,
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "X-Proxy-Cache": cache_status
        },
        "body": body
    }
