// Records a call on this Mac (ScreenCaptureKit, no network): the meeting app's windows, a whole display, or sound
// only, with the call's sound and your microphone kept as SEPARATE audio tracks.
//
//   quorum-record list [--events <file>]
//       JSON: {"displays": [...], "apps": [...], "microphones": [...], "picker": bool}. Never asks for screen access:
//       without it (picker: true) the apps come from the running apps, without window titles.
//   quorum-record start (--app <bundle-id> | --app-any | --display <id> | --audio-only) [--mic <device-uid>]
//                       --out <file.mov> [--live <dir>] [--events <file>]
//       Records until SIGINT/SIGTERM, then finishes the file. Prints JSON lines (on stdout, and appended to
//       --events when given): launched (pid), picker, started, levels (every second), warning, stopped, error.
//
// It runs as a small background app, "Quorum Recorder.app" (opened with `open`), so macOS asks for microphone
// permission for Quorum Recorder itself, whatever started the transcriber (Terminal, VS Code, …).
// Screen: when Screen & System Audio Recording is allowed for the app, it records the named app or display
// directly. Otherwise (or with --app-any) it opens macOS's own window/screen picker (SCContentSharingPicker), which
// needs no Screen Recording permission: the user's choice in the picker is the consent. On managed Macs (MDM) and
// standard accounts the permission switch may not take effect for a self-built app, so the picker is the
// dependable path.
//
// The .mov has: H.264 video (2 fps, at most 1920 px wide; none with --audio-only), AAC audio track 0 = the call
// ("System audio") and audio track 1 = your microphone ("Microphone"). The same layout is written to
// <file>.quorum.json so the transcriber never has to guess which track is which.
// The file is written in 10-second fragments, so a crash or power cut keeps all but the last few seconds.
// With --live <dir>: each track is also appended as raw 16 kHz mono 16-bit PCM (<dir>/system.pcm, <dir>/mic.pcm,
// both aligned to the start of the recording) and a video frame is saved every 2 s as <dir>/frames/<second>.jpg.
// The microphone follows the Mac's input when no --mic is given (and falls back to it when the chosen one
// disappears); gaps are filled with silence so both tracks keep the same timing.
// Built by scripts/setup_local_ai.sh, or on first use by transcriber/recorder.py.

import AVFoundation
import AppKit
import CoreAudio
import CoreImage
import Foundation
import IOKit.pwr_mgt
import ScreenCaptureKit

// MARK: - Output

var eventsFile: FileHandle?

func emit(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
        fflush(stdout)
        if let eventsFile {
            eventsFile.seekToEndOfFile()
            eventsFile.write((text + "\n").data(using: .utf8)!)
            try? eventsFile.synchronize()
        }
    }
}

func fail(_ kind: String, _ message: String) -> Never {
    emit(["event": "error", "kind": kind, "message": message])
    exit(1)
}

// MARK: - Microphones (CoreAudio)

func audioProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                      scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ empty: T) -> T? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope,
                                             mElement: kAudioObjectPropertyElementMain)
    var value = empty
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0) }
    return status == noErr ? value : nil
}

func defaultInputUID() -> String? {
    guard let device: AudioDeviceID = audioProperty(AudioObjectID(kAudioObjectSystemObject),
                                                     kAudioHardwarePropertyDefaultInputDevice, AudioDeviceID(0)),
          device != 0,
          let uid: CFString = audioProperty(device, kAudioDevicePropertyDeviceUID, "" as CFString) else { return nil }
    return uid as String
}

func deviceIsBluetooth(uid: String) -> Bool {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var cfUID = uid as CFString
    var device = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let status = withUnsafeMutablePointer(to: &cfUID) { uidPtr in
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                   UInt32(MemoryLayout<CFString>.size), uidPtr, &size, &device)
    }
    guard status == noErr, device != 0,
          let transport: UInt32 = audioProperty(device, kAudioDevicePropertyTransportType, UInt32(0)) else { return false }
    return transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE
}

