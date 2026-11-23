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
    let sid: String?
    /// A link that brings this chat's VS Code tab forward; empty for chats in a terminal.
    let jump: String?
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
    let waste_pct_h: Double?
    let waste_usd_h: Double?
    let waste: Waste?
    let advice: [[String]]
    let cause: Cause?
    let job: Job?
    var id: Int { pid }
}

struct Waste: Codable { let share: Double; let steps: Int }
struct Cause: Codable, Hashable { let kind: String; let pill: String; let text: String; let fix: String? }
struct Scoped: Codable { let label: String; let pct: Double; let resets: Double? }
struct SettingCheck: Codable, Hashable { let label: String; let ok: Bool }
struct SettingsInfo: Codable { let checks: [SettingCheck]; let advice: String }

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
    let reasons: [String]?
    let fix: String?
    let subs_active: Int?
    let causes: [Cause]?
    let waste_pct_h: Double?
    let waste_usd_h: Double?
    let share: Double?
    let cold_in_min: Int?
    let cold_cost_pct: Double?
    let cold_cost_usd: Double?
    let sid: String?
    let jump: String?
    /// For a finished alert: big enough that a handoff into a new chat beats Continue.
    let handoff_ok: Bool?
    /// For a cold alert: done, waiting, loose or unclear, read from the chat's last reply.
    let state: String?
    let closing: String?
    /// The causes that come with a message to paste into the chat.
    var fixes: [Cause] { (causes ?? []).filter { !($0.fix ?? "").isEmpty } }
}

