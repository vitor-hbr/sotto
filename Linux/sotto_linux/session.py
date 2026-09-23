"""Recording lifecycle, independent of GTK for protocol and failure testing."""

import time
import uuid

from .api import APIError


def upload(client, cancel, generation_id, kind, sequence, pcm, sample_rate=16000, channels=1, attempts=3):
    """Send one audio chunk. The server stores chunks by sequence number and
    answers a repeated chunk with the same receipt, so a lost connection may be
    retried with identical bytes. Any HTTP status error is final."""
    for attempt in range(attempts):
        try:
            return client.audio(generation_id, kind, sequence, pcm, sample_rate, channels)
        except APIError as error:
            if error.status_code is not None or attempt == attempts - 1 or cancel.is_set():
                raise
            cancel.wait(0.2 * (attempt + 1))


def record(client, device, capture_factory, stop, cancel, status, *, poll_seconds=0.5, result_timeout=600,
           mode="dictation", continuation_id=None, warmup_timeout=120):
    generation_id = None
    capture = None
    sealed = False
    try:
        status("Connecting…")
        request_id = str(uuid.uuid4())
        deadline = time.monotonic() + warmup_timeout
        warming = False
        while True:
            remaining = deadline - time.monotonic()
            if warming and remaining <= 0:
                raise APIError("Speech model did not become ready in time.")
            try:
                record = client.create(device, mode, request_id=request_id, timeout=max(0.001, remaining))
                generation_id = record["id"]
                if time.monotonic() >= deadline:
                    # A late successful admission must be cancelled below, not
                    # turn into a microphone recording after the wait expired.
                    raise APIError("Speech model did not become ready in time.")
                break
            except APIError as error:
                # A 503 starts server warmup before admission. Keep the same
                # idempotency key; never retry delivery or an ambiguous admission
                # error. Stopping here must never open the microphone.
                remaining = deadline - time.monotonic()
                if error.status_code != 503 or remaining <= 0:
                    raise
                warming = True
                if stop.is_set() or cancel.is_set():
                    raise APIError("Recording cancelled before the microphone started.") from None
                status("Loading speech model…")
                stop.wait(min(0.25, remaining))
                if stop.is_set() or cancel.is_set():
                    raise APIError("Recording cancelled before the microphone started.") from None
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
        # Audio the server acknowledged after the last complete loop pass. When
        # the microphone or the connection fails mid-take, this much is kept.
        committed = (0, 0)
        interruption = None
        started = time.monotonic()
        try:
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
                receipt = upload(client, cancel, generation_id, "inference", sequence, pcm)
                if receipt["nextSequence"] != sequence + 1 or receipt["frameCount"] != frames:
                    raise APIError("Server did not acknowledge the complete audio chunk.")
                if keep_original:
                    original, rate, channels, target_frames = capture.original_audio(frames)
                    stride = channels * 4
                    chunk_size = (1_048_576 // stride) * stride
                    for offset in range(0, len(original), chunk_size):
                        chunk = original[offset:offset + chunk_size]
                        original_frames += len(chunk) // stride
                        receipt = upload(client, cancel, generation_id, "original", original_sequence, chunk, rate, channels)
                        if receipt["nextSequence"] != original_sequence + 1 or receipt["frameCount"] != original_frames:
                            raise APIError("Server did not acknowledge the complete original audio chunk.")
                        original_sequence += 1
                    if original_frames != target_frames:
                        raise APIError("Original audio frame counts did not match capture.")
                sequence += 1
                committed = (frames, original_frames)
        except APIError as error:
            # Never discard speech the server already holds. Below a quarter
            # second there is nothing to transcribe, so the take is cancelled.
            if cancel.is_set() or committed[0] < 4000:
                raise
            interruption = error
            frames, original_frames = committed
        capture.close()
        capture = None
        if cancel.is_set():
            raise APIError("Recording cancelled.")
        if frames < 4000:
            raise APIError("Recording was too short. Speak for at least a quarter of a second.")
        status("Recording interrupted — transcribing what was captured…" if interruption else "Transcribing…")
        payload = {"inferenceFrames": frames}
        if continuation_id is not None:
            payload["continuationID"] = continuation_id
        if keep_original:
            payload["originalFrames"] = original_frames
        try:
            record = client.generation(generation_id, "finish", payload)
        except APIError as error:
            if interruption is None:
                raise
            raise APIError(f"{interruption} The interrupted recording could not be saved.") from error
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
        if interruption is not None:
            record["clientWarning"] = f"Recording stopped early: {interruption}"
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