func microphones() -> [AVCaptureDevice] {
    AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external], mediaType: .audio,
                                     position: .unspecified).devices
}

// MARK: - list

/// True when Screen & System Audio Recording is allowed for this app (never prompts).
func screenAccess() -> Bool { CGPreflightScreenCaptureAccess() }

func shareableContent() async -> SCShareableContent {
    do {
        return try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    } catch {
        fail("permission_screen", "Screen recording isn't allowed for this app yet (\(error.localizedDescription)).")
    }
}

func displayName(_ id: CGDirectDisplayID) -> String {
    for screen in NSScreen.screens {
        if let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
           number.uint32Value == id { return screen.localizedName }
    }
    return "Display \(id)"
}

func displayOf(_ frame: CGRect, _ displays: [SCDisplay]) -> SCDisplay? {
    displays.max { a, b in a.frame.intersection(frame).area < b.frame.intersection(frame).area }
}

extension CGRect {
    var area: CGFloat { isNull ? 0 : width * height }
}

func microphoneList() -> [[String: Any]] {
    let defaultUID = defaultInputUID()
    return microphones().map {
        ["id": $0.uniqueID, "name": $0.localizedName, "default": $0.uniqueID == defaultUID,
         "bluetooth": deviceIsBluetooth(uid: $0.uniqueID)]
    }
}

/// Without screen access: displays from NSScreen and the running apps (no window titles; the picker chooses).
func listWithoutScreenAccess() {
    let main = CGMainDisplayID()
    let displays: [[String: Any]] = NSScreen.screens.compactMap { screen in
        guard let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else { return nil }
        let id = number.uint32Value
        return ["id": Int(id), "name": screen.localizedName, "width": Int(screen.frame.width),
                "height": Int(screen.frame.height), "main": id == main]
    }
    let me = ProcessInfo.processInfo.processIdentifier
    let apps: [[String: Any]] = NSWorkspace.shared.runningApplications.compactMap { app in
        guard app.activationPolicy == .regular, app.processIdentifier != me, let id = app.bundleIdentifier else { return nil }
        return ["bundle_id": id, "name": app.localizedName ?? id, "windows": [] as [[String: Any]], "display": 0]
    }.sorted { ($0["name"] as! String).lowercased() < ($1["name"] as! String).lowercased() }
    emit(["displays": displays, "apps": apps, "microphones": microphoneList(), "picker": true])
}

func listSources() async {
    guard screenAccess() else { return listWithoutScreenAccess() }
    let content = await shareableContent()
    let me = ProcessInfo.processInfo.processIdentifier
    var apps: [String: [String: Any]] = [:]
    var windows: [String: [[String: Any]]] = [:]
    for window in content.windows {
        guard let app = window.owningApplication, app.processID != me, window.windowLayer == 0,
              window.frame.width >= 200, window.frame.height >= 150, !app.bundleIdentifier.isEmpty else { continue }
        let id = app.bundleIdentifier
        apps[id] = ["bundle_id": id, "name": app.applicationName]
        windows[id, default: []].append([
            "title": window.title ?? "", "width": Int(window.frame.width), "height": Int(window.frame.height),
            "display": Int(displayOf(window.frame, content.displays)?.displayID ?? 0),
        ])
    }
    let appList: [[String: Any]] = apps.values.map { app in
        var entry = app
        let wins = windows[app["bundle_id"] as! String] ?? []
        entry["windows"] = wins
        entry["display"] = (wins.max { ($0["width"] as! Int) * ($0["height"] as! Int) < ($1["width"] as! Int) * ($1["height"] as! Int) })?["display"] ?? 0
        return entry
    }.sorted { ($0["name"] as! String).lowercased() < ($1["name"] as! String).lowercased() }
    let main = CGMainDisplayID()
    let displays: [[String: Any]] = content.displays.map {
        ["id": Int($0.displayID), "name": displayName($0.displayID), "width": $0.width, "height": $0.height,
         "main": $0.displayID == main]
    }
    emit(["displays": displays, "apps": appList, "microphones": microphoneList(), "picker": false])
}