struct Plan: Codable {
    let why: String?
    let unit: String?
    let meters: [String: Meter]
    let plan: String?
    let scoped: [Scoped]?
    let session_expected: Bool?
    let settings: SettingsInfo?
}
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
    /// Whether macOS lets this app post notifications (nil until first asked).
    @Published var notifyOK: Bool?
    /// What the last test notification's button did.
    @Published var testResult: String?
    private var timer: Timer?

    init(poll: Bool = true) {
        guard poll else { return }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in
            Task { @MainActor in self.refresh() }
        }
    }

    func checkNotify() {
        UNUserNotificationCenter.current().getNotificationSettings { s in
            let ok = s.authorizationStatus == .authorized || s.authorizationStatus == .provisional
            Task { @MainActor in
                if self.notifyOK != ok { log("notifications allowed now: \(ok)") }
                self.notifyOK = ok
            }
        }
    }

    func refresh() {
        checkNotify()
        Task {
            do {
                var req = URLRequest(url: base.appendingPathComponent("api/chats"))
                req.setValue("1", forHTTPHeaderField: "X-Chatwatch-App")
                // while macOS blocks this app's notifications, the server sends the alerts itself
                req.setValue(self.notifyOK == true ? "1" : "0", forHTTPHeaderField: "X-Chatwatch-Notify")
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

    func close(_ pid: Int, handoff: Bool, reopen: Bool = false) {
        post("api/close", ["pid": pid, "handoff": handoff, "reopen": reopen])
    }
    /// A VS Code chat that is already open takes no text from outside, so this opens it with
    /// "continue" on the clipboard: paste and press Enter.
    func continueChat(_ sid: String?) {
        copy("continue")
        jump(sid)
    }
    func snooze(_ id: String, hours: Double = 1) { post("api/snooze", ["id": id, "hours": hours]) }
    /// Brings the chat's VS Code window and tab to the front (the server picks the window).
    func jump(_ sid: String?) {
        guard let s = sid, !s.isEmpty else { return }
        post("api/jump", ["sid": s])
    }
    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
    func copyModelSwitch() { copy("/model opus") }

    /// A harmless notification, alternating between the two button sets, to check that notifications
    /// show and that each button reaches the app. Their buttons never touch a chat.
    private var testCount = 0
    func sendTest() {
        testResult = nil
        // one at a time, alternating, so macOS does not stack one under the other
        let kinds = [("chat", "Test: a chat wasting tokens"), ("fable", "Test: a chat on Fable")]
        testCount += 1
        for (cat, title) in [kinds[(testCount - 1) % 2]] {
            let c = UNMutableNotificationContent()
            c.title = title
            c.body = "A test from Chatwatch. Its buttons do nothing to your chats."
            c.categoryIdentifier = cat
            c.userInfo = ["pid": 0, "id": "test:" + cat]
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "test:" + cat, content: c, trigger: nil)) { err in
                log("test notification \(cat) \(err.map { "failed: \($0)" } ?? "ok")")
            }
        }
    }

    var alerts: [AlertItem] { data?.alerts ?? [] }
    var urgent: Int { alerts.filter { $0.notify }.count }

    /// Posts a notification for each new fast alert. The same alert repeats after an
    /// hour if it is still true (six hours for Fable).
    func deliver() {
        // nothing would show; leave the alerts unsent so they go out once notifications are allowed
        guard notifyOK == true else { return }
        let d = UserDefaults.standard
        var sent = d.dictionary(forKey: "notified") as? [String: Double] ?? [:]
        let now = Date().timeIntervalSince1970
        for a in alerts where a.notify {
            let gap: Double = a.kind == "fable" ? 6 * 3600 : 3600
            if now - (sent[a.id] ?? 0) < gap { continue }
            sent[a.id] = now
            let c = UNMutableNotificationContent()
            let fin = a.kind == "finished"
            c.title = alertTitle(a) + a.title
            c.body = a.kind == "cold" || fin ? a.text : a.text + (a.reasons?.first.map { " " + $0 } ?? "")
            c.sound = .default
            let fix = a.fixes.first?.fix ?? ""
            c.categoryIdentifier = fin ? (a.state == "waiting" ? "answer" : a.handoff_ok == true ? "followupBig" : "followup")
                : fix.isEmpty ? "plain" : "chat"
            c.userInfo = ["pid": a.pid, "id": a.id, "fix": fix, "sid": a.sid ?? "", "kind": a.kind]
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: a.id + "@\(Int(now))", content: c, trigger: nil)) { err in
                log("notify \(a.id) \(err.map { "failed: \($0)" } ?? "ok")")
            }
        }
        sent = sent.filter { now - $0.value < 86400 }
        d.set(sent, forKey: "notified")
    }

    func alertTitle(_ a: AlertItem) -> String {
        switch a.kind {
        case "runaway": return "Burning fast: "
        case "cold": return "Going cold: "
        case "finished": return a.state == "waiting" ? "Needs your answer: " : "Finished, more to do: "
        default: return "Wasting tokens: "
        }
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

// MARK: look

let coral = Color(red: 0.85, green: 0.47, blue: 0.36)

/// "in 2 hr 14 min (Thu, 9:54 PM)"
func resetsIn(_ t: Double) -> String {
    let left = max(0, t - Date().timeIntervalSince1970)
    let rel: String
    if left < 3600 { rel = "\(Int(left / 60)) min" }
    else if left < 86400 { rel = "\(Int(left / 3600)) hr \(Int(left.truncatingRemainder(dividingBy: 3600) / 60)) min" }
    else { let d = Int((left / 86400).rounded()); rel = d == 1 ? "1 day" : "\(d) days" }
    let f = DateFormatter()
    f.dateFormat = "EEE, h:mm a"
    return "Resets in \(rel) (\(f.string(from: Date(timeIntervalSince1970: t))))"
}

struct IconButton: View {
    let symbol: String
    let help: String
    let action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 13, weight: .medium))
                .frame(width: 26, height: 24)
                .background(RoundedRectangle(cornerRadius: 6).fill(hover ? Color.primary.opacity(0.08) : .clear))
        }
        .buttonStyle(.plain).help(help).onHover { hover = $0 }
    }
}

/// A thin bar; the striped part ahead is the last hour's pace.
struct MeterBar: View {
    let pct: Double
    var ahead: Double = 0
    var body: some View {
        GeometryReader { g in
            let w = g.size.width
            let used = w * CGFloat(min(pct, 100) / 100)
            ZStack(alignment: .leading) {
                Capsule().fill(coral.opacity(0.18))
                Capsule().fill(pct >= 90 ? Color.red : coral).frame(width: max(used, pct > 0 ? 6 : 0))
                Rectangle().fill(coral.opacity(0.45))
                    .frame(width: w * CGFloat(min(max(0, 100 - pct), ahead) / 100)).offset(x: used)
            }
            .clipShape(Capsule())
        }
        .frame(height: 7)
    }
}

