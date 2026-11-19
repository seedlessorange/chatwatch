// Chatwatch: a menu bar app for chatwatch. It shows the plan limit you are closest to
// in the menu bar and, when clicked, every open Claude Code chat with what it is using.
// It reads the chatwatch service on http://127.0.0.1:7731, which does the measuring
// and the alerts; this app only displays and sends the close buttons.
//
// Build: mac/build.sh   (needs Xcode's command line tools)
// Snapshot of the panel, for checking the layout: Chatwatch --snapshot out.png

import AppKit
import SwiftUI
import UserNotifications

let base = URL(string: "http://127.0.0.1:" + (ProcessInfo.processInfo.environment["CHATWATCH_PORT"] ?? "7731"))!
let tokenPath = (NSHomeDirectory() as NSString).appendingPathComponent(".claude/chatwatch-token")

// MARK: data from /api/chats

struct Job: Codable { let state: String?; let msg: String?; let path: String? }

struct Chat: Codable, Identifiable {
    let pid: Int
    let title: String
    let folder: String
    let family: String
    let busy: Bool
    let idle_min: Int
    let ctx: Int
    let hour_usd: Double
    let hour_pct: Double?
    let day_pct: Double?
    let wake_pct: Double?
    let wake_usd: Double
    let hour_share: Int
    let rate_pct_h: Double?
    let rate_usd_h: Double?
    let subs_active: Int?
    let advice: [[String]]
    let job: Job?
    var id: Int { pid }
}

struct Meter: Codable {
    let label: String
    let pct: Double
    let resets: Double?
    let pct_per_usd: Double?
    let hour_pct: Double?
    let full_at: Double?
}

struct AlertItem: Codable, Identifiable {
    let id: String
    let pid: Int
    let kind: String
    let title: String
    let text: String
    let busy: Bool
    let notify: Bool
}

struct Plan: Codable { let why: String?; let unit: String?; let meters: [String: Meter] }
struct Thresholds: Codable { let ctx: Int }
struct Payload: Codable {
    let chats: [Chat]
    let closed: [String: Job]?
    let plan: Plan
    let thresholds: Thresholds
    let alerts: [AlertItem]?
}

func log(_ s: String) {
    let p = (NSHomeDirectory() as NSString).appendingPathComponent(".claude/chatwatch-app.log")
    let line = "\(Date()) \(s)\n"
    if let h = FileHandle(forWritingAtPath: p) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
    else { try? line.write(toFile: p, atomically: true, encoding: .utf8) }
}

// MARK: model

@MainActor
final class Model: ObservableObject {
    static let shared = Model()
    @Published var data: Payload?
    @Published var error: String?
    @Published var updated = Date()
    private var timer: Timer?

    init(poll: Bool = true) {
        guard poll else { return }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in
            Task { @MainActor in self.refresh() }
        }
    }

    func refresh() {
        Task {
            do {
                var req = URLRequest(url: base.appendingPathComponent("api/chats"))
                req.setValue("1", forHTTPHeaderField: "X-Chatwatch-App")
                let (d, _) = try await URLSession.shared.data(for: req)
                self.data = try JSONDecoder().decode(Payload.self, from: d)
                self.error = nil
                self.updated = Date()
                self.deliver()
            } catch {
                self.error = "The chatwatch service is not running. Run install.sh in the chatwatch folder."
            }
        }
    }

    func post(_ path: String, _ body: [String: Any]) {
        guard let token = try? String(contentsOfFile: tokenPath, encoding: .utf8) else { return }
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(token.trimmingCharacters(in: .whitespacesAndNewlines), forHTTPHeaderField: "X-Token")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        Task {
            _ = try? await URLSession.shared.data(for: req)
            self.refresh()
        }
    }

    func close(_ pid: Int, handoff: Bool) { post("api/close", ["pid": pid, "handoff": handoff]) }
    func snooze(_ id: String, hours: Double = 1) { post("api/snooze", ["id": id, "hours": hours]) }
    func copyModelSwitch() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("/model opus", forType: .string)
    }

    var alerts: [AlertItem] { data?.alerts ?? [] }
    var urgent: Int { alerts.filter { $0.notify }.count }

    /// Posts a notification for each new fast alert. The same alert repeats after an
    /// hour if it is still true (six hours for Fable).
    func deliver() {
        let d = UserDefaults.standard
        var sent = d.dictionary(forKey: "notified") as? [String: Double] ?? [:]
        let now = Date().timeIntervalSince1970
        for a in alerts where a.notify {
            let gap: Double = a.kind == "fable" ? 6 * 3600 : 3600
            if now - (sent[a.id] ?? 0) < gap { continue }
            sent[a.id] = now
            let c = UNMutableNotificationContent()
            c.title = (a.kind == "burn" ? "Burning fast: " : a.kind == "jump" ? "Context jumping: " : "On Fable: ") + a.title
            c.body = a.text
            c.sound = .default
            c.categoryIdentifier = a.kind == "fable" ? "fable" : "chat"
            c.userInfo = ["pid": a.pid, "id": a.id]
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: a.id + "@\(Int(now))", content: c, trigger: nil)) { err in
                log("notify \(a.id) \(err.map { "failed: \($0)" } ?? "ok")")
            }
        }
        sent = sent.filter { now - $0.value < 86400 }
        d.set(sent, forKey: "notified")
    }

    /// The meter per-chat shares are counted against, or the fullest one.
    var meter: Meter? {
        guard let p = data?.plan else { return nil }
        if let u = p.unit, let m = p.meters[u] { return m }
        return p.meters.values.max { $0.pct < $1.pct }
    }

    var warning: Bool {
        guard let m = meter else { return false }
        if m.pct >= 90 { return true }
        if let f = m.full_at, let r = m.resets, f < r { return true }
        return false
    }
}