// MARK: - Recording

struct Options {
    var app: String?          // "" = any app, chosen in the picker
    var display: CGDirectDisplayID?
    var audioOnly = false
    var mic: String?
    var out: URL!
    var live: URL?
}

func parseOptions(_ args: [String]) -> Options {
    var options = Options()
    var i = 0
    func value() -> String {
        i += 1
        guard i < args.count else { fail("usage", "Missing value for \(args[i - 1])") }
        return args[i]
    }
    while i < args.count {
        switch args[i] {
        case "--app": options.app = value()
        case "--app-any": options.app = ""
        case "--display": options.display = CGDirectDisplayID(value()) ?? 0
        case "--audio-only": options.audioOnly = true
        case "--mic": options.mic = value()
        case "--out": options.out = URL(fileURLWithPath: value())
        case "--live": options.live = URL(fileURLWithPath: value(), isDirectory: true)
        case "--events": _ = value()  // handled in main
        default: fail("usage", "Unknown option \(args[i])")
        }
        i += 1
    }
    if options.out == nil { fail("usage", "--out is required") }
    if options.app == nil && options.display == nil && !options.audioOnly {
        fail("usage", "Choose --app, --display or --audio-only")
    }
    return options
}

/// Raw 16 kHz mono PCM for the live transcript, aligned to the recording's start: a gap is padded with zeros, so
/// sample n of both files is the same moment.
final class PCMWriter {
    let handle: FileHandle
    var written: Int64 = 0  // samples
    var converter: AVAudioConverter?
    var sourceFormat: AVAudioFormat?
    let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!

    init(_ url: URL) {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        handle = try! FileHandle(forWritingTo: url)
    }

    func append(_ buffer: CMSampleBuffer, at seconds: Double) {
        guard let pcm = buffer.pcmBuffer() else { return }
        if sourceFormat == nil || sourceFormat! != pcm.format {
            sourceFormat = pcm.format
            converter = AVAudioConverter(from: pcm.format, to: target)
        }
        guard let converter else { return }
        let capacity = AVAudioFrameCount(Double(pcm.frameLength) * 16000 / pcm.format.sampleRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var given = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return pcm
        }
        guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData?[0] else { return }
        var data = Data(bytes: samples, count: Int(out.frameLength) * 2)
        let position = Int64(max(0, seconds) * 16000)
        if position > written + 1600 {  // more than 0.1 s missing: pad with silence
            handle.write(Data(count: Int(position - written) * 2))
            written = position
        } else if position + Int64(out.frameLength) <= written - 1600 {
            return  // already covered (overlap after a restart of the mic)
        }
        handle.write(data)
        written += Int64(out.frameLength)
        data.removeAll()
    }

    func close() { try? handle.close() }
}

extension CMSampleBuffer {
    /// The audio as an AVAudioPCMBuffer (copied).
    func pcmBuffer() -> AVAudioPCMBuffer? {
        guard let description = formatDescription,
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description) else { return nil }
        guard let format = AVAudioFormat(streamDescription: asbd) else { return nil }
        let frames = AVAudioFrameCount(numSamples)
        guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return nil }
        buffer.frameLength = frames
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(self, at: 0, frameCount: Int32(frames),
                                                                  into: buffer.mutableAudioBufferList)
        return status == noErr ? buffer : nil
    }

    /// Root-mean-square level 0..1 of the first channel.
    func rms() -> Float {
        guard let buffer = pcmBuffer() else { return 0 }
        let n = Int(buffer.frameLength)
        guard n > 0 else { return 0 }
        var sum: Float = 0
        if let floats = buffer.floatChannelData?[0] {
            for i in 0..<n { sum += floats[i] * floats[i] }
        } else if let ints = buffer.int16ChannelData?[0] {
            for i in 0..<n { let v = Float(ints[i]) / 32768; sum += v * v }
        } else { return 0 }
        return (sum / Float(n)).squareRoot()
    }
}

