"""Small, bounded HTTP client. Credentials never follow redirects."""

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone


class APIError(RuntimeError):
    pass


def validate_endpoint(endpoint):
    endpoint = endpoint.strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(endpoint)
        port = parts.port
    except ValueError as exc:
        raise APIError("Invalid server address.") from exc
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in {"", "/"}
            or any(c.isspace() or ord(c) < 32 for c in endpoint)
            or port == 0):
        raise APIError("Use a server origin such as http://localhost:8391, without a path or credentials.")
    if parts.scheme == "http":
        local = parts.hostname.lower() == "localhost"
        try:
            local = local or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            pass
        if not local:
            raise APIError("Remote servers require HTTPS. HTTP is allowed only on loopback.")
    return endpoint


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, endpoint, token="", timeout=15):
        self.endpoint = validate_endpoint(endpoint)
        self.token = token.strip()
        if any(c.isspace() or ord(c) < 32 for c in self.token):
            raise APIError("The server token cannot contain whitespace.")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, payload=None):
        binary = isinstance(payload, bytes)
        data = payload if binary else (json.dumps(payload).encode() if payload is not None else None)
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.endpoint + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(2_097_153)
                if len(body) > 2_097_152:
                    raise APIError("Server response exceeded the client limit.")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            # Do not reflect URLs, tokens, or arbitrary server error bodies in diagnostics.
            messages = {401: "Server token rejected.", 409: "Server is busy or the recording conflicted.",
                        503: "Server models are not ready."}
            raise APIError(messages.get(exc.code, f"Server returned HTTP {exc.code}.")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise APIError("Cannot reach the server. Check its address and connection.") from exc
        except (ValueError, UnicodeError) as exc:
            raise APIError("Server returned invalid JSON.") from exc

    def create(self, device):
        return self.request("POST", "/v1/generations", {
            "requestID": str(uuid.uuid4()), "device": device, "mode": "dictation"})

    def generation(self, generation_id, action="", payload=None):
        # Only UUIDs from the server may become path components.
        generation_id = str(uuid.UUID(generation_id))
        return self.request("POST" if action else "GET",
                            f"/v1/generations/{generation_id}" + ("/" + action if action else ""), payload)

    def audio(self, generation_id, kind, sequence, pcm):
        if kind not in {"inference", "original"} or not pcm or len(pcm) % 4 or len(pcm) > 1_048_576:
            raise APIError("Invalid microphone audio chunk.")
        return self.generation(generation_id,
                               f"audio/{kind}?sequence={sequence}&sampleRate=16000&channels=1", pcm)

    def delivery(self, generation_id):
        return self.generation(generation_id, "delivery", {
            "status": "copied", "reportedAt": datetime.now(timezone.utc).isoformat(),
            "message": "Copied by the Linux client; paste is user-controlled."})
