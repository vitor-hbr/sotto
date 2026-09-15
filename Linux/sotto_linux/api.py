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


def require(condition):
    if not condition:
        raise ValueError("Invalid response schema")


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

    def request(self, method, path, payload=None, *, raw=False):
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
                limit = 268_435_500 if raw else 2_097_152
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise APIError("Server response exceeded the client limit.")
                if raw:
                    return body
                result = json.loads(body) if body else None
                if body and not isinstance(result, dict):
                    raise APIError("Server returned an incompatible response.")
                return result
        except urllib.error.HTTPError as exc:
            # Do not reflect URLs, tokens, or arbitrary server error bodies in diagnostics.
            messages = {401: "Server token rejected.", 409: "Server is busy or the recording conflicted.",
                        503: "Server models are not ready."}
            raise APIError(messages.get(exc.code, f"Server returned HTTP {exc.code}.")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise APIError("Cannot reach the server. Check its address and connection.") from exc
        except (ValueError, UnicodeError) as exc:
            raise APIError("Server returned invalid JSON.") from exc

    def create(self, device, mode="dictation"):
        return self.request("POST", "/v1/generations", {
            "requestID": str(uuid.uuid4()), "device": device, "mode": mode})

    def generation(self, generation_id, action="", payload=None):
        # Only UUIDs from the server may become path components.
        generation_id = str(uuid.UUID(generation_id))
        return self.request("POST" if action else "GET",
                            f"/v1/generations/{generation_id}" + ("/" + action if action else ""), payload)

    def audio(self, generation_id, kind, sequence, pcm, sample_rate=16000, channels=1):
        if (kind not in {"inference", "original"} or not 1 <= channels <= 8 or not 8000 <= sample_rate <= 192000
                or not pcm or len(pcm) % (4 * channels) or len(pcm) > 1_048_576):
            raise APIError("Invalid microphone audio chunk.")
        return self.generation(generation_id,
                               f"audio/{kind}?sequence={sequence}&sampleRate={sample_rate}&channels={channels}", pcm)

    def delivery(self, generation_id, status="copied", message="Copied by the Linux client; paste is user-controlled."):
        return self.generation(generation_id, "delivery", {
            "status": status, "reportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message": message})

    def preferences(self, snapshot=None):
        result = self.request("GET" if snapshot is None else "PUT", "/v1/preferences", snapshot)
        try:
            preferences = result["preferences"]
            require(type(result["revision"]) is int)
            require(all(isinstance(preferences[key], str) for key in ("language", "proofreadingPrompt", "vocabulary")))
            require(all(type(preferences[key]) is bool for key in ("textCorrectionEnabled", "keepOriginalAudio")))
            require(isinstance(preferences["dictionary"]["lists"], list))
            for group in preferences["dictionary"]["lists"]:
                require(all(isinstance(group[key], str) for key in ("id", "name")))
                require(isinstance(group["entries"], list))
                for entry in group["entries"]:
                    require(all(isinstance(entry[key], str) for key in ("id", "term")))
                    require(isinstance(entry.get("aliases", []), list))
                    require(all(isinstance(alias, str) for alias in entry.get("aliases", [])))
                    require(type(entry.get("isPriority", False)) is bool)
        except (KeyError, TypeError, ValueError):
            raise APIError("Server returned incompatible preferences.") from None
        return result

    def history(self, before=None):
        query = "?limit=50"
        if before:
            query += "&before=" + str(uuid.UUID(before))
        result = self.request("GET", "/v1/generations" + query)
        try:
            require(isinstance(result["items"], list))
            if result.get("nextCursor"):
                uuid.UUID(result["nextCursor"])
            for record in result["items"]:
                uuid.UUID(record["id"])
                require(all(isinstance(record[key], str) for key in ("createdAt", "status", "finalText", "rawText")))
                require(isinstance(record["device"]["name"], str))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise APIError("Server returned incompatible history.") from None
        return result

    def delete(self, generation_id):
        return self.request("DELETE", "/v1/generations/" + str(uuid.UUID(generation_id)))

    def artifact(self, generation_id, filename):
        if filename not in {"original.wav", "inference.wav", "transcript.txt", "metadata.json"}:
            raise APIError("Unknown recording artifact.")
        return self.request("GET", f"/v1/generations/{uuid.UUID(generation_id)}/artifacts/{filename}", raw=True)