/// A silent copy of `template`'s format lasting `seconds` (fills gaps in an audio track, e.g. when the mic switches).
func silence(like template: CMSampleBuffer, at time: CMTime, seconds: Double) -> CMSampleBuffer? {
    guard let description = template.formatDescription,
          let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description)?.pointee,
          let format = AVAudioFormat(streamDescription: [asbd]) else { return nil }
    let frames = AVAudioFrameCount(seconds * asbd.mSampleRate)
    guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return nil }
    buffer.frameLength = frames  // AVAudioPCMBuffer starts zeroed
    var sampleBuffer: CMSampleBuffer?
    var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: CMTimeScale(asbd.mSampleRate)),
                                    presentationTimeStamp: time, decodeTimeStamp: .invalid)
    guard CMSampleBufferCreate(allocator: kCFAllocatorDefault, dataBuffer: nil, dataReady: false,
                               makeDataReadyCallback: nil, refcon: nil, formatDescription: description,
                               sampleCount: CMItemCount(frames), sampleTimingEntryCount: 1,
                               sampleTimingArray: &timing, sampleSizeEntryCount: 0, sampleSizeArray: nil,
                               sampleBufferOut: &sampleBuffer) == noErr, let sampleBuffer else { return nil }
    guard CMSampleBufferSetDataBufferFromAudioBufferList(sampleBuffer, blockBufferAllocator: kCFAllocatorDefault,
                                                         blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0,
                                                         bufferList: buffer.audioBufferList) == noErr else { return nil }
    return sampleBuffer
}

/// macOS's window/screen picker: no Screen Recording permission needed, the user's choice is the consent.
final class Picker: NSObject, SCContentSharingPickerObserver {
    private var continuation: CheckedContinuation<SCContentFilter?, Never>?
    private let lock = NSLock()
    var failure: Error?
    /// The user changed what is shared (from the menu bar) during the recording.
    var onChange: ((SCContentFilter, SCStream) -> Void)?

    /// Shows the picker for an app or window (`app`) or a display, and waits for the choice (nil: cancelled).
    func choose(app: Bool) async -> SCContentFilter? {
        await withCheckedContinuation { continuation in
            lock.lock(); self.continuation = continuation; lock.unlock()
            DispatchQueue.main.async {
                let picker = SCContentSharingPicker.shared
                var configuration = SCContentSharingPickerConfiguration()
                configuration.allowedPickerModes = app ? [.singleApplication, .singleWindow] : [.singleDisplay]
                configuration.excludedBundleIDs = [Bundle.main.bundleIdentifier ?? "local.quorum.record"]
                configuration.allowsChangingSelectedContent = true
                picker.defaultConfiguration = configuration
                picker.maximumStreamCount = 1
                picker.add(self)
                picker.isActive = true
                NSApplication.shared.activate(ignoringOtherApps: true)
                picker.present(using: app ? .application : .display)
            }
        }
    }

    private func finish(_ filter: SCContentFilter?) {
        lock.lock(); let waiting = continuation; continuation = nil; lock.unlock()
        waiting?.resume(returning: filter)
    }