// MARK: formatting

func pct(_ n: Double?) -> String {
    guard let n = n else { return "" }
    if n == 0 { return "0%" }
    if n > 0 && n < 0.01 { return "<0.01%" }
    return n < 1 ? String(format: "%.2f%%", n) : String(format: "%.1f%%", n)
}
func usd(_ n: Double) -> String { n >= 100 ? String(format: "$%.0f", n) : String(format: "$%.2f", n) }
func kTok(_ n: Int) -> String { n >= 1_000_000 ? String(format: "%.1fM", Double(n) / 1e6) : "\(n / 1000)K" }
func when(_ t: Double) -> String {
    let f = DateFormatter()
    f.dateFormat = "EEE h:mm a"
    return f.string(from: Date(timeIntervalSince1970: t))
}
func idle(_ m: Int) -> String { m < 60 ? "\(m) min" : m < 600 ? String(format: "%.1f h", Double(m) / 60) : "\(m / 60) h" }

// MARK: menu bar label

/// A ring filled to the meter's percentage, then the number, drawn as one image so
/// the menu bar keeps it on one line. Template (follows the menu bar's colour)
/// unless something needs attention.
func labelImage(pct: Double?, warning: Bool, urgent: Int = 0) -> NSImage {
    let text = pct.map { String(format: "%.0f%%", $0) } ?? "--"
    let badge = urgent > 0 ? " \(urgent)" : ""
    let font = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .medium)
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.black]
    let tw = (text as NSString).size(withAttributes: attrs).width
    let bw: CGFloat = urgent > 0 ? 16 + CGFloat(badge.count - 2) * 7 : 0
    let size = NSSize(width: 14 + 5 + ceil(tw) + (urgent > 0 ? bw + 4 : 0), height: 18)
    let img = NSImage(size: size, flipped: false) { _ in
        let ring = NSRect(x: 1, y: 2, width: 13, height: 13)
        let color: NSColor = warning ? .systemRed : .black
        color.withAlphaComponent(0.3).setStroke()
        let bg = NSBezierPath(ovalIn: ring.insetBy(dx: 1, dy: 1))
        bg.lineWidth = 2
        bg.stroke()
        if let p = pct {
            let arc = NSBezierPath()
            let c = NSPoint(x: ring.midX, y: ring.midY)
            arc.appendArc(withCenter: c, radius: 5.5, startAngle: 90, endAngle: 90 - 360 * CGFloat(min(p, 100) / 100), clockwise: true)
            arc.lineWidth = 2
            arc.lineCapStyle = .round
            color.setStroke()
            arc.stroke()
        }
        var a = attrs
        a[.foregroundColor] = warning ? NSColor.systemRed : NSColor.black
        (text as NSString).draw(at: NSPoint(x: 19, y: 1), withAttributes: a)
        if urgent > 0 {
            // a red pill with the number of chats that need a look
            let r = NSRect(x: 19 + ceil(tw) + 4, y: 2, width: bw, height: 14)
            NSColor.systemRed.setFill()
            NSBezierPath(roundedRect: r, xRadius: 7, yRadius: 7).fill()
            let n = "\(urgent)" as NSString
            let ba: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 10, weight: .bold), .foregroundColor: NSColor.white]
            let ns = n.size(withAttributes: ba)
            n.draw(at: NSPoint(x: r.midX - ns.width / 2, y: r.midY - ns.height / 2), withAttributes: ba)
        }
        return true
    }
    img.isTemplate = !warning && urgent == 0
    return img
}

