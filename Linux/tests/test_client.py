import json
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sotto_linux.api import APIError, Client, validate_endpoint
from sotto_linux.session import record


GENERATION_ID = str(uuid.uuid4())


class Capture:
    def __init__(self, stop, chunks=2):
        self.stop = stop
        self.remaining = chunks
        self.closed = False
        self.started = False

    def start(self):
        self.started = True

    def read(self):
        if not self.remaining:
            self.stop.set()
            return b""
        self.remaining -= 1
        return b"\0" * 16000

    def finish(self):
        pass

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, keep=True):
        self.keep = keep
        self.calls = []
        self.frames = {}
        self.fail_upload = False
        self.bad_receipt = False
        self.status = "completed"

    def create(self, device):
        self.calls.append(("create", device))
        return {"id": GENERATION_ID, "settings": {"preferences": {"keepOriginalAudio": self.keep}}}

    def audio(self, generation_id, kind, sequence, pcm):
        self.calls.append(("audio", kind, sequence, len(pcm)))
        if self.fail_upload:
            raise APIError("Disconnected")
        self.frames[kind] = self.frames.get(kind, 0) + len(pcm) // 4
        return {"nextSequence": sequence + (2 if self.bad_receipt else 1), "frameCount": self.frames[kind]}

    def generation(self, generation_id, action="", payload=None):
        self.calls.append((action, payload))
        return {"id": GENERATION_ID, "status": self.status, "insertionText": "Hello, Linux."}


class SessionTests(unittest.TestCase):
    def run_session(self, client, chunks=2, cancel=None, **kwargs):
        stop = threading.Event()
        capture = Capture(stop, chunks)
        self.capture = capture
        return record(client, {"id": "test", "name": "test"}, lambda: capture,
                      stop, cancel or threading.Event(), lambda _: None, poll_seconds=0, **kwargs)

    def test_streams_original_only_when_requested_and_finishes_exact_frames(self):
        for keep in (True, False):
            with self.subTest(keep=keep):
                client = FakeClient(keep)
                result = self.run_session(client)
                self.assertEqual(result["insertionText"], "Hello, Linux.")
                self.assertEqual(client.frames.get("original"), 8000 if keep else None)
                expected = {"inferenceFrames": 8000}
                if keep:
                    expected["originalFrames"] = 8000
                self.assertIn(("finish", expected), client.calls)
                self.assertTrue(self.capture.closed)

    def test_failed_upload_cancels_and_closes_microphone_without_finish(self):
        client = FakeClient()
        client.fail_upload = True
        with self.assertRaises(APIError):
            self.run_session(client)
        self.assertIn(("cancel", None), client.calls)
        self.assertFalse(any(call[0] == "finish" for call in client.calls))
        self.assertTrue(self.capture.closed)

    def test_wrong_acknowledgement_cancels(self):
        client = FakeClient()
        client.bad_receipt = True
        with self.assertRaisesRegex(APIError, "acknowledge"):
            self.run_session(client)
        self.assertIn(("cancel", None), client.calls)

    def test_microphone_is_closed_before_network_cancellation(self):
        client = FakeClient()
        client.fail_upload = True
        generation = client.generation
        def cancel_after_close(generation_id, action="", payload=None):
            if action == "cancel":
                self.assertTrue(self.capture.closed)
            return generation(generation_id, action, payload)
        client.generation = cancel_after_close
        with self.assertRaises(APIError):
            self.run_session(client)

    def test_short_take_never_finishes(self):
        client = FakeClient()
        with self.assertRaisesRegex(APIError, "too short"):
            self.run_session(client, chunks=0)
        self.assertIn(("cancel", None), client.calls)

    def test_cancel_before_admission_never_starts_microphone(self):
        client = FakeClient()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(APIError):
            self.run_session(client, cancel=cancel)
        self.assertFalse(self.capture.started)
        self.assertIn(("cancel", None), client.calls)

    def test_server_failure_is_not_delivered(self):
        client = FakeClient()
        client.status = "failed"
        with self.assertRaisesRegex(APIError, "could not complete"):
            self.run_session(client)

    def test_processing_timeout_does_not_cancel_sealed_generation(self):
        client = FakeClient()
        client.status = "queued"
        with self.assertRaisesRegex(APIError, "Timed out"):
            self.run_session(client, result_timeout=0)
        self.assertNotIn(("cancel", None), client.calls)

    def test_recording_is_bounded_at_three_minutes(self):
        client = FakeClient(False)
        self.run_session(client, chunks=1000)
        self.assertEqual(client.frames["inference"], 180 * 16000)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/destination")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"not-json" if self.path == "/invalid" else b'{"ready":true}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requests.append((self.path, self.headers.get("Content-Type"), body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"nextSequence": 1, "frameCount": len(body) // 4}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}", "secret-for-test")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_authorization_and_json(self):
        self.assertEqual(self.client.request("GET", "/v1/health"), {"ready": True})
        self.assertEqual(self.requests, [("/v1/health", "Bearer secret-for-test")])

    def test_redirect_never_forwards_credentials(self):
        with self.assertRaisesRegex(APIError, "302"):
            self.client.request("GET", "/redirect")
        self.assertEqual(len(self.requests), 1)

    def test_invalid_json_is_reported(self):
        with self.assertRaisesRegex(APIError, "invalid JSON"):
            self.client.request("GET", "/invalid")

    def test_binary_wire_format(self):
        pcm = b"\0" * 16000
        self.assertEqual(self.client.audio(GENERATION_ID, "inference", 0, pcm)["frameCount"], 4000)
        self.assertEqual(self.requests[0], (
            f"/v1/generations/{GENERATION_ID}/audio/inference?sequence=0&sampleRate=16000&channels=1",
            "application/octet-stream", pcm))


class EndpointTests(unittest.TestCase):
    def test_unsafe_endpoints_are_rejected(self):
        for endpoint in ("http://192.168.1.1:8391", "ftp://localhost", "https://user:pass@example.com",
                         "https://example.com?token=x", "https://example.com/#x", "https://example.com/api",
                         "http://localhost:0", "http://localhost:99999", "https://exam\nple.com"):
            with self.subTest(endpoint=endpoint), self.assertRaises(APIError):
                validate_endpoint(endpoint)

    def test_loopback_and_https(self):
        for endpoint in ("http://localhost:8391", "http://127.0.0.1:8391", "http://[::1]:8391", "https://example.com"):
            self.assertEqual(validate_endpoint(endpoint + "/"), endpoint)


if __name__ == "__main__":
    unittest.main()
