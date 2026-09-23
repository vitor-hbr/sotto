import json
import threading
import unittest
import uuid
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sotto_linux.api import APIError, Client, validate_endpoint
from sotto_linux.session import record


GENERATION_ID = str(uuid.uuid4())


class Capture:
    def __init__(self, stop, chunks=2, rate=16000, channels=1):
        self.stop = stop
        self.remaining = chunks
        self.closed = False
        self.started = False
        self.original_frames = 0
        self.rate, self.channels = rate, channels

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

    def original_audio(self, frames):
        target = frames * self.rate // 16000
        pcm = b"\0" * ((target - self.original_frames) * self.channels * 4)
        self.original_frames = target
        return pcm, self.rate, self.channels, target

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

    def create(self, device, mode="dictation", *, request_id=None, timeout=None):
        self.calls.append(("create", device))
        return {"id": GENERATION_ID, "settings": {"preferences": {"keepOriginalAudio": self.keep}}}

    def audio(self, generation_id, kind, sequence, pcm, sample_rate=16000, channels=1):
        self.calls.append(("audio", kind, sequence, len(pcm)))
        if self.fail_upload:
            raise APIError("Disconnected")
        self.frames[kind] = self.frames.get(kind, 0) + len(pcm) // (4 * channels)
        return {"nextSequence": sequence + (2 if self.bad_receipt else 1), "frameCount": self.frames[kind]}

    def generation(self, generation_id, action="", payload=None):
        self.calls.append((action, payload))
        return {"id": GENERATION_ID, "status": self.status, "insertionText": "Hello, Linux."}