struct MeterBlock: View {
    let label: String
    let pct: Double?
    var rate: Double? = nil
    var resets: Double? = nil
    var fullAt: Double? = nil
    var note: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(label).font(.system(size: 12)).foregroundStyle(.secondary)
                Spacer()
                if let r = rate { Text(String(format: r < 1 ? "%.2f%%/hr" : "%.1f%%/hr", r)).font(.system(size: 12)).monospacedDigit().foregroundStyle(.secondary) }
            }
            Text(pct.map { String(format: "%.0f%%", $0) } ?? "0%").font(.system(size: 24, weight: .bold)).monospacedDigit()
            MeterBar(pct: pct ?? 0, ahead: rate ?? 0)
            if let n = note {
                Text(n).font(.system(size: 11)).foregroundStyle(.secondary)
            } else if let r = resets {
                Text(resetsIn(r)).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if let f = fullAt, let r = resets, f < r {
                Text("At this pace you hit it \(when(f)), before the reset.")
                    .font(.system(size: 11, weight: .medium)).foregroundStyle(.red)
            }
        }
    }
}

struct MetersCard: View {
    @EnvironmentObject var model: Model
    let plan: Plan
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let m = plan.meters["five_hour"] {
                MeterBlock(label: "Current session", pct: m.pct, rate: m.hour_pct, resets: m.resets, fullAt: m.full_at)
            } else if plan.session_expected == true {
                MeterBlock(label: "Current session", pct: nil, note: "Starts with your next message")
            }
            if let m = plan.meters["week"] {
                MeterBlock(label: "Weekly limit", pct: m.pct, rate: m.hour_pct, resets: m.resets, fullAt: m.full_at)
            }
            ForEach(plan.scoped ?? [], id: \.label) { sc in
                MeterBlock(label: sc.label, pct: sc.pct, resets: sc.resets)
            }
            if plan.meters.isEmpty {
                Text("Plan limits unavailable\(plan.why.map { ": " + $0 } ?? ""). Showing meter dollars.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            HStack(spacing: 5) {
                Image(systemName: "clock")
                Text(model.updated.formatted(date: .omitted, time: .shortened))
                Spacer()
            }
            .font(.system(size: 11)).foregroundStyle(.tertiary)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color.primary.opacity(0.05)))
    }
}

/// A small capsule label that never wraps.
extension View {
    /// Click to open the chat, with a hand cursor; does nothing for chats without a link.
    @ViewBuilder func jumpable(_ link: String?, _ action: @escaping () -> Void) -> some View {
        if let l = link, !l.isEmpty {
            self.contentShape(Rectangle()).onTapGesture(perform: action).help("Open this chat")
                .onHover { inside in if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() } }
        } else {
            self
        }
    }
}

struct Tag: View {
    let text: String
    var color: Color = .secondary
    var filled = false
    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .semibold))
            .lineLimit(1)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Capsule().fill(filled ? color.opacity(0.16) : Color.clear))
            .overlay(Capsule().stroke(color.opacity(filled ? 0 : 0.5), lineWidth: 1))
            .foregroundStyle(color)
            .fixedSize()
    }
}

func causeColor(_ kind: String) -> Color {
    switch kind {
    case "fable": return .purple
    case "rebuild": return .blue
    case "cold": return .teal
    case "state-done": return .green
    case "state-waiting": return .pink
    case "state-loose": return .orange
    case "context": return .orange
    case "jump": return .red
    default: return .orange
    }
}

/// One right-aligned number with its caption, at a fixed width so columns line up.
struct Num: View {
    let value: String
    let caption: String
    var color: Color = .primary
    var width: CGFloat = 54
    var body: some View {
        VStack(alignment: .trailing, spacing: 1) {
            Text(value).font(.system(size: 12, weight: .semibold)).monospacedDigit()
                .foregroundStyle(color).lineLimit(1)
            Text(caption).font(.system(size: 10)).foregroundStyle(.tertiary).lineLimit(1)
        }
        .frame(width: width, alignment: .trailing)
    }
}

