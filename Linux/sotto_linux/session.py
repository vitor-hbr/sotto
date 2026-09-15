"""Recording lifecycle, independent of GTK for protocol and failure testing."""

import time

from .api import APIError


def record(client, device, capture_factory, stop, cancel, status, *, poll_seconds=0.5, result_timeout=600,
           mode="dictation", continuation_id=None):
    generation_id = None
    capture = None
    sealed = False
    try:
        status("Connecting…")
        record = client.create(device) if mode == "dictation" else client.create(device, mode)
        generation_id = record["id"]
        keep_original = record["settings"]["preferences"]["keepOriginalAudio"]
        if stop.is_set() or cancel.is_set():
            raise APIError("Recording cancelled before the microphone started.")
        capture = capture_factory()
        capture.keep_original = keep_original
        capture.start()
        status("Recording — release your shortcut or press Stop")
        frames = 0
        original_frames = 0
        original_sequence = 0
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
            receipt = client.audio(generation_id, "inference", sequence, pcm)
            if receipt["nextSequence"] != sequence + 1 or receipt["frameCount"] != frames:
                raise APIError("Server did not acknowledge the complete audio chunk.")
            if keep_original:
                original, rate, channels, target_frames = capture.original_audio(frames)
                stride = channels * 4
                chunk_size = (1_048_576 // stride) * stride
                for offset in range(0, len(original), chunk_size):
                    chunk = original[offset:offset + chunk_size]
                    original_frames += len(chunk) // stride
                    receipt = client.audio(generation_id, "original", original_sequence, chunk, rate, channels)
                    if receipt["nextSequence"] != original_sequence + 1 or receipt["frameCount"] != original_frames:
                        raise APIError("Server did not acknowledge the complete original audio chunk.")
                    original_sequence += 1
                if original_frames != target_frames:
                    raise APIError("Original audio frame counts did not match capture.")
            sequence += 1
        capture.close()
        capture = None
        if cancel.is_set():
            raise APIError("Recording cancelled.")
        if frames < 4000:
            raise APIError("Recording was too short. Speak for at least a quarter of a second.")
        status("Transcribing…")
        payload = {"inferenceFrames": frames}
        if continuation_id is not None:
            payload["continuationID"] = continuation_id
        if keep_original:
            payload["originalFrames"] = original_frames
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
        if not isinstance(record.get("insertionText"), str) or "\0" in record["insertionText"]:
            raise APIError("Server returned an invalid transcript.")
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
