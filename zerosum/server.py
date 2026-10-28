"""JSON over HTTP in front of the ledger.

    POST /accounts       {"id": "alice", "currency": "AUD", "allow_negative": false}
    GET  /accounts/<id>
    POST /transfers      {"legs": [...], "memo": "..."} with an Idempotency-Key header

A new account or transfer comes back as 201. A retry of one that already
exists comes back as 200 with the original, so a client can treat both as
success and tell them apart if it cares.

This is http.server from the standard library with a thread per request.
It is enough to put the ledger under real concurrent load. It isn't
something to expose to the internet: there is no TLS and no
authentication, and anyone who can reach it can move money.
"""

import json
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from zerosum.ledger import Conflict, Ledger, NotFound, Rejected

MAX_BODY = 64 * 1024  # bytes; a transfer with the maximum 100 legs is about 5 KB

STATUS = {NotFound: HTTPStatus.NOT_FOUND, Conflict: HTTPStatus.CONFLICT, Rejected: HTTPStatus.UNPROCESSABLE_ENTITY}


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class LedgerServer(ThreadingHTTPServer):
    # The default listen backlog is 5, which refuses connections as soon as
    # more than a handful of clients arrive at the same moment.
    request_queue_size = 128

    def __init__(self, ledger: Ledger, address: tuple[str, int]):
        super().__init__(address, Handler)
        self.ledger = ledger


class Handler(BaseHTTPRequestHandler):
    server: LedgerServer

    def do_GET(self) -> None:
        match urlsplit(self.path).path.split("/"):
            case ["", "accounts", account_id] if account_id:
                self._respond(lambda: (HTTPStatus.OK, self.server.ledger.account(unquote(account_id))))
            case _:
                self._send(HTTPStatus.NOT_FOUND, {"error": "no such route"})

    def do_POST(self) -> None:
        match urlsplit(self.path).path:
            case "/accounts":
                self._respond(self._open_account)
            case "/transfers":
                self._respond(self._transfer)
            case _:
                self._send(HTTPStatus.NOT_FOUND, {"error": "no such route"})

    def _open_account(self) -> tuple[HTTPStatus, dict]:
        body = self._json()
        account, created = self.server.ledger.open_account(
            body.get("id"), body.get("currency"), body.get("allow_negative", False)
        )
        return HTTPStatus.CREATED if created else HTTPStatus.OK, account

    def _transfer(self) -> tuple[HTTPStatus, dict]:
        key = self.headers.get("Idempotency-Key")
        if key is None:
            raise RequestError(HTTPStatus.BAD_REQUEST, "an Idempotency-Key header is required")
        body = self._json()
        transfer, created = self.server.ledger.transfer(key, body.get("legs"), body.get("memo", ""))
        return HTTPStatus.CREATED if created else HTTPStatus.OK, transfer

    def _json(self) -> dict:
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            raise RequestError(HTTPStatus.LENGTH_REQUIRED, "a Content-Length header is required")
        if int(length) > MAX_BODY:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"request bodies are limited to {MAX_BODY} bytes")
        try:
            body = json.loads(self.rfile.read(int(length)))
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "the body must be JSON") from None
        if not isinstance(body, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "the body must be a JSON object")
        return body

    def _respond(self, action) -> None:
        try:
            status, body = action()
        except RequestError as e:
            status, body = e.status, {"error": str(e)}
        except (NotFound, Conflict, Rejected) as e:
            status, body = STATUS[type(e)], {"error": str(e)}
        except Exception:
            # Logged with its traceback, but the client only learns that it
            # failed. Retrying with the same key is always safe.
            self.log_error("%s", traceback.format_exc())
            status, body = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}
        self._send(status, body)

    def _send(self, status: HTTPStatus, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