/// How many of the usage settings in SETTINGS-ADVICE.md are in place, with the advice to copy.
struct SettingsRow: View {
    let info: SettingsInfo
    var dismiss: () -> Void = {}
    @State private var copied = false
    var done: Int { info.checks.filter { $0.ok }.count }
    var all: Bool { done == info.checks.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: all ? "checkmark.seal.fill" : "slider.horizontal.3")
                    .foregroundStyle(all ? Color.green : Color.orange)
                Text(all ? "Usage settings all in place" : "\(done) of \(info.checks.count) usage settings in place")
                    .font(.system(size: 12, weight: .medium))
                Spacer()
                Button(copied ? "Copied" : "Copy settings advice") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(info.advice, forType: .string)
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { copied = false }
                }
                .buttonStyle(.borderless).font(.system(size: 11)).disabled(info.advice.isEmpty)
                if all {
                    Button { withAnimation { dismiss() } } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.borderless).help("Hide this and the test notification line until a setting goes missing")
                }
            }
            if !all {
                ForEach(info.checks.filter { !$0.ok }, id: \.self) { c in
                    Text("Missing: " + c.label).font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Text("Paste the advice into a Claude Code chat to apply them.")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.04)))
    }
}

/// Warns when macOS blocks Chatwatch's notifications, and offers a test once they work.
struct NotifyRow: View {
    @EnvironmentObject var model: Model
    var body: some View {
        if model.notifyOK == false {
            HStack(spacing: 6) {
                Image(systemName: "bell.slash.fill").foregroundStyle(.orange)
                Text("Notifications are off for Chatwatch, so alerts come without buttons.")
                    .font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button("Open Settings") {
                    NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=com.chatwatch.menubar")!)
                }
                .buttonStyle(.borderless).font(.system(size: 11))
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.orange.opacity(0.08)))
        } else if model.notifyOK == true {
            HStack(spacing: 6) {
                Text(model.testResult ?? "").font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                Spacer()
                Button("Send a test notification") { model.sendTest() }
                    .buttonStyle(.borderless).font(.system(size: 11))
            }
        }
    }
}

struct AttentionRow: View {
    @EnvironmentObject var model: Model
    let a: AlertItem
    @State private var open = false
    @State private var copied: String?

