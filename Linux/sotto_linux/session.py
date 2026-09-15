"""Recording lifecycle, independent of GTK for protocol and failure testing."""

import time

from .api import APIError


def record(client, device, capture_factory, stop, cancel, status, *, poll_seconds=0.5, result_timeout=600):
    generation_id = None
    capture = None
    sealed = False
    try:
        status("Connecting…")
        record = client.create(device)
        generation_id = record["id"]
        keep_original = record["settings"]["preferences"]["keepOriginalAudio"]
        if stop.is_set() or cancel.is_set():
            raise APIError("Recording cancelled before the microphone started.")
        capture = capture_factory()
        capture.start()
        status("Recording — press the shortcut again to finish")
        frames = 0
        sequence = 0
        started = time.monotonic()
        while not cancel.is_set():
            if stop.is_set():
                capture.finish()
            if time.monotonic() - started >= 180 or frames >= 180 * 16000:
                break
            pcm = capture.read()
            if pcm is None:
                continue
            pcm = pcm[:(180 * 16000 - frames) * 4]
            if not pcm:
                break
            frames += len(pcm) // 4
            for kind in (["inference", "original"] if keep_original else ["inference"]):
                receipt = client.audio(generation_id, kind, sequence, pcm)
                if receipt["nextSequence"] != sequence + 1 or receipt["frameCount"] != frames:
                    raise APIError("Server did not acknowledge the complete audio chunk.")
            sequence += 1
        capture.close()
        capture = None
        if cancel.is_set():
            raise APIError("Recording cancelled.")
        if frames < 4000:
            raise APIError("Recording was too short. Speak for at least a quarter of a second.")
        status("Transcribing…")
        payload = {"inferenceFrames": frames}
        if keep_original:
            payload["originalFrames"] = frames
        record = client.generation(generation_id, "finish", payload)
        sealed = True
        deadline = time.monotonic() + result_timeout
        while record["status"] not in {"completed", "failed", "cancelled"}:
            if cancel.is_set():
                client.generation(generation_id, "cancel")
                raise APIError("Recording cancelled.")
            if time.monotonic() >= deadline:
                raise APIError("Timed out waiting for transcription. The server may still finish; nothing will be copied later.")
            status("Proofreading…" if record["status"] == "proofreading" else "Transcribing…")
            cancel.wait(poll_seconds)
            record = client.generation(generation_id)
        if cancel.is_set():
            raise APIError("Recording cancelled.")
        if record["status"] != "completed":
            raise APIError("The server could not complete this recording.")
        return record
    except Exception:
        # Release the device before a best-effort network cancellation, which
        # can itself take a full request timeout when the connection is lost.
        if capture is not None:
            capture.close()
            capture = None
        if generation_id and not sealed:
            try:
                client.generation(generation_id, "cancel")
            except Exception:
                pass  # A disconnected server expires the incomplete upload.
        raise
    finally:
        if capture is not None:
            capture.close()