    func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        if stream == nil { finish(nil) }
    }

    func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter,
                              for stream: SCStream?) {
        if let stream { onChange?(filter, stream) } else { finish(filter) }
    }

    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        failure = error
        finish(nil)
    }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let options: Options
    let queue = DispatchQueue(label: "quorum.record")
    var stream: SCStream!
    var writer: AVAssetWriter!
    var videoInput: AVAssetWriterInput?
    var systemInput: AVAssetWriterInput!
    var micInput: AVAssetWriterInput!
    var start: CMTime?
    var lastEnd: [String: CMTime] = [:]       // per audio track: where the last buffer ended
    var lastBuffer: [String: CMSampleBuffer] = [:]
    var levels: [String: Float] = ["system": 0, "mic": 0]
    var pcm: [String: PCMWriter] = [:]
    var lastFrameSaved = Date.distantPast
    let ciContext = CIContext()
    var micUID: String?
    var followDefaultMic = true
    var stopping = false
    var sleepAssertion: IOPMAssertionID = 0
    var config: SCStreamConfiguration!
    var picker: Picker?
    var watchApp: String?      // the recorded app's bundle id: stop when it quits
    var sourceInfo: [String: Any] = [:]

    init(_ options: Options) {
        self.options = options
        self.micUID = options.mic
        self.followDefaultMic = options.mic == nil
    }

    /// The filter and the video size from the named app or display (needs screen access).
    func directFilter() async -> (SCContentFilter, Int, Int, Int) {
        let content = await shareableContent()
        let main = CGMainDisplayID()
        var filter: SCContentFilter
        var display: SCDisplay
        if let bundle = options.app {
            let apps = content.applications.filter { $0.bundleIdentifier == bundle }
            guard !apps.isEmpty else { fail("app_not_found", "\(bundle) has no open window.") }
            let wins = content.windows.filter { $0.owningApplication?.bundleIdentifier == bundle && $0.windowLayer == 0 }
            let biggest = wins.max { $0.frame.area < $1.frame.area }
            display = (biggest.flatMap { displayOf($0.frame, content.displays) })
                ?? content.displays.first { $0.displayID == main } ?? content.displays[0]
            filter = SCContentFilter(display: display, including: apps, exceptingWindows: [])
            watchApp = bundle
            sourceInfo = ["kind": "app", "id": bundle]
        } else {
            let wanted = options.display ?? main
            guard let found = content.displays.first(where: { $0.displayID == wanted })
                    ?? content.displays.first(where: { $0.displayID == main }) else {
                fail("display_not_found", "No display to record.")
            }
            display = found
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            sourceInfo = options.audioOnly ? ["kind": "audio"] : ["kind": "display", "id": Int(display.displayID)]
        }
        return (filter, display.width, display.height, Int(display.displayID))
    }

    /// The filter and the video size from macOS's picker (no screen access needed).
    func pickedFilter() async -> (SCContentFilter, Int, Int, Int) {
        let picker = Picker()
        self.picker = picker
        let appMode = options.app != nil
        emit(["event": "picker", "mode": appMode ? "app" : "display"])
        guard let filter = await picker.choose(app: appMode) else {
            if let error = picker.failure {
                fail("picker", "macOS couldn't show the window picker: \(error.localizedDescription)")
            }
            fail("cancelled", "No window or screen was chosen, so nothing is recorded.")
        }
        let scale = Double(filter.pointPixelScale)
        let width = Int(filter.contentRect.width * scale)
        let height = Int(filter.contentRect.height * scale)
        var kind = options.audioOnly ? "audio" : (filter.style == .display ? "display" : (filter.style == .window ? "window" : "app"))
        if appMode, #available(macOS 15.2, *), let app = filter.includedApplications.first {
            watchApp = app.bundleIdentifier
            sourceInfo = ["kind": kind, "id": app.bundleIdentifier, "name": app.applicationName, "picked": true]
        } else {
            if kind == "app" && !appMode { kind = "display" }
            sourceInfo = ["kind": kind, "picked": true]
        }
        picker.onChange = { [weak self] newFilter, stream in
            stream.updateContentFilter(newFilter) { error in
                if let error { emit(["event": "warning", "kind": "picker_change_failed", "message": error.localizedDescription]) }
                else { emit(["event": "warning", "kind": "source_changed", "message": "Now recording what you picked."]) }
            }
            _ = self
        }
        return (filter, max(2, width), max(2, height), 0)
    }

    func run() async {
        let usePicker = !screenAccess() || options.app == ""
        let (filter, fullWidth, fullHeight, displayID) = usePicker ? await pickedFilter() : await directFilter()

        if await !AVCaptureDevice.requestAccess(for: .audio) {
            fail("permission_mic", "Microphone access isn't allowed for this app yet.")
        }
        if micUID == nil { micUID = defaultInputUID() }

        let config = SCStreamConfiguration()
        let scale = options.audioOnly ? 0 : min(1.0, 1920.0 / Double(max(fullWidth, 1)))
        config.width = options.audioOnly ? 2 : max(2, Int(Double(fullWidth) * scale) / 2 * 2)
        config.height = options.audioOnly ? 2 : max(2, Int(Double(fullHeight) * scale) / 2 * 2)
        config.minimumFrameInterval = options.audioOnly ? CMTime(value: 10, timescale: 1) : CMTime(value: 1, timescale: 2)
        config.queueDepth = 4
        config.showsCursor = false
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 1
        config.captureMicrophone = true
        config.microphoneCaptureDeviceID = micUID
        self.config = config

        do {
            try setUpWriter(width: config.width, height: config.height)
        } catch {
            fail("writer", "Can't write \(options.out.path): \(error.localizedDescription)")
        }
        if let live = options.live {
            try? FileManager.default.createDirectory(at: live, withIntermediateDirectories: true)
            pcm["system"] = PCMWriter(live.appendingPathComponent("system.pcm"))
            pcm["mic"] = PCMWriter(live.appendingPathComponent("mic.pcm"))
        }

        stream = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            if !options.audioOnly { try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue) }
            try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
            try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
            try await stream.startCapture()
        } catch {
            let text = error.localizedDescription
            fail(text.lowercased().contains("permission") || text.lowercased().contains("declined")
                 ? "permission_screen" : "capture", "Recording couldn't start: \(text)")
        }
        IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep as CFString,
                                    IOPMAssertionLevel(kIOPMAssertionLevelOn), "Quorum is recording a call" as CFString,
                                    &sleepAssertion)
        writeLayout()
        emit(["event": "started", "file": options.out.path, "mic": micName(micUID) as Any,
              "display": displayID, "video": !options.audioOnly, "source": sourceInfo])
        watch()
    }

    func micName(_ uid: String?) -> String? {
        microphones().first { $0.uniqueID == uid }?.localizedName
    }

    func setUpWriter(width: Int, height: Int) throws {
        try? FileManager.default.removeItem(at: options.out)
        writer = try AVAssetWriter(outputURL: options.out, fileType: .mov)
        writer.movieFragmentInterval = CMTime(value: 10, timescale: 1)
        writer.shouldOptimizeForNetworkUse = false
        if !options.audioOnly {
            let video = AVAssetWriterInput(mediaType: .video, outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
                AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 1_200_000,
                                                  AVVideoMaxKeyFrameIntervalKey: 20],
            ])
            video.expectsMediaDataInRealTime = true
            writer.add(video)
            videoInput = video
        }
        func audio(_ title: String) -> AVAssetWriterInput {
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48000, AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 96000,
            ])
            input.expectsMediaDataInRealTime = true
            let name = AVMutableMetadataItem()
            name.identifier = .quickTimeUserDataTrackName
            name.value = title as NSString
            name.dataType = kCMMetadataBaseDataType_UTF8 as String
            input.metadata = [name]
            writer.add(input)
            return input
        }
        systemInput = audio("System audio")
        micInput = audio("Microphone")
        guard writer.startWriting() else { throw writer.error ?? NSError(domain: "quorum", code: 1) }
    }

    /// <file>.quorum.json: which track is which (the transcriber reads this first).
    func writeLayout() {
        let layout: [String: Any] = ["recorder": "quorum-record", "version": 1, "audio_tracks": ["system", "mic"],
                                     "source": sourceInfo, "video": !options.audioOnly,
                                     "started_at": ISO8601DateFormatter().string(from: Date())]
        let url = URL(fileURLWithPath: options.out.path + ".quorum.json")
        if let data = try? JSONSerialization.data(withJSONObject: layout, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url)
        }
    }

    // SCStreamOutput
    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard !stopping, buffer.isValid, writer.status == .writing else { return }
        switch type {
        case .screen: handleVideo(buffer)
        case .audio: handleAudio(buffer, track: "system")
        case .microphone: handleAudio(buffer, track: "mic")
        @unknown default: break
        }
    }

    func begin(at time: CMTime) {
        if start == nil {
            start = time
            writer.startSession(atSourceTime: time)
        }
    }

    func handleVideo(_ buffer: CMSampleBuffer) {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int, SCFrameStatus(rawValue: raw) == .complete,
              let video = videoInput else { return }
        begin(at: buffer.presentationTimeStamp)
        if video.isReadyForMoreMediaData { video.append(buffer) }
        if let live = options.live, let start, Date().timeIntervalSince(lastFrameSaved) >= 2,
           let pixels = buffer.imageBuffer {
            lastFrameSaved = Date()
            let image = CIImage(cvPixelBuffer: pixels)
            let frames = live.appendingPathComponent("frames", isDirectory: true)
            try? FileManager.default.createDirectory(at: frames, withIntermediateDirectories: true)
            let second = Int((buffer.presentationTimeStamp - start).seconds)
            let tmp = frames.appendingPathComponent(".tmp.jpg")
            if let space = CGColorSpace(name: CGColorSpace.sRGB) {
                try? ciContext.writeJPEGRepresentation(of: image, to: tmp, colorSpace: space, options: [:])
                // frames/000123.jpg = the screen 123 s into the recording (the live draft names speakers from it)
                let dest = frames.appendingPathComponent(String(format: "%06d.jpg", second))
                try? FileManager.default.removeItem(at: dest)
                try? FileManager.default.moveItem(at: tmp, to: dest)
            }
        }
    }

    func handleAudio(_ buffer: CMSampleBuffer, track: String) {
        let pts = buffer.presentationTimeStamp
        begin(at: pts)
        guard let start, pts >= start else { return }
        let input = track == "mic" ? micInput! : systemInput!
        if let end = lastEnd[track], let template = lastBuffer[track] {
            let gap = (pts - end).seconds
            if gap > 0.1, gap < 6 * 3600, let filler = silence(like: template, at: end, seconds: gap) {
                if input.isReadyForMoreMediaData { input.append(filler) }
            }
        } else if (pts - start).seconds > 0.1, let filler = silence(like: buffer, at: start, seconds: (pts - start).seconds) {
            if input.isReadyForMoreMediaData { input.append(filler) }  // keep both tracks starting at 0
        }
        if input.isReadyForMoreMediaData { input.append(buffer) }
        lastEnd[track] = pts + buffer.duration
        lastBuffer[track] = buffer
        levels[track] = max(levels[track] ?? 0, buffer.rms())
        pcm[track]?.append(buffer, at: (pts - start).seconds)
    }

    // SCStreamDelegate
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        let text = error.localizedDescription
        emit(["event": "warning", "kind": "capture_stopped", "message": text])
        finish(reason: "capture_stopped")
    }

    /// Levels every second, disk space every 10 s, the meeting app quitting, mic changes, signals.
    func watch() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        var ticks = 0
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [self] in
            ticks += 1
            let elapsed = start.map { (CMClockGetTime(CMClockGetHostTimeClock()) - $0).seconds } ?? 0
            emit(["event": "levels", "system": Double(levels["system"] ?? 0), "mic": Double(levels["mic"] ?? 0),
                  "elapsed": elapsed.rounded()])
            levels = ["system": 0, "mic": 0]
            if ticks % 10 == 0, let free = try? options.out.deletingLastPathComponent()
                .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
                .volumeAvailableCapacityForImportantUsage, free < 2_000_000_000 {
                emit(["event": "warning", "kind": "disk_full", "message": "Less than 2 GB free: recording stopped."])
                finish(reason: "disk_full")
            }
        }
        timer.resume()
        retained.append(timer)

        if let bundle = watchApp {
            NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didTerminateApplicationNotification,
                                                              object: nil, queue: nil) { [self] note in
                let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
                if app?.bundleIdentifier == bundle {
                    queue.async { self.finish(reason: "app_quit") }
                }
            }
        }

        // The Mac's input changed (a headset connected or disconnected): follow it, if following.
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, queue) { [self] _, _ in
            guard followDefaultMic, let uid = defaultInputUID(), uid != micUID else { return }
            switchMic(to: uid)
        }
        // A chosen mic that disappears: fall back to the Mac's input.
        NotificationCenter.default.addObserver(forName: AVCaptureDevice.wasDisconnectedNotification, object: nil,
                                               queue: nil) { [self] note in
            guard let device = note.object as? AVCaptureDevice else { return }
            queue.async {
                if device.uniqueID == self.micUID, let uid = defaultInputUID(), uid != device.uniqueID {
                    self.followDefaultMic = true
                    self.switchMic(to: uid)
                }
            }
        }

        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: queue)
            source.setEventHandler { [self] in finish(reason: "stopped") }
            source.resume()
            retained.append(source)
        }
    }

    var retained: [AnyObject] = []

    func switchMic(to uid: String) {
        guard !stopping else { return }
        micUID = uid
        config.microphoneCaptureDeviceID = uid
        let name = micName(uid) ?? uid
        stream.updateConfiguration(config) { error in
            if let error {
                emit(["event": "warning", "kind": "mic_switch_failed", "message": error.localizedDescription])
            } else {
                emit(["event": "warning", "kind": "mic_switched", "mic": name,
                      "message": "Microphone switched to \(name)."])
            }
        }
    }

    func finish(reason: String) {
        guard !stopping else { return }
        stopping = true
        let done = DispatchGroup()
        done.enter()
        stream?.stopCapture { _ in done.leave() }
        _ = done.wait(timeout: .now() + 3)
        if picker != nil { DispatchQueue.main.async { SCContentSharingPicker.shared.isActive = false } }
        videoInput?.markAsFinished()
        systemInput?.markAsFinished()
        micInput?.markAsFinished()
        for writer in pcm.values { writer.close() }
        if sleepAssertion != 0 { IOPMAssertionRelease(sleepAssertion) }
        let elapsed = start.map { ((lastEnd["system"] ?? lastEnd["mic"] ?? $0) - $0).seconds } ?? 0
        if start == nil {
            writer.cancelWriting()
            emit(["event": "stopped", "reason": reason, "file": NSNull(), "seconds": 0])
            exit(0)
        }
        writer.finishWriting { [self] in
            let ok = writer.status == .completed
            emit(["event": "stopped", "reason": reason, "file": options.out.path, "seconds": elapsed.rounded(),
                  "complete": ok, "error": ok ? NSNull() : (writer.error?.localizedDescription ?? "unknown") as Any])
            exit(ok ? 0 : 1)
        }
    }
}

// MARK: - main

var args = Array(CommandLine.arguments.dropFirst())
if let i = args.firstIndex(of: "--events"), i + 1 < args.count {
    let path = args[i + 1]
    if !FileManager.default.fileExists(atPath: path) { FileManager.default.createFile(atPath: path, contents: nil) }
    eventsFile = FileHandle(forWritingAtPath: path)
    args.removeSubrange(i...(i + 1))
}
emit(["event": "launched", "pid": Int(ProcessInfo.processInfo.processIdentifier)])
guard let command = args.first else {
    fail("usage", "quorum-record list | start (--app <bundle-id> | --app-any | --display <id> | --audio-only) [--mic <uid>] --out <file.mov> [--live <dir>]")
}
_ = NSApplication.shared  // window server connection for ScreenCaptureKit and NSWorkspace notifications
switch command {
case "list":
    Task {
        await listSources()
        exit(0)
    }
case "start":
    let recorder = Recorder(parseOptions(Array(args.dropFirst())))
    Task { await recorder.run() }
default:
    fail("usage", "Unknown command \(command)")
}
RunLoop.main.run()