// MARK: panel

struct PaceBar: View {
    let m: Meter
    var body: some View {
        GeometryReader { g in
            let w = g.size.width
            let used = w * CGFloat(min(m.pct, 100) / 100)
            let ahead = w * CGFloat(min(max(0, 100 - m.pct), m.hour_pct ?? 0) / 100)
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.1))
                Capsule().fill(m.pct >= 90 ? Color.red : m.pct >= 75 ? Color.orange : Color.green)
                    .frame(width: used)
                Rectangle().fill(Color.orange.opacity(0.45)).frame(width: ahead).offset(x: used)
            }
            .clipShape(Capsule())
        }
        .frame(height: 6)
    }
}

struct MeterView: View {
    let m: Meter
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(String(format: "%.0f%%", m.pct)).font(.system(size: 28, weight: .semibold)).monospacedDigit()
                Text("of your \(m.label)").foregroundStyle(.secondary)
                Spacer()
                if let r = m.resets { Text("resets \(when(r))").font(.caption).foregroundStyle(.secondary) }
            }
            PaceBar(m: m)
            if let h = m.hour_pct {
                let before = (m.full_at ?? .infinity) < (m.resets ?? 0)
                Text("Last hour \(pct(h))" + (before ? ". At that pace you hit the limit \(when(m.full_at!)), before the reset." : "."))
                    .font(.caption)
                    .foregroundStyle(before ? Color.red : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Per-chat shares start once this meter has moved a few points.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct ChatRow: View {
    @EnvironmentObject var model: Model
    let c: Chat
    let ctxAlert: Int
    @State private var confirming = false
    @State private var hover = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(c.title).font(.system(size: 13, weight: .medium)).lineLimit(1)
                    HStack(spacing: 6) {
                        Text(c.family.isEmpty ? "?" : c.family)
                            .font(.system(size: 10, weight: .semibold))
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .overlay(Capsule().stroke(c.family == "fable" ? Color.purple : Color.secondary, lineWidth: 1))
                            .foregroundStyle(c.family == "fable" ? Color.purple : Color.secondary)
                        Text(c.busy ? "working" : "idle \(idle(c.idle_min))")
                            .foregroundStyle(c.busy ? Color.green : Color.secondary)
                        Text(c.folder).foregroundStyle(.secondary).lineLimit(1)
                        if let n = c.subs_active, n > 0 {
                            Text("\(n) subagents").foregroundStyle(.orange)
                        }
                    }
                    .font(.caption)
                }
                Spacer(minLength: 4)
                VStack(alignment: .trailing, spacing: 1) {
                    Text(c.rate_pct_h != nil ? pct(c.rate_pct_h) + "/h" : usd(c.rate_usd_h ?? 0) + "/h")
                        .font(.system(size: 13, weight: .semibold)).monospacedDigit()
                    Text("now").font(.caption2).foregroundStyle(.secondary)
                }
                .frame(width: 66, alignment: .trailing)
                VStack(alignment: .trailing, spacing: 1) {
                    Text(c.hour_pct != nil ? pct(c.hour_pct) : usd(c.hour_usd))
                        .font(.system(size: 13, weight: .semibold)).monospacedDigit()
                    Text("last hour").font(.caption2).foregroundStyle(.secondary)
                }
                VStack(alignment: .trailing, spacing: 1) {
                    Text(kTok(c.ctx)).font(.system(size: 13, weight: .semibold)).monospacedDigit()
                        .foregroundStyle(c.ctx > ctxAlert ? Color.orange : Color.primary)
                    Text("context").font(.caption2).foregroundStyle(.secondary)
                }
                .frame(width: 52, alignment: .trailing)
            }
            if let a = c.advice.first, a.count > 1 {
                Text(a[1]).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let j = c.job, let msg = j.msg {
                Text(msg).font(.caption).foregroundStyle(j.state == "error" ? Color.red : Color.secondary)
            } else if confirming {
                HStack(spacing: 6) {
                    Button("Handoff and close") { model.close(c.pid, handoff: true); confirming = false }
                        .buttonStyle(.borderedProminent).controlSize(.small)
                    Button("Close now") { model.close(c.pid, handoff: false); confirming = false }
                        .controlSize(.small)
                    Button("Cancel") { confirming = false }.controlSize(.small).buttonStyle(.borderless)
                    Spacer()
                }
                if c.busy {
                    Text("It is working right now. Closing stops it mid-task.").font(.caption).foregroundStyle(.red)
                }
            } else if hover {
                HStack {
                    Spacer()
                    Button("Close...") { confirming = true }.controlSize(.small).buttonStyle(.borderless)
                }
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
        .background(RoundedRectangle(cornerRadius: 8).fill(hover ? Color.primary.opacity(0.05) : Color.clear))
        .onHover { hover = $0 }
    }
}

struct AlertCard: View {
    @EnvironmentObject var model: Model
    let a: AlertItem

    var color: Color { a.kind.hasPrefix("fable") ? .purple : a.kind == "burn" ? .red : .orange }
    var icon: String { a.kind == "burn" ? "flame.fill" : a.kind == "jump" ? "arrow.up.right" : "sparkles" }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: icon).foregroundStyle(color)
                Text(a.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
            }
            Text(a.text).font(.caption).fixedSize(horizontal: false, vertical: true)
            if a.busy {
                Text("Working right now: closing stops it mid-task.").font(.caption2).foregroundStyle(.secondary)
            }
            HStack(spacing: 6) {
                if a.kind.hasPrefix("fable") {
                    Button("Copy /model opus") { model.copyModelSwitch() }
                }
                Button("Handoff and close") { model.close(a.pid, handoff: true) }.buttonStyle(.borderedProminent)
                Button("Close now") { model.close(a.pid, handoff: false) }
                Spacer()
                Button("Snooze 1 h") { model.snooze(a.id) }.buttonStyle(.borderless)
            }
            .controlSize(.small)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(color.opacity(0.08)))
    }
}