class SessionTests(unittest.TestCase):
    def run_session(self, client, chunks=2, cancel=None, rate=16000, channels=1, **kwargs):
        stop = threading.Event()
        capture = Capture(stop, chunks, rate, channels)
        self.capture = capture
        return record(client, {"id": "test", "name": "test"}, lambda: capture,
                      stop, cancel or threading.Event(), lambda _: None, poll_seconds=0, **kwargs)

    def test_first_recording_waits_for_warmup_with_one_request_identity(self):
        client = FakeClient(False)
        client.create = Mock(side_effect=[APIError("Warming", status_code=503),
                                         {"id": GENERATION_ID, "settings": {"preferences": {"keepOriginalAudio": False}}}])
        result = self.run_session(client)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.capture.started)
        self.assertEqual(client.create.call_count, 2)
        identifiers = [call.kwargs["request_id"] for call in client.create.call_args_list]
        self.assertEqual(identifiers[0], identifiers[1])
        self.assertEqual(str(uuid.UUID(identifiers[0])), identifiers[0])

    def test_warmup_does_not_retry_after_deadline(self):
        client = FakeClient()
        client.create = Mock(side_effect=APIError("Warming", status_code=503))
        with patch("sotto_linux.session.time.monotonic", side_effect=[0, 0, 29.9, 30.1]):
            with self.assertRaisesRegex(APIError, "in time"):
                self.run_session(client, warmup_timeout=30)
        self.assertEqual(client.create.call_count, 1)
        self.assertFalse(self.capture.started)

    def test_late_successful_admission_is_cancelled_without_microphone(self):
        client = FakeClient()
        # The second response arrives late even though its socket timeout was
        # bounded (e.g. a response body trickled in before each socket timeout).
        client.create = Mock(side_effect=[APIError("Warming", status_code=503),
                                         {"id": GENERATION_ID, "settings": {"preferences": {"keepOriginalAudio": False}}}])
        with patch("sotto_linux.session.time.monotonic", side_effect=[0, 0, 29.5, 29.75, 30.1, 30.1]):
            with self.assertRaisesRegex(APIError, "in time"):
                self.run_session(client, warmup_timeout=30)
        self.assertFalse(self.capture.started)
        self.assertIn(("cancel", None), client.calls)
        self.assertEqual(client.create.call_args.kwargs["timeout"], 0.25)

    def test_stop_or_cancel_during_warmup_never_opens_microphone(self):
        for action in ("stop", "cancel"):
            stop, cancel = threading.Event(), threading.Event()
            client = FakeClient()
            def warming(*_args, **_kwargs):
                (stop if action == "stop" else cancel).set()
                raise APIError("Warming", status_code=503)
            client.create = Mock(side_effect=warming)
            capture = Mock()
            with self.assertRaisesRegex(APIError, "cancelled"):
                record(client, {}, capture, stop, cancel, lambda _: None)
            capture.assert_not_called()
            self.assertEqual(client.create.call_count, 1)
            self.assertFalse(client.calls)

    def test_admission_retries_are_bounded_and_only_for_503(self):
        for error in (APIError("Warming", status_code=503), APIError("Busy", status_code=409),
                      APIError("Unauthorized", status_code=401), APIError("Disconnected")):
            client = FakeClient()
            client.create = Mock(side_effect=error)
            with self.assertRaises(APIError):
                self.run_session(client, warmup_timeout=0 if error.status_code == 503 else 30)
            self.assertEqual(client.create.call_count, 1)
            self.assertFalse(self.capture.started)

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

    def test_high_rate_original_audio_uses_independent_bounded_chunks(self):
        client = FakeClient()
        self.run_session(client, rate=192000, channels=8)
        original = [call for call in client.calls if call[0] == "audio" and call[1] == "original"]
        self.assertEqual([call[2] for call in original], [0, 1, 2, 3])
        self.assertTrue(all(call[3] <= 1_048_576 for call in original))
        self.assertIn(("finish", {"inferenceFrames": 8000, "originalFrames": 96000}), client.calls)

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

    def test_microphone_failure_after_speech_transcribes_what_was_captured(self):
        client = FakeClient(False)
        stop = threading.Event()
        capture = Capture(stop, chunks=3)
        read = capture.read
        def failing_read():
            if capture.remaining == 1:
                raise APIError("No microphone audio received for five seconds.")
            return read()
        capture.read = failing_read
        result = record(client, {"id": "test", "name": "test"}, lambda: capture, stop, threading.Event(),
                        lambda _: None, poll_seconds=0)
        self.assertEqual(result["insertionText"], "Hello, Linux.")
        self.assertIn("No microphone audio", result["clientWarning"])
        self.assertIn(("finish", {"inferenceFrames": 8000}), client.calls)
        self.assertNotIn(("cancel", None), client.calls)
        self.assertTrue(capture.closed)

    def test_microphone_failure_before_speech_still_cancels(self):
        client = FakeClient(False)
        stop = threading.Event()
        capture = Capture(stop, chunks=3)
        def failing_read():
            raise APIError("Microphone disconnected or stopped. Check GNOME sound settings.")
        capture.read = failing_read
        with self.assertRaisesRegex(APIError, "Microphone disconnected"):
            record(client, {"id": "test", "name": "test"}, lambda: capture, stop, threading.Event(),
                   lambda _: None, poll_seconds=0)
        self.assertIn(("cancel", None), client.calls)
        self.assertFalse(any(call[0] == "finish" for call in client.calls))

    def test_interrupted_take_that_cannot_seal_reports_the_interruption(self):
        client = FakeClient(True)
        stop = threading.Event()
        capture = Capture(stop, chunks=3)
        read = capture.read
        def failing_read():
            if capture.remaining == 1:
                raise APIError("No microphone audio received for five seconds.")
            return read()
        capture.read = failing_read
        generation = client.generation
        def refuse_finish(generation_id, action="", payload=None):
            if action == "finish":
                raise APIError("Server returned HTTP 409.", status_code=409)
            return generation(generation_id, action, payload)
        client.generation = refuse_finish
        with self.assertRaisesRegex(APIError, "No microphone audio.*could not be saved"):
            record(client, {"id": "test", "name": "test"}, lambda: capture, stop, threading.Event(),
                   lambda _: None, poll_seconds=0)
        self.assertIn(("cancel", None), client.calls)

    def test_lost_connection_retries_the_same_chunk_once_and_keeps_going(self):
        client = FakeClient(False)
        audio = client.audio
        failures = {"left": 1}
        def flaky_audio(generation_id, kind, sequence, pcm, sample_rate=16000, channels=1):
            if failures["left"] and sequence == 1:
                failures["left"] -= 1
                client.calls.append(("audio", kind, sequence, len(pcm)))
                raise APIError("Cannot reach the server. Check its address and connection.")
            return audio(generation_id, kind, sequence, pcm, sample_rate, channels)
        client.audio = flaky_audio
        result = self.run_session(client, chunks=3)
        self.assertEqual(result["insertionText"], "Hello, Linux.")
        self.assertNotIn("clientWarning", result)
        uploads = [call[2] for call in client.calls if call[0] == "audio"]
        self.assertEqual(uploads, [0, 1, 1, 2])
        self.assertIn(("finish", {"inferenceFrames": 12000}), client.calls)

    def test_http_status_errors_during_upload_are_never_retried(self):
        client = FakeClient(False)
        audio = client.audio
        def rejecting_audio(generation_id, kind, sequence, pcm, sample_rate=16000, channels=1):
            if sequence == 1:
                client.calls.append(("audio", kind, sequence, len(pcm)))
                raise APIError("Server returned HTTP 409.", status_code=409)
            return audio(generation_id, kind, sequence, pcm, sample_rate, channels)
        client.audio = rejecting_audio
        result = self.run_session(client, chunks=3)
        uploads = [call[2] for call in client.calls if call[0] == "audio"]
        self.assertEqual(uploads, [0, 1])
        # The first quarter second was acknowledged, so it is transcribed with a warning.
        self.assertIn("HTTP 409", result["clientWarning"])
        self.assertIn(("finish", {"inferenceFrames": 4000}), client.calls)

    def test_persistent_connection_loss_stops_after_bounded_retries(self):
        client = FakeClient(False)
        client.fail_upload = True
        cancel = threading.Event()
        with self.assertRaisesRegex(APIError, "Disconnected"):
            self.run_session(client, cancel=cancel)
        uploads = [call for call in client.calls if call[0] == "audio"]
        self.assertEqual(len(uploads), 3)
        self.assertIn(("cancel", None), client.calls)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/warming":
                    self.send_response(503)
                    self.end_headers()
                elif self.path == "/redirect":
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

    def test_warming_status_is_available_without_exposing_server_body(self):
        with self.assertRaises(APIError) as raised:
            self.client.request("GET", "/warming")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(str(raised.exception), "Server models are not ready.")

    def test_admission_request_timeout_respects_remaining_budget(self):
        with patch.object(self.client.opener, "open", wraps=self.client.opener.open) as opened:
            self.client.create({"id": "device"}, timeout=0.25)
        self.assertEqual(opened.call_args.kwargs["timeout"], 0.25)

    def test_create_preserves_supplied_idempotency_key(self):
        identifier = str(uuid.uuid4())
        for _ in range(2):
            self.client.create({"id": "device"}, request_id=identifier)
        self.assertEqual([json.loads(item[2])["requestID"] for item in self.requests], [identifier, identifier])

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