    var runaway: Bool { a.kind == "runaway" }
    var cold: Bool { a.kind == "cold" }
    var finished: Bool { cold && a.state == "done" }
    var followup: Bool { a.kind == "finished" }
    var waiting: Bool { a.state == "waiting" }
    var color: Color { runaway ? .red : cold ? .teal : followup ? .blue : .orange }
    var headline: String {
        if runaway { return "Burning fast, mostly on real work" }
        if followup { return waiting ? "Finished its turn: needs your answer" : "Finished its turn: more to do" }
        if finished { return "Looks finished: safe to let it go cold" }
        if cold { return "Cache expires in \(a.cold_in_min ?? 0) min: hand off now" }
        let share = Int(((a.share ?? 0) * 100).rounded())
        return "\(share)% of what it uses is waste"
    }
    var rate: String {
        if followup { return "" }
        if cold {
            if let p = a.cold_cost_pct { return pct(p) }
            return usd(a.cold_cost_usd ?? 0)
        }
        if let p = a.waste_pct_h { return String(format: "%.1f%%/hr", p) }
        return usd(a.waste_usd_h ?? 0) + "/hr"
    }
    /// The button name for a cause's fix; just "Copy fix" when there is only one.
    func label(_ c: Cause) -> String {
        if a.fixes.count < 2 { return "Copy fix" }
        let names = ["context": "context", "subagents": "subagents", "jump": "big reads",
                     "fable": "Fable", "rebuild": "rebuild", "cold": "handoff"]
        return "Copy fix: " + (names[c.kind] ?? c.kind)
    }
    func continueIt() {
        model.continueChat(a.sid)
        withAnimation { copied = "continue" }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { withAnimation { if copied == "continue" { copied = nil } } }
    }
    func copyFix(_ c: Cause) {
        model.copy(c.fix ?? "")
        withAnimation { copied = c.kind }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { withAnimation { if copied == c.kind { copied = nil } } }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Image(systemName: runaway ? "flame.fill" : cold ? "snowflake" : followup ? "checkmark.circle.fill" : "drop.fill").font(.system(size: 11)).foregroundStyle(color)
                Text(a.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    .jumpable(a.jump) { model.jump(a.sid) }
                if let n = a.subs_active, n > 0 { Tag(text: "\(n) sub", color: .orange, filled: true) }
                Spacer(minLength: 4)
                Text(rate).font(.system(size: 13, weight: .semibold)).monospacedDigit().foregroundStyle(color)
            }
            HStack(spacing: 5) {
                Text(headline).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 4)
                Button(open ? "Hide" : "Why") { withAnimation(.easeOut(duration: 0.15)) { open.toggle() } }
                    .buttonStyle(.borderless).font(.system(size: 11))
            }
            if let cs = a.causes, !cs.isEmpty {
                HStack(spacing: 5) {
                    ForEach(cs, id: \.self) { c in Tag(text: c.pill, color: causeColor(c.kind), filled: true) }
                }
            }
            if cold || followup, let line = a.closing, !line.isEmpty {
                Text(line).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true).help(line)
            }
            if open {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(a.causes ?? [], id: \.self) { c in
                        Text(c.text).font(.system(size: 11)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        if let f = c.fix, !f.isEmpty {
                            Text("Fix to paste: " + f).font(.system(size: 10)).foregroundStyle(.tertiary)
                                .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if a.busy {
                        Text("Working right now: closing stops it mid-task.").font(.system(size: 10)).foregroundStyle(.tertiary)
                    }
                }
            }
            HStack(spacing: 6) {
                ForEach(Array(a.fixes.prefix(2).enumerated()), id: \.offset) { i, c in
                    if i == 0 {
                        Button(label(c)) { copyFix(c) }.buttonStyle(.borderedProminent)
                    } else {
                        Button(label(c)) { copyFix(c) }.buttonStyle(.bordered)
                    }
                }
                if followup {
                    if waiting {
                        Button("Open the chat") { model.jump(a.sid) }.buttonStyle(.borderedProminent)
                    } else if a.handoff_ok == true {
                        Button("Handoff into a new chat") { model.close(a.pid, handoff: true, reopen: true) }
                            .buttonStyle(.borderedProminent)
                        Button("Continue") { continueIt() }.buttonStyle(.bordered)
                    } else {
                        Button("Continue") { continueIt() }.buttonStyle(.borderedProminent)
                    }
                } else if finished {
                    Button("Close now") { model.close(a.pid, handoff: false) }.buttonStyle(.borderedProminent)
                } else if a.fixes.isEmpty {
                    Button("Handoff and close") { model.close(a.pid, handoff: true) }.buttonStyle(.borderedProminent)
                }
                Spacer(minLength: 4)
                Menu("Close") {
                    if followup { Button("Handoff into a new chat") { model.close(a.pid, handoff: true, reopen: true) } }
                    Button("Handoff and close") { model.close(a.pid, handoff: true) }
                    Button("Close now") { model.close(a.pid, handoff: false) }
                }
                .menuStyle(.borderlessButton).fixedSize()
                Button("Snooze 1 h") { model.snooze(a.id) }.buttonStyle(.borderless)
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            .controlSize(.small).tint(coral)
            if copied == "continue" {
                Text("\"continue\" is on your clipboard: paste it into the chat and press Enter.")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            } else if copied != nil {
                Text("Copied. Paste it into that chat.").font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12).fill(color.opacity(0.07)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(color.opacity(0.16), lineWidth: 1))
    }
}

struct ChatRow: View {
    @EnvironmentObject var model: Model
    let c: Chat
    @State private var confirming = false
    @State private var hover = false

    var share: Double { c.waste?.share ?? 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .center, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(c.title).font(.system(size: 13, weight: .medium)).lineLimit(1).truncationMode(.tail)
                        .jumpable(c.jump) { model.jump(c.sid) }
                    HStack(spacing: 5) {
                        Tag(text: c.family.isEmpty ? "?" : c.family, color: c.family == "fable" ? .purple : .secondary)
                        if let n = c.subs_active, n > 0 { Tag(text: "\(n) sub", color: .orange, filled: true) }
                        if c.busy {
                            Circle().fill(Color.green).frame(width: 6, height: 6)
                            Text("working").font(.system(size: 11)).foregroundStyle(.green).fixedSize()
                        } else {
                            Text("idle \(idle(c.idle_min))").font(.system(size: 11)).foregroundStyle(.secondary).fixedSize()
                        }
                        Text(c.folder).font(.system(size: 11)).lineLimit(1).foregroundStyle(.tertiary).layoutPriority(-1)
                    }
                    if let cause = c.cause {
                        Tag(text: cause.pill, color: causeColor(cause.kind), filled: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Num(value: c.rate_pct_h != nil ? pct(c.rate_pct_h) + "/h" : usd(c.rate_usd_h ?? 0) + "/h",
                    caption: "now", width: 62)
                Num(value: (c.waste?.steps ?? 0) == 0 ? "-" : "\(Int((share * 100).rounded()))%",
                    caption: "waste", color: share >= 0.4 ? .orange : .primary, width: 40)
                Num(value: kTok(c.ctx), caption: "context", width: 46)
            }
            if let j = c.job, let msg = j.msg {
                Text(msg).font(.system(size: 11)).foregroundStyle(j.state == "error" ? Color.red : Color.secondary)
            } else if confirming {
                HStack(spacing: 6) {
                    Button("Handoff and close") { model.close(c.pid, handoff: true); confirming = false }
                        .buttonStyle(.borderedProminent)
                    Button("Close now") { model.close(c.pid, handoff: false); confirming = false }
                    Button("Cancel") { confirming = false }.buttonStyle(.borderless)
                    Spacer()
                }
                .controlSize(.small).tint(coral)
            }
        }
        .padding(.vertical, 7).padding(.horizontal, 10)
        .background(RoundedRectangle(cornerRadius: 8).fill(hover ? Color.primary.opacity(0.05) : Color.clear))
        .overlay(alignment: .topTrailing) {
            if hover && c.job == nil && !confirming {
                Button { confirming = true } label: { Image(systemName: "xmark.circle.fill") }
                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Close this chat").padding(4)
            }
        }
        .onHover { hover = $0 }
    }
}

struct Panel: View {
    @EnvironmentObject var model: Model
    var scrolls = true
    @State private var showAll = false
    @AppStorage("hideSetupRows") private var hideSetup = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Text("Chatwatch").font(.system(size: 15, weight: .bold))
                if let p = model.data?.plan.plan, !p.isEmpty {
                    Text(p).font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill(Color.primary.opacity(0.08)))
                }
                Spacer()
                IconButton(symbol: "arrow.clockwise", help: "Refresh") { model.refresh() }
                IconButton(symbol: "chart.line.uptrend.xyaxis", help: "Open dashboard") { NSWorkspace.shared.open(base) }
            }
            if let err = model.error, model.data == nil {
                Text(err).font(.callout).foregroundStyle(.secondary).padding(.vertical, 20)
            } else if let d = model.data {
                MetersCard(plan: d.plan)
                // once everything is in place, both rows can be dismissed; a missing setting brings them back
                let allSet = d.plan.settings.map { s in s.checks.allSatisfy { $0.ok } } ?? true
                if !(allSet && hideSetup) || model.notifyOK == false { NotifyRow() }
                if let st = d.plan.settings, !(allSet && hideSetup) { SettingsRow(info: st) { hideSetup = true } }
                content(d)
            } else {
                ProgressView().frame(maxWidth: .infinity).padding()
            }
            Divider()
            HStack {
                Button { NSWorkspace.shared.open(base) } label: {
                    Label("Open Dashboard", systemImage: "chart.line.uptrend.xyaxis")
                }
                .buttonStyle(.borderless)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.borderless).foregroundStyle(.secondary)
            }
            .font(.system(size: 12))
        }
        .padding(14)
        .frame(width: 420)
    }

    @ViewBuilder func content(_ d: Payload) -> some View {
        let shown = showAll ? d.chats : Array(d.chats.prefix(6))
        let body = VStack(alignment: .leading, spacing: 10) {
            if model.alerts.isEmpty {
                Label("No chat is wasting much right now.", systemImage: "checkmark.circle")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                HStack {
                    Text("Needs attention").font(.system(size: 13, weight: .semibold))
                    Spacer()
                    let costed = model.alerts.filter { $0.kind != "finished" }
                    Text(costed.isEmpty ? "" : costed.allSatisfy { $0.kind == "cold" } ? "cost to resume cold"
                         : costed.contains { $0.kind == "cold" } ? "wasted per hour, or to resume cold"
                         : "wasted per hour").font(.system(size: 10)).foregroundStyle(.tertiary)
                }
                ForEach(model.alerts) { a in AttentionRow(a: a) }
            }
            HStack {
                Text("Chats").font(.system(size: 13, weight: .semibold))
                Text("\(d.chats.count) open, \(d.chats.filter { $0.busy }.count) working")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.top, 4)
            VStack(spacing: 0) {
                ForEach(shown) { c in ChatRow(c: c) }
            }
            if d.chats.count > 6 {
                Button(showAll ? "Show fewer" : "Show all \(d.chats.count)") { showAll.toggle() }
                    .buttonStyle(.borderless).font(.system(size: 12))
            }
            ForEach(Array((d.closed ?? [:]).values.enumerated()), id: \.offset) { _, j in
                HStack {
                    Text(j.msg ?? "").font(.system(size: 11)).foregroundStyle(.secondary)
                    Spacer()
                    if let p = j.path, !p.isEmpty {
                        Button("Open handoff") { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }.controlSize(.small)
                    }
                }
            }
        }
        if scrolls {
            ScrollView { body }.frame(height: min(CGFloat(shown.count) * 52 + CGFloat(model.alerts.count) * 130 + 90, 460))
        } else {
            body
        }
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
        let copyFix = UNNotificationAction(identifier: "copyfix", title: "Copy fix", options: [])
        let copy = UNNotificationAction(identifier: "copy", title: "Copy /model opus", options: [])
        let cont = UNNotificationAction(identifier: "continue", title: "Continue", options: [])
        let openIt = UNNotificationAction(identifier: "open", title: "Open the chat", options: [])
        let handNew = UNNotificationAction(identifier: "handoffnew", title: "Handoff into a new chat", options: [])
        c.setNotificationCategories([
            UNNotificationCategory(identifier: "chat", actions: [copyFix, handoff, close, snooze], intentIdentifiers: []),
            UNNotificationCategory(identifier: "plain", actions: [handoff, close, snooze], intentIdentifiers: []),
            UNNotificationCategory(identifier: "fable", actions: [copy, handoff, snooze], intentIdentifiers: []),
            UNNotificationCategory(identifier: "followup", actions: [cont, handNew, snooze], intentIdentifiers: []),
            UNNotificationCategory(identifier: "followupBig", actions: [handNew, cont, snooze], intentIdentifiers: []),
            UNNotificationCategory(identifier: "answer", actions: [openIt, handNew, snooze], intentIdentifiers: []),
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
        let fix = info["fix"] as? String ?? ""
        let sid = info["sid"] as? String ?? ""
        let kind = info["kind"] as? String ?? ""
        let action = r.actionIdentifier
        log("action \(action) on \(id)")
        Task { @MainActor in
            let m = Model.shared
            if id.hasPrefix("test:") {
                let names = ["handoff": "Handoff and close", "close": "Close now", "snooze": "Snooze 1 hour",
                             "copy": "Copy /model opus (copied)"]
                if action == "copy" { m.copyModelSwitch() }
                m.testResult = "Test notification: \(names[action] ?? "the notification itself") reached Chatwatch."
                if names[action] == nil { self.showWindow() }
                done()
                return
            }
            switch action {
            case "handoff": m.close(pid, handoff: true)
            case "close": m.close(pid, handoff: false)
            case "snooze": m.snooze(id)
            case "copyfix": if !fix.isEmpty { m.copy(fix) }
            case "copy": m.copyModelSwitch()
            case "continue": m.continueChat(sid)
            case "open": m.jump(sid)
            case "handoffnew": m.close(pid, handoff: true, reopen: true)
            default: if kind == "finished" { m.jump(sid) } else { self.showWindow() }
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
        // Drawn through a real (offscreen) window rather than ImageRenderer, which
        // leaves buttons and menus as yellow placeholders.
        _ = NSApplication.shared
        for dark in [false, true] {
            let view = Panel(scrolls: false).environmentObject(model)
                .background(Color(nsColor: .windowBackgroundColor))
                .environment(\.controlActiveState, .key)   // prominent buttons in their active colour
            let host = NSHostingView(rootView: view)
            host.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
            host.setFrameSize(host.fittingSize)
            let win = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
            win.appearance = host.appearance
            win.contentView = host
            host.layoutSubtreeIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.3))
            let size = host.bounds.size
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2),
                    pixelsHigh: Int(size.height * 2), bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                    isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { continue }
            rep.size = size
            host.cacheDisplay(in: host.bounds, to: rep)
            if let png = rep.representation(using: .png, properties: [:]) {
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