struct Panel: View {
    @EnvironmentObject var model: Model
    var scrolls = true

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let err = model.error, model.data == nil {
                Text(err).font(.callout).foregroundStyle(.secondary).padding(.vertical, 20)
            } else if let d = model.data {
                if d.plan.meters.isEmpty {
                    Text("Plan limits unavailable\(d.plan.why.map { ": " + $0 } ?? ""). Showing API-rate dollars.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    ForEach(["five_hour", "week"].filter { d.plan.meters[$0] != nil }, id: \.self) { k in
                        MeterView(m: d.plan.meters[k]!)
                    }
                }
                if !model.alerts.isEmpty {
                    Divider()
                    Text("Needs attention").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                    ForEach(model.alerts) { a in AlertCard(a: a) }
                }
                Divider()
                HStack {
                    Text("\(d.chats.count) open chats, \(d.chats.filter { $0.busy }.count) working")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if let u = d.plan.unit, let m = d.plan.meters[u], d.plan.meters.count > 1 {
                        Text("shares of your \(m.label)").font(.caption).foregroundStyle(.secondary)
                    }
                }
                let rows = VStack(spacing: 2) {
                    ForEach(d.chats) { c in ChatRow(c: c, ctxAlert: d.thresholds.ctx) }
                    ForEach(Array((d.closed ?? [:]).values.enumerated()), id: \.offset) { _, j in
                        HStack {
                            Text(j.msg ?? "").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            if let p = j.path, !p.isEmpty {
                                Button("Open handoff") { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
                                    .controlSize(.small)
                            }
                        }
                        .padding(.horizontal, 10)
                    }
                }
                if scrolls {
                    // a ScrollView has no height of its own in the menu bar panel; give it one
                    ScrollView { rows }
                        .frame(height: min(CGFloat(d.chats.count) * 64 + 16, model.alerts.isEmpty ? 440 : 300))
                } else {
                    rows
                }
            } else {
                ProgressView().frame(maxWidth: .infinity).padding()
            }
            Divider()
            HStack {
                Button("Open full page") { NSWorkspace.shared.open(base) }.buttonStyle(.borderless)
                Spacer()
                Text("updated \(model.updated.formatted(date: .omitted, time: .shortened))")
                    .font(.caption2).foregroundStyle(.tertiary)
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.borderless)
            }
            .font(.caption)
        }
        .padding(14)
        .frame(width: 400)
    }
}

// MARK: app

