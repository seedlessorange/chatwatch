// Draws Chatwatch's app icon: a ring gauge (the menu bar ring, grown up) around a chat bubble.
// Run: swiftc -parse-as-library -o /tmp/make_icon mac/make_icon.swift && /tmp/make_icon out.png
// mac/build.sh turns the 1024 px PNG into AppIcon.icns; the .icns is committed, so building
// the app does not need this.

import AppKit
import SwiftUI

struct Icon: View {
    var body: some View {
        ZStack {
            // the tile: macOS icon grid, 824 of 1024 with a continuous corner
            RoundedRectangle(cornerRadius: 185, style: .continuous)
                .fill(LinearGradient(colors: [Color(red: 0.17, green: 0.18, blue: 0.22),
                                              Color(red: 0.07, green: 0.075, blue: 0.09)],
                                     startPoint: .top, endPoint: .bottom))
                .overlay(
                    RoundedRectangle(cornerRadius: 185, style: .continuous)
                        .stroke(LinearGradient(colors: [.white.opacity(0.18), .white.opacity(0.02)],
                                               startPoint: .top, endPoint: .bottom), lineWidth: 3)
                )
                .frame(width: 824, height: 824)
                .shadow(color: .black.opacity(0.35), radius: 18, y: 10)

            // the gauge track
            Circle()
                .stroke(Color.white.opacity(0.08), lineWidth: 78)
                .frame(width: 540, height: 540)

            // the gauge, warm and glowing
            Circle()
                .trim(from: 0, to: 0.72)
                .stroke(AngularGradient(colors: [Color(red: 1.0, green: 0.78, blue: 0.35),
                                                 Color(red: 1.0, green: 0.52, blue: 0.22),
                                                 Color(red: 0.98, green: 0.30, blue: 0.30)],
                                        center: .center, startAngle: .degrees(0), endAngle: .degrees(260)),
                        style: StrokeStyle(lineWidth: 78, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .frame(width: 540, height: 540)
                .shadow(color: Color(red: 1.0, green: 0.45, blue: 0.2).opacity(0.55), radius: 28)

            // a chat bubble in the middle
            Bubble()
                .fill(Color.white.opacity(0.94))
                .frame(width: 262, height: 214)
                .offset(y: 6)
            HStack(spacing: 26) {
                ForEach(0..<3) { _ in Circle().fill(Color(red: 0.10, green: 0.11, blue: 0.13)).frame(width: 34, height: 34) }
            }
            .offset(y: -10)
        }
        .frame(width: 1024, height: 1024)
    }
}

/// A rounded speech bubble with a small tail at the bottom left.
struct Bubble: Shape {
    func path(in r: CGRect) -> Path {
        var p = Path()
        let body = CGRect(x: r.minX, y: r.minY, width: r.width, height: r.height * 0.78)
        p.addRoundedRect(in: body, cornerSize: CGSize(width: 62, height: 62), style: .continuous)
        // drawn in the same direction as the body, so the overlap stays filled
        p.move(to: CGPoint(x: r.minX + r.width * 0.46, y: body.maxY - 30))
        p.addLine(to: CGPoint(x: r.minX + r.width * 0.12, y: r.maxY))
        p.addLine(to: CGPoint(x: r.minX + r.width * 0.18, y: body.maxY - 30))
        p.closeSubpath()
        return p
    }
}

@main
enum Main {
    @MainActor static func main() {
        let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
        let r = ImageRenderer(content: Icon())
        r.scale = 1
        guard let cg = r.cgImage else { fatalError("render failed") }
        let rep = NSBitmapImageRep(cgImage: cg)
        try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
    }
}
