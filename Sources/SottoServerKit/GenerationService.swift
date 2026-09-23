import Foundation
import SottoAPI
import SottoDomain

public struct ServiceError: LocalizedError, Sendable {
    public let status: Int
    public let code: String
    public let message: String
    public var errorDescription: String? { message }
    init(_ status: Int, _ code: String, _ message: String) { self.status = status; self.code = code; self.message = message }
}

/// The sole owner of durable product state. Actor isolation serializes admission,
/// chunk commits and preference revisions; native inference never blocks this actor.
public actor GenerationService {
    private static let maximumMetadataBytes = 1_048_576
    private static let maximumPreferencesBytes = 262_144
    private static let maximumDictionaryOutputBytes = 24 * 1_024
    private struct Chunk {
        var offset: UInt64
        var count: Int
    }
    private struct Upload {
        var format: AudioStreamFormat
        var chunks: [Chunk] = []
        var bytes: Int64 = 0
        var frameCount: Int64 { bytes / Int64(format.channels * 4) }
    }
    private let configuration: ServerConfiguration
    private let inference: NativeInference
    private var preferences: PreferencesSnapshot
    private var records: [UUID: GenerationRecord] = [:]
    private var uploads: [UUID: [AudioKind: Upload]] = [:]
    private var activeID: UUID?
    private var activeTask: Task<Void, Never>?
    private var warmTask: Task<Void, Never>?
    private var expiryTask: Task<Void, Never>?
    private var warmError: String?
    private var warmFailedAt: Date?
    private var warmRetryDelay: TimeInterval = 30
    private var warming = false
    private var stopping = false
    private var subscribers: [UUID: [UUID: AsyncStream<GenerationRecord>.Continuation]] = [:]

    public init(configuration: ServerConfiguration, inference: NativeInference? = nil) throws {
        self.configuration = configuration
        self.inference = inference ?? NativeInference(configuration: configuration.inference)
        let files = FileManager.default
        try files.createDirectory(at: configuration.dataDirectory, withIntermediateDirectories: true,
                                  attributes: [.posixPermissions: 0o700])
        let directory = configuration.dataDirectory.appendingPathComponent("generations", isDirectory: true)
        try files.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        guard try files.attributesOfItem(atPath: directory.path)[.type] as? FileAttributeType == .typeDirectory else {
            throw ServiceError(500, "invalid_storage", "The generations directory must not be a symbolic link.")
        }
        let preferencesURL = configuration.dataDirectory.appendingPathComponent("preferences.json")
        if files.fileExists(atPath: preferencesURL.path) {
            let attributes = try files.attributesOfItem(atPath: preferencesURL.path)
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  let size = attributes[.size] as? NSNumber, size.intValue <= Self.maximumPreferencesBytes else {
                throw ServiceError(500, "invalid_preferences", "Server preferences must be a regular JSON file of at most 256 KiB.")
            }
            preferences = try SottoAPI.decoder().decode(PreferencesSnapshot.self, from: Data(contentsOf: preferencesURL))
            if let error = preferences.preferences.validationError { throw ServiceError(500, "invalid_preferences", error) }
        } else {
            preferences = PreferencesSnapshot()
            try SottoAPI.encoder().encode(preferences).write(to: preferencesURL, options: .atomic)
        }
        for child in try files.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil, options: .skipsHiddenFiles) {
            guard let id = UUID(uuidString: child.lastPathComponent) else { continue }
            guard try files.attributesOfItem(atPath: child.path)[.type] as? FileAttributeType == .typeDirectory else {
                throw ServiceError(500, "invalid_archive", "Generation directories must not be symbolic links.")
            }
            let metadata = child.appendingPathComponent("metadata.json")
            guard files.fileExists(atPath: metadata.path) else { continue }
            let metadataAttributes = try files.attributesOfItem(atPath: metadata.path)
            guard metadataAttributes[.type] as? FileAttributeType == .typeRegular,
                  let metadataSize = metadataAttributes[.size] as? NSNumber, metadataSize.intValue <= Self.maximumMetadataBytes else {
                throw ServiceError(500, "invalid_archive", "Generation metadata must be a regular JSON file of at most 1 MiB.")
            }
            var record = try SottoAPI.decoder().decode(GenerationRecord.self, from: Data(contentsOf: metadata))
            guard record.id == id, record.schemaVersion == 1 else { throw ServiceError(500, "invalid_archive", "A generation has invalid metadata.") }
            if !record.status.isTerminal {
                record.status = .failed
                record.error = "Server restarted before this generation completed."
                record.updatedAt = Date()
                record.progress = nil
                for name in ["inference.raw", "original.raw", "inference.wav.partial", "original.wav.partial"] {
                    try? files.removeItem(at: child.appendingPathComponent(name))
                }
                let recovered = try SottoAPI.encoder().encode(record)
                // Recovery adds an error message. If a nearly full record has
                // no room for it, leave its readable on-disk snapshot intact.
                if recovered.count <= Self.maximumMetadataBytes { try recovered.write(to: metadata, options: .atomic) }
            }
            records[id] = record
        }
    }

    public func start() {
        guard !stopping else { return }
        beginWarmup()
        guard expiryTask == nil else { return }
        expiryTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
                await self?.heartbeat()
                await self?.expireUploads()
                await self?.keepWarm()
            }
        }
    }

    public func shutdown() async {
        stopping = true
        expiryTask?.cancel(); expiryTask = nil
        warmTask?.cancel(); warmTask = nil
        activeTask?.cancel(); activeTask = nil
        if let id = activeID { _ = try? await cancel(id) }
        await inference.shutdown()
        for group in subscribers.values { for continuation in group.values { continuation.finish() } }
        subscribers.removeAll()
    }

    public func health() async -> ServerHealth {
        let state = await inference.readiness(proofreadingEnabled: false)
        let writable = FileManager.default.isWritableFile(atPath: configuration.dataDirectory.path) && (try? requireDiskSpace()) != nil
        let ready = state.available && state.speechLoaded && writable
        let message = !writable ? "Server storage is unavailable or full." : (activeID != nil ? "Server is handling a recording." :
            (ready ? "Server ready." : (warming ? "Loading server models…" :
                (warmError.map { "Server models failed to load: \($0)" } ?? "Server models are unavailable."))))
        if !state.speechLoaded, !warming, activeID == nil { beginWarmup() }
        return ServerHealth(isDev: configuration.development, ready: ready && activeID == nil,
            speech: ModelRuntimeInfo(modelID: "whisper-large-v3-turbo", backend: Self.speechBackend, ready: state.speechLoaded),
            proofreading: ModelRuntimeInfo(modelID: "Qwen3-4B-Instruct-2507", backend: Self.proofBackend,
                                           ready: state.proofLoaded, message: preferences.preferences.textCorrectionEnabled ?
                                               (state.proofLoaded ? nil : "Unavailable; deterministic text is preserved.") : "Disabled"),
            message: message)
    }

    public func getPreferences() -> PreferencesSnapshot { preferences }

    public func updatePreferences(_ update: PreferencesSnapshot) throws -> PreferencesSnapshot {
        guard update.revision == preferences.revision else { throw ServiceError(409, "stale_preferences", "Preferences changed on another device. Reload and try again.") }
        if let error = update.preferences.validationError { throw ServiceError(400, "invalid_preferences", error) }
        let next = PreferencesSnapshot(revision: preferences.revision + 1, preferences: update.preferences)
        let data = try SottoAPI.encoder().encode(next)
        guard data.count <= Self.maximumPreferencesBytes else {
            throw ServiceError(413, "preferences_too_large", "Server preferences exceeded the 256 KiB storage limit.")
        }
        try data.write(to: configuration.dataDirectory.appendingPathComponent("preferences.json"), options: .atomic)
        preferences = next
        if activeID == nil { beginWarmup() }
        return next
    }

    public func create(_ request: CreateGenerationRequest) async throws -> GenerationRecord {
        guard !stopping else { throw ServiceError(503, "server_stopping", "The server is shutting down.") }
        guard validLabel(request.device.id, limit: 128), validLabel(request.device.name, limit: 128) else {
            throw ServiceError(400, "invalid_device", "Device ID and name must be nonempty single-line text of at most 128 characters.")
        }
        if let existing = records.values.first(where: { $0.requestID == request.requestID && $0.device.id == request.device.id }) { return existing }
        guard activeID == nil else { throw ServiceError(409, "server_busy", "The server is handling another recording. Try again when it finishes.") }
        let state = await inference.readiness(proofreadingEnabled: false)
        guard state.available else { beginWarmup(); throw ServiceError(503, "server_unavailable", state.message) }
        guard state.speechLoaded else {
            beginWarmup(); throw ServiceError(503, "server_warming", "The server is loading its models. Recording will be available when it is ready.")
        }
        // Readiness suspends the actor; admission must be checked again.
        if let existing = records.values.first(where: { $0.requestID == request.requestID && $0.device.id == request.device.id }) { return existing }
        guard activeID == nil else { throw ServiceError(409, "server_busy", "The server is handling another recording.") }
        try requireDiskSpace()
        let record = GenerationRecord(requestID: request.requestID, device: request.device, mode: request.mode, settings: preferences)
        try FileManager.default.createDirectory(at: directory(record.id), withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        try save(record)
        activeID = record.id
        uploads[record.id] = [:]
        return record
    }

    public func appendAudio(_ id: UUID, kind: AudioKind, sequence: Int, format: AudioStreamFormat, data: Data) throws -> AudioChunkReceipt {
        var record = try get(id)
        guard record.status == .receiving else { throw ServiceError(409, "upload_closed", "This recording is no longer accepting audio.") }
        guard kind != .original || record.settings.preferences.keepOriginalAudio else { throw ServiceError(400, "original_disabled", "Original audio retention was disabled for this recording.") }
        guard sequence >= 0, sequence < 4096 else { throw ServiceError(413, "chunk_limit", "This recording exceeded its audio chunk limit.") }
        guard (8_000...192_000).contains(format.sampleRate), (1...8).contains(format.channels),
              kind != .inference || format == AudioStreamFormat(sampleRate: 16_000, channels: 1) else {
            throw ServiceError(400, "invalid_format", "Inference audio must be mono 16 kHz. Original audio must have 1–8 channels at 8–192 kHz.")
        }
        guard !data.isEmpty, data.count <= SottoAPI.maximumChunkBytes, data.count % (format.channels * 4) == 0 else {
            throw ServiceError(data.count > SottoAPI.maximumChunkBytes ? 413 : 400, "invalid_chunk", "Audio chunks must contain complete float32 frames and fit within 1 MiB.")
        }
        guard Self.finiteSamples(data) else { throw ServiceError(400, "invalid_samples", "Audio must contain finite float32 samples.") }
        var upload = uploads[id]?[kind] ?? Upload(format: format)
        guard upload.format == format else { throw ServiceError(409, "format_changed", "An audio stream cannot change format during recording.") }
        let raw = directory(id).appendingPathComponent("\(kind.rawValue).raw")
        if sequence < upload.chunks.count {
            let chunk = upload.chunks[sequence]
            let handle = try FileHandle(forReadingFrom: raw)
            defer { try? handle.close() }
            try handle.seek(toOffset: chunk.offset)
            guard chunk.count == data.count, try handle.read(upToCount: chunk.count) == data else {
                throw ServiceError(409, "conflicting_chunk", "A repeated audio chunk did not match the original.")
            }
            return AudioChunkReceipt(nextSequence: upload.chunks.count, frameCount: upload.frameCount)
        }
        guard sequence == upload.chunks.count else { throw ServiceError(409, "missing_chunk", "Audio chunks must arrive in sequence.") }
        let nextBytes = upload.bytes + Int64(data.count)
        let duration = Double(nextBytes) / Double(format.sampleRate * format.channels * 4)
        guard duration <= Double(SottoAPI.maximumRecordingSeconds) + 0.1, nextBytes <= 268_435_456 else {
            throw ServiceError(413, "recording_limit", "Recordings are limited to 180 seconds and 256 MiB per audio stream.")
        }
        try requireDiskSpace()
        if upload.bytes == 0 { try Data().write(to: raw, options: .atomic) }
        let handle = try FileHandle(forWritingTo: raw)
        defer { try? handle.close() }
        try handle.seek(toOffset: UInt64(upload.bytes))
        try handle.write(contentsOf: data)
        upload.chunks.append(Chunk(offset: UInt64(upload.bytes), count: data.count))
        upload.bytes = nextBytes
        uploads[id, default: [:]][kind] = upload
        record.updatedAt = Date()
        records[id] = record
        return AudioChunkReceipt(nextSequence: upload.chunks.count, frameCount: upload.frameCount)
    }

    public func finish(_ id: UUID, request: FinishGenerationRequest) throws -> GenerationRecord {
        var record = try get(id)
        if record.status != .receiving {
            guard record.inferenceAudio?.frameCount == request.inferenceFrames,
                  record.originalAudio?.frameCount == request.originalFrames else {
                throw ServiceError(409, "conflicting_finish", "The recording was already sealed with different audio counts.")
            }
            return record
        }
        guard let streams = uploads[id], let speech = streams[.inference], speech.frameCount == request.inferenceFrames else {
            throw ServiceError(409, "incomplete_audio", "Inference audio has not been completely uploaded.")
        }
        let duration = Double(speech.frameCount) / 16_000
        guard duration >= 0.25, duration <= 180 else { throw ServiceError(400, "invalid_duration", "Recordings must be between 0.25 and 180 seconds.") }
        let original = streams[.original]
        if record.settings.preferences.keepOriginalAudio {
            guard let original, original.frameCount == request.originalFrames else { throw ServiceError(409, "incomplete_original", "Original audio has not been completely uploaded.") }
            let originalDuration = Double(original.frameCount) / Double(original.format.sampleRate)
            guard abs(originalDuration - duration) <= 0.075 else { throw ServiceError(400, "audio_mismatch", "Original and inference audio must cover the same recording interval.") }
        } else if request.originalFrames != nil || original != nil {
            throw ServiceError(400, "unexpected_original", "This recording does not retain original audio.")
        }
        let previous = continuation(request.continuationID, for: record)
        do {
            record.inferenceAudio = try seal(speech, kind: .inference, id: id)
            if let original { record.originalAudio = try seal(original, kind: .original, id: id) }
            record.status = .queued
            record.updatedAt = Date()
            record.progress = 0
            try save(record)
        } catch {
            record.status = .failed
            record.error = "The server could not preserve the complete recording."
            record.updatedAt = Date()
            do { try save(record) } catch { publish(record) }
            cleanPartial(id)
            activeID = nil
            beginWarmup()
            throw ServiceError(500, "audio_storage_failed", "The server could not preserve the complete recording.")
        }
        uploads[id] = nil
        activeTask = Task { [weak self] in await self?.process(id, previous: previous) }
        return record
    }

    public func get(_ id: UUID) throws -> GenerationRecord {
        guard let record = records[id] else { throw ServiceError(404, "not_found", "Recording not found.") }
        return record
    }

    public func history(limit: Int, before: String?) throws -> GenerationPage {
        guard (1...100).contains(limit) else { throw ServiceError(400, "invalid_limit", "History page size must be between 1 and 100.") }
        let sorted = records.values.sorted { $0.createdAt == $1.createdAt ? $0.id.uuidString > $1.id.uuidString : $0.createdAt > $1.createdAt }
        let start: Int
        if let before {
            guard let id = UUID(uuidString: before), let index = sorted.firstIndex(where: { $0.id == id }) else {
                throw ServiceError(400, "invalid_cursor", "The history cursor is no longer valid. Reload history.")
            }
            start = index + 1
        } else { start = 0 }
        let items = Array(sorted.dropFirst(start).prefix(limit))
        return GenerationPage(items: items, nextCursor: start + items.count < sorted.count ? items.last?.id.uuidString : nil)
    }

    public func events(_ id: UUID) throws -> AsyncStream<GenerationRecord> {
        let record = try get(id)
        guard (subscribers[id]?.count ?? 0) < 8 else { throw ServiceError(429, "stream_limit", "Too many connections are watching this recording.") }
        let subscriber = UUID()
        return AsyncStream(bufferingPolicy: .bufferingNewest(8)) { continuation in
            continuation.yield(record)
            if record.status.isTerminal { continuation.finish(); return }
            subscribers[id, default: [:]][subscriber] = continuation
            continuation.onTermination = { [weak self] _ in Task { await self?.removeSubscriber(id, subscriber) } }
        }
    }

    public func cancel(_ id: UUID) async throws -> GenerationRecord {
        var record = try get(id)
        guard !record.status.isTerminal else { return record }
        record.status = .cancelled
        record.error = "Recording cancelled."
        record.progress = nil
        record.updatedAt = Date()
        do { try save(record) } catch { publish(record) }
        cleanPartial(id)
        if activeID == id {
            activeTask?.cancel()
            await inference.cancel()
            if activeID == id { activeTask = nil; activeID = nil; beginWarmup() }
        }
        return record
    }

    public func recordDelivery(_ id: UUID, receipt: DeliveryReceipt) throws -> GenerationRecord {
        var record = try get(id)
        let statuses: Set<String> = ["inserted", "copied", "unconfirmed", "failed", "tested", "listUpdated", "cancelled", "none"]
        guard record.status == .completed, statuses.contains(receipt.status), (receipt.message?.utf8.count ?? 0) <= 4096 else {
            throw ServiceError(400, "invalid_delivery", "A valid delivery receipt requires a completed generation.")
        }
        if let existing = record.delivery {
            guard existing.status == receipt.status, existing.message == receipt.message else { throw ServiceError(409, "delivery_recorded", "This recording already has a delivery outcome.") }
            return record
        }
        record.delivery = DeliveryReceipt(status: receipt.status, message: receipt.message, reportedAt: Date())
        record.updatedAt = Date()
        try save(record)
        return record
    }

    public func delete(_ id: UUID) throws {
        let record = try get(id)
        guard record.status.isTerminal else { throw ServiceError(409, "generation_active", "Cancel or finish a recording before deleting it.") }
        try FileManager.default.removeItem(at: directory(id))
        records[id] = nil
    }

    public func artifact(_ id: UUID, filename: String) throws -> URL {
        let record = try get(id)
        let allowed = ["metadata.json", "transcript.txt", "inference.wav", "original.wav"]
        guard allowed.contains(filename), filename != "inference.wav" || record.inferenceAudio != nil,
              filename != "original.wav" || record.originalAudio != nil,
              filename != "transcript.txt" || record.status == .completed else {
            throw ServiceError(404, "artifact_not_found", "Artifact not found.")
        }
        let url = directory(id).appendingPathComponent(filename)
        let files = FileManager.default
        guard (try? files.attributesOfItem(atPath: directory(id).path)[.type] as? FileAttributeType) == .typeDirectory,
              (try? files.attributesOfItem(atPath: url.path)[.type] as? FileAttributeType) == .typeRegular else {
            throw ServiceError(404, "artifact_not_found", "Artifact not found.")
        }
        return url
    }

    private func process(_ id: UUID, previous: DictationContinuation?) async {
        do {
            var record = try get(id)
            let settings = record.settings.preferences
            record.status = .transcribing
            try save(record)
            let vocabulary = settings.dictionary.recognitionVocabularyTerms(settings.vocabulary)
            let speech = try await inference.transcribe(directory(id).appendingPathComponent("inference.wav"),
                language: settings.language, vocabularyTerms: vocabulary,
                onProgress: { [weak self] value in Task { await self?.progress(id, value) } })
            try Task.checkCancellation()
            guard try get(id).status == .transcribing else { return }
            record.rawText = speech.text
            record.detectedLanguage = speech.language
            record.recognitionHints = speech.hints
            record.speech = ModelProvenance(modelID: "whisper-large-v3-turbo", modelSHA256: speech.modelSHA256, backend: Self.speechBackend,
                                           engineVersion: speech.engineVersion, processingSeconds: speech.processingSeconds)
            let cleaned = TranscriptCleaner.clean(speech.text)
            let transcript = settings.dictionary.apply(to: cleaned, maximumOutputUTF8Bytes: Self.maximumDictionaryOutputBytes)
            let structured = SpokenListFormatter.format(transcript, context: previous?.list)
            record.formattingRejectionReason = structured.formattingRejectionReason
            record.consumedListControls = structured.consumedControls
            if settings.textCorrectionEnabled, !structured.text.isEmpty { record.status = .proofreading; record.progress = nil; try save(record) }
            let processing = try await proofread(structured.text, settings: settings, dictionaryChanged: cleaned != transcript, language: speech.language)
            try Task.checkCancellation()
            guard !(try get(id)).status.isTerminal else { return }
            record.textProcessing = processing
            if [.applied, .unchanged, .rejected].contains(processing.status) {
                let allTerms = settings.dictionary.vocabularyTerms
                let included = TextCorrectionPolicy.modelHints(allTerms)
                record.proofreadingHints = ModelHintUsage(includedTerms: included,
                    omittedTerms: allTerms.filter { !Set(included).contains($0) })
            }
            if settings.textCorrectionEnabled {
                record.proofreading = ModelProvenance(modelID: "Qwen3-4B-Instruct-2507", modelSHA256: processing.modelSHA256,
                    backend: Self.proofBackend, engineVersion: processing.engineVersion, processingSeconds: processing.processingSeconds)
            }
            let formatted = structured.replacingText(processing.outputText)
            let composition = DictationComposer.compose(formatted, previous: previous)
            record.finalText = formatted.text
            record.insertionText = composition.insertion
            record.previewText = composition.preview
            record.continuation = composition.continuation
            record.status = .completed
            record.updatedAt = Date()
            record.progress = 1
            try Data(record.finalText.utf8).write(to: directory(id).appendingPathComponent("transcript.txt"), options: .atomic)
            try save(record)
        } catch {
            if var record = records[id], !record.status.isTerminal {
                record.status = Task.isCancelled ? .cancelled : .failed
                record.error = error.localizedDescription
                record.updatedAt = Date()
                record.progress = nil
                do { try save(record) } catch { publish(record) }
            }
        }
        if activeID == id, records[id]?.status != .cancelled { activeID = nil; activeTask = nil; beginWarmup() }
    }

    private func proofread(_ text: String, settings: ServerPreferences, dictionaryChanged: Bool, language: String) async throws -> TextProcessingRecord {
        let start = Date()
        let terms = settings.dictionary.vocabularyTerms
        func make(_ status: TextProcessingRecord.Status, output: String? = nil, reason: String? = nil,
                  proposedText: String? = nil, verifiedRepairs: [VerifiedTextRepair]? = nil,
                  seconds: Double? = nil, version: String? = nil, sha256: String? = nil) -> TextProcessingRecord {
            TextProcessingRecord(dictionaryTerms: terms, dictionaryChangedText: dictionaryChanged,
                inputText: text, outputText: output ?? text, enabled: settings.textCorrectionEnabled, status: status, reason: reason,
                modelID: settings.textCorrectionEnabled ? "Qwen3-4B-Instruct-2507" : nil, modelSHA256: sha256, engineVersion: version,
                processingSeconds: seconds, wallSeconds: Date().timeIntervalSince(start),
                proposedText: proposedText, verifiedRepairs: verifiedRepairs)
        }
        guard settings.textCorrectionEnabled else { return make(.disabled) }
        guard !text.isEmpty else { return make(.skipped, reason: "No text to correct.") }
        guard text.count <= TextCorrectionPolicy.maximumInputCharacters else { return make(.skipped, reason: "The transcript exceeded the correction length limit.") }
        do {
            let proof = try await inference.correct(text, terms: TextCorrectionPolicy.modelHints(terms), language: language,
                                                    systemPrompt: settings.proofreadingPrompt)
            try Task.checkCancellation()
            let candidate = settings.dictionary.apply(to: proof.text.trimmingCharacters(in: .whitespacesAndNewlines),
                                                      maximumOutputUTF8Bytes: Self.maximumDictionaryOutputBytes)
            let evaluation = TextCorrectionPolicy.evaluate(original: text, candidate: candidate, preferredTerms: terms)
            if let reason = evaluation.rejectionReason {
                return make(.rejected, reason: reason, proposedText: candidate, verifiedRepairs: evaluation.verifiedRepairs,
                            seconds: proof.processingSeconds, version: proof.engineVersion, sha256: proof.modelSHA256)
            }
            return make(candidate == text ? .unchanged : .applied, output: candidate,
                        proposedText: candidate, verifiedRepairs: evaluation.verifiedRepairs,
                        seconds: proof.processingSeconds, version: proof.engineVersion, sha256: proof.modelSHA256)
        } catch {
            try Task.checkCancellation()
            return make(.failed, reason: error.localizedDescription)
        }
    }

    private func continuation(_ id: UUID?, for record: GenerationRecord) -> DictationContinuation? {
        guard let id else { return nil }
        guard let previous = records[id], previous.device.id == record.device.id, previous.status == .completed,
              Date().timeIntervalSince(previous.updatedAt) >= 0, Date().timeIntervalSince(previous.updatedAt) < 900,
              previous.mode == record.mode,
              previous.mode == .test || ["inserted", "listUpdated"].contains(previous.delivery?.status ?? "") else {
            // Caret context is only a hint. Losing history or its confirmation
            // must never discard the complete new recording.
            return nil
        }
        return previous.continuation
    }

    private func seal(_ upload: Upload, kind: AudioKind, id: UUID) throws -> AudioArtifact {
        let raw = directory(id).appendingPathComponent("\(kind.rawValue).raw")
        let temporary = directory(id).appendingPathComponent("\(kind.rawValue).wav.partial")
        let output = directory(id).appendingPathComponent("\(kind.rawValue).wav")
        try WaveFile.write(rawURL: raw, outputURL: temporary, sampleRate: upload.format.sampleRate, channels: upload.format.channels, float: true)
        if FileManager.default.fileExists(atPath: output.path) { try FileManager.default.removeItem(at: output) }
        try FileManager.default.moveItem(at: temporary, to: output)
        try FileManager.default.removeItem(at: raw)
        return AudioArtifact(filename: output.lastPathComponent, sampleRate: upload.format.sampleRate, channels: upload.format.channels,
                             frameCount: upload.frameCount, byteCount: upload.bytes + 44)
    }

    private func save(_ record: GenerationRecord) throws {
        let data = try SottoAPI.encoder().encode(record)
        guard data.count <= Self.maximumMetadataBytes else {
            throw ServiceError(413, "metadata_too_large", "The generation metadata exceeded its 1 MiB storage limit.")
        }
        try data.write(to: directory(record.id).appendingPathComponent("metadata.json"), options: .atomic)
        publish(record)
    }
    private func publish(_ record: GenerationRecord) {
        records[record.id] = record
        for continuation in subscribers[record.id]?.values ?? [:].values {
            continuation.yield(record)
            if record.status.isTerminal { continuation.finish() }
        }
        if record.status.isTerminal { subscribers[record.id] = nil }
    }

    private func directory(_ id: UUID) -> URL { configuration.dataDirectory.appendingPathComponent("generations/\(id.uuidString)", isDirectory: true) }
    private func removeSubscriber(_ id: UUID, _ subscriber: UUID) { subscribers[id]?[subscriber] = nil }
    private func validLabel(_ text: String, limit: Int) -> Bool {
        !text.isEmpty && text.count <= limit && text == text.trimmingCharacters(in: .whitespacesAndNewlines)
            && text.rangeOfCharacter(from: .controlCharacters.union(.newlines)) == nil
    }
    private func requireDiskSpace() throws {
        let attributes = try FileManager.default.attributesOfFileSystem(forPath: configuration.dataDirectory.path)
        if let free = attributes[.systemFreeSize] as? NSNumber, free.int64Value < 100 * 1024 * 1024 {
            throw ServiceError(507, "storage_full", "The server needs more free disk space before accepting audio.")
        }
    }
    private static func finiteSamples(_ data: Data) -> Bool {
        data.withUnsafeBytes { storage in
            for offset in stride(from: 0, to: storage.count, by: 4) {
                let bits = UInt32(littleEndian: storage.loadUnaligned(fromByteOffset: offset, as: UInt32.self))
                if bits & 0x7f80_0000 == 0x7f80_0000 { return false }
            }
            return true
        }
    }
    private func cleanPartial(_ id: UUID) {
        uploads[id] = nil
        for filename in ["inference.raw", "original.raw", "inference.wav.partial", "original.wav.partial"] {
            try? FileManager.default.removeItem(at: directory(id).appendingPathComponent(filename))
        }
    }
    private func progress(_ id: UUID, _ value: Double) {
        guard var record = records[id], record.status == .transcribing, value.isFinite else { return }
        record.progress = min(1, max(0, value))
        records[id] = record
        for continuation in subscribers[id]?.values ?? [:].values { continuation.yield(record) }
    }
    private func heartbeat() {
        for (id, group) in subscribers {
            guard let record = records[id] else { continue }
            for continuation in group.values { continuation.yield(record) }
        }
    }
    private func expireUploads() async {
        guard let id = activeID, let record = records[id], record.status == .receiving,
              Date().timeIntervalSince(record.updatedAt) > 45 || Date().timeIntervalSince(record.createdAt) > 300 else { return }
        _ = try? await cancel(id)
    }
    private func beginWarmup() {
        guard !stopping, !warming, activeID == nil else { return }
        warming = true
        warmError = nil
        let enabled = preferences.preferences.textCorrectionEnabled
        warmTask = Task { [weak self, inference] in
            do { try await inference.warmUp(proofreadingEnabled: enabled); await self?.warmupFinished(error: nil) }
            catch { await self?.warmupFinished(error: error.localizedDescription) }
        }
    }
    private func warmupFinished(error: String?) {
        warming = false; warmTask = nil; warmError = error
        if let error {
            warmFailedAt = Date()
            fputs("Sotto: model warmup failed: \(error)\n", stderr)
        } else {
            warmFailedAt = nil
            warmRetryDelay = 30
        }
    }
    /// Reloads a helper that exited while idle so the first recording never waits
    /// for a cold model. Failures back off exponentially; only verified, present
    /// models are retried, so a missing file never causes a load loop.
    private func keepWarm() async {
        guard !stopping, !warming, activeID == nil else { return }
        if let failed = warmFailedAt {
            guard Date().timeIntervalSince(failed) >= warmRetryDelay else { return }
            warmRetryDelay = min(600, warmRetryDelay * 2)
        }
        let proofreading = preferences.preferences.textCorrectionEnabled
        let state = await inference.readiness(proofreadingEnabled: proofreading)
        guard state.available, !warming, activeID == nil else { return }
        if !state.speechLoaded || (proofreading && !state.proofLoaded) {
            if warmFailedAt == nil { fputs("Sotto: a model helper is not loaded; reloading.\n", stderr) }
            beginWarmup()
        }
    }
    private static var speechBackend: String {
        #if os(macOS)
        return "whisper.cpp/Metal"
        #else
        return "whisper.cpp"
        #endif
    }
    private static var proofBackend: String {
        #if os(macOS)
        return "MLX"
        #else
        return "llama.cpp"
        #endif
    }
}