/// Opening Chatwatch from Spotlight, Launchpad or Applications shows the panel as a
/// normal window, so it is reachable even when the menu bar icon is hidden.
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow?

    func setUpNotifications() {
        let c = UNUserNotificationCenter.current()
        c.delegate = self
        let handoff = UNNotificationAction(identifier: "handoff", title: "Handoff and close", options: [.destructive])
        let close = UNNotificationAction(identifier: "close", title: "Close now", options: [.destructive])
        let snooze = UNNotificationAction(identifier: "snooze", title: "Snooze 1 hour", options: [])
        let copy = UNNotificationAction(identifier: "copy", title: "Copy /model opus", options: [])
        c.setNotificationCategories([
            UNNotificationCategory(identifier: "chat", actions: [handoff, close, snooze], intentIdentifiers: []),
            UNNotificationCategory(identifier: "fable", actions: [copy, handoff, snooze], intentIdentifiers: []),
        ])
        c.requestAuthorization(options: [.alert, .sound, .badge]) { ok, err in
            log("notifications allowed: \(ok) \(err.map { "\($0)" } ?? "")")
        }
    }

    // show banners even while the app is in front
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let info = r.notification.request.content.userInfo
        let pid = info["pid"] as? Int ?? 0
        let id = info["id"] as? String ?? ""
        let action = r.actionIdentifier
        log("action \(action) on \(id)")
        Task { @MainActor in
            let m = Model.shared
            switch action {
            case "handoff": m.close(pid, handoff: true)
            case "close": m.close(pid, handoff: false)
            case "snooze": m.snooze(id)
            case "copy": m.copyModelSwitch()
            default: self.showWindow()
            }
            done()
        }
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        setUpNotifications()
        // the login item passes --background; a launch by hand shows the window
        if !CommandLine.arguments.contains("--background") { showWindow() }
    }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow()
        return false
    }

    @MainActor func showWindow() {
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 600),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            w.title = "Chatwatch"
            w.isReleasedWhenClosed = false
            w.contentView = NSHostingView(rootView: Panel().environmentObject(Model.shared))
            w.center()
            window = w
        }
        Model.shared.refresh()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}

struct ChatwatchApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject var model = Model.shared
    var body: some Scene {
        MenuBarExtra {
            Panel().environmentObject(model).onAppear { model.refresh() }
        } label: {
            Image(nsImage: labelImage(pct: model.meter?.pct, warning: model.warning, urgent: model.urgent))
        }
        .menuBarExtraStyle(.window)
    }
}

@main
enum Main {
    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
            MainActor.assumeIsolated { snapshot(to: args[i + 1]) }
            return
        }
        // first run: sit near the clock, where the notch cannot hide the icon
        // (distance from the right edge of the screen, the way macOS stores it)
        let key = "NSStatusItem Preferred Position Item-0"
        if UserDefaults.standard.object(forKey: key) == nil { UserDefaults.standard.set(240.0, forKey: key) }
        ChatwatchApp.main()
    }

    /// Renders the panel with live data to a PNG and exits: a way to check the layout
    /// without clicking the menu bar.
    @MainActor static func snapshot(to path: String) {
        let model = Model(poll: false)
        let sem = DispatchSemaphore(value: 0)
        var payload: Payload?
        URLSession.shared.dataTask(with: base.appendingPathComponent("api/chats")) { d, _, _ in
            if let d = d { payload = try? JSONDecoder().decode(Payload.self, from: d) }
            sem.signal()
        }.resume()
        sem.wait()
        model.data = payload
        if payload == nil { model.error = "The chatwatch service is not running." }
        for dark in [false, true] {
            let view = Panel(scrolls: false).environmentObject(model)
                .background(Color(nsColor: .windowBackgroundColor))
                .environment(\.colorScheme, dark ? .dark : .light)
            let r = ImageRenderer(content: view)
            r.scale = 2
            if let img = r.nsImage, let tiff = img.tiffRepresentation,
               let rep = NSBitmapImageRep(data: tiff), let png = rep.representation(using: .png, properties: [:]) {
                let out = dark ? path.replacingOccurrences(of: ".png", with: "-dark.png") : path
                try? png.write(to: URL(fileURLWithPath: out))
            }
        }
        let label = labelImage(pct: model.meter?.pct, warning: model.warning)
        if let tiff = label.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: path.replacingOccurrences(of: ".png", with: "-label.png")))
        }
    }
}
