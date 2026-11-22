// On-device text recognition for meeting video frames (Apple Vision framework, no network).
//
// Usage: quorum-ocr <image> [<image> ...]
// Prints one JSON object per image, one per line:
//   {"path": "...", "width": 1920, "height": 1080,
//    "lines": [{"text": "Priya Shah", "confidence": 0.98, "box": [x, y, w, h]}]}
// Boxes are normalised 0..1 with the origin at the TOP-left (Vision's is bottom-left).
// Built by scripts/setup_local_ai.sh, or on first use by transcriber/screen_names.py.

import Foundation
import Vision
import ImageIO

func recognise(_ path: String) -> [String: Any] {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        return ["path": path, "error": "unreadable image"]
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false  // names are not dictionary words
    request.minimumTextHeight = 0.008
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return ["path": path, "error": "\(error)"]
    }
    var lines: [[String: Any]] = []
    for observation in request.results ?? [] {
        guard let best = observation.topCandidates(1).first else { continue }
        let b = observation.boundingBox
        lines.append([
            "text": best.string,
            "confidence": Double(best.confidence),
            "box": [Double(b.minX), Double(1 - b.maxY), Double(b.width), Double(b.height)],
        ])
    }
    return ["path": path, "width": image.width, "height": image.height, "lines": lines]
}

for path in CommandLine.arguments.dropFirst() {
    let result = recognise(path)
    if let data = try? JSONSerialization.data(withJSONObject: result, options: []),
       let text = String(data: data, encoding: .utf8) {
        print(text)
        fflush(stdout)
    }
}
