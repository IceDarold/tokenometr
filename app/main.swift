import Cocoa
import WebKit

/// One window with the usage page. Every 30 seconds (and whenever the app comes to the front)
/// it runs usage.py and hands the fresh numbers to the page. Every 5 minutes it also takes an
/// official limit reading through Claude Code CLI (`usage.py --probe`). At launch it makes sure the
/// chat sync between Claude accounts runs in the background (see ChatSync).
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var pageReady = false
    private var running = false
    private var probing = false
    private var lastRun = Date.distantPast
    private let interval: TimeInterval = 30
    private let probeInterval: TimeInterval = 300
    /// Counting and probing both rewrite Tokenometr's readings file, so they take turns on one queue.
    private let worker = DispatchQueue(label: "local.tokenometr.worker", qos: .userInitiated)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = makeMenu()

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "refresh")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Токенометр"
        window.minSize = NSSize(width: 520, height: 420)
        window.backgroundColor = NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor(srgbRed: 0x13 / 255, green: 0x15 / 255, blue: 0x17 / 255, alpha: 1)
                : NSColor(srgbRed: 0xE9 / 255, green: 0xEC / 255, blue: 0xE7 / 255, alpha: 1)
        }
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("Tokenometr")
        window.makeKeyAndOrderFront(nil)

        let resources = Bundle.main.resourceURL!
        webView.loadFileURL(resources.appendingPathComponent("index.html"), allowingReadAccessTo: resources)

        DispatchQueue.global(qos: .utility).async { ChatSync.install() }
        Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in self?.refresh() }
        Timer.scheduledTimer(withTimeInterval: probeInterval, repeats: true) { [weak self] _ in self?.probe() }
        probe()
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            guard let self = self, Date().timeIntervalSince(self.lastRun) > 5 else { return }
            self.refresh()
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageReady = true
        refresh()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        refresh()
    }

    @objc private func refreshNow() { refresh() }

    private func refresh() {
        guard pageReady, !running else { return }
        running = true
        lastRun = Date()
        worker.async {
            let js = Self.runCounter()
            DispatchQueue.main.async {
                self.webView.evaluateJavaScript(js, completionHandler: nil)
                self.running = false
            }
        }
    }

    /// Takes a limit reading through Claude Code CLI, then shows the numbers with it.
    private func probe() {
        guard !probing else { return }
        probing = true
        worker.async {
            _ = Self.runScript(["--probe"])
            DispatchQueue.main.async {
                self.probing = false
                self.refresh()
            }
        }
    }

    /// Runs usage.py and returns the JavaScript call that shows its result (or its error) on the page.
    private static func runCounter() -> String {
        guard let result = runScript([]) else {
            return "window.renderError(\(jsString("python3 не запустился")))"
        }
        if result.status == 0, let json = String(data: result.output, encoding: .utf8), !json.isEmpty {
            return "window.render(\(json))"
        }
        let message = String(data: result.errors, encoding: .utf8).flatMap { $0.isEmpty ? nil : $0 }
            ?? "python3 завершился с кодом \(result.status)"
        return "window.renderError(\(jsString(message)))"
    }

    /// Runs usage.py with `arguments`; nil if it could not be started.
    private static func runScript(_ arguments: [String]) -> (status: Int32, output: Data, errors: Data)? {
        let script = Bundle.main.resourceURL!.appendingPathComponent("usage.py").path
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [script] + arguments
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        do {
            try process.run()
        } catch {
            return nil
        }
        // Read stderr alongside stdout so neither pipe can fill up and stall the counter.
        let errors = DataBox()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global().async {
            errors.data = err.fileHandleForReading.readDataToEndOfFile()
            group.leave()
        }
        let output = out.fileHandleForReading.readDataToEndOfFile()
        group.wait()
        process.waitUntilExit()
        return (process.terminationStatus, output, errors.data)
    }

    private static func jsString(_ text: String) -> String {
        guard let data = try? JSONEncoder().encode(text), let literal = String(data: data, encoding: .utf8) else {
            return "\"\""
        }
        return literal
    }

    private func makeMenu() -> NSMenu {
        let menu = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Скрыть Токенометр", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Выйти из Токенометра", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(submenu: appMenu, title: "Токенометр")

        let editMenu = NSMenu(title: "Правка")
        editMenu.addItem(withTitle: "Скопировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Выбрать всё", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        menu.addItem(submenu: editMenu, title: "Правка")

        let viewMenu = NSMenu(title: "Вид")
        let refreshItem = NSMenuItem(title: "Обновить", action: #selector(refreshNow), keyEquivalent: "r")
        refreshItem.target = self
        viewMenu.addItem(refreshItem)
        menu.addItem(submenu: viewMenu, title: "Вид")

        let windowMenu = NSMenu(title: "Окно")
        windowMenu.addItem(withTitle: "Свернуть", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Закрыть", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        menu.addItem(submenu: windowMenu, title: "Окно")
        return menu
    }
}

/// Keeps the Code-tab chat list the same in every Claude account (chatsync.py). launchd runs it in the
/// background as this binary with `--chat-sync`, so macOS lists it under Tokenometr's name in Login Items.
enum ChatSync {
    static let label = "local.tokenometr.chat-sync"
    static let flag = "--chat-sync"

    /// /usr/bin/python3 works only with Apple's Command Line Tools or Xcode; otherwise it offers to install them.
    static var pythonAvailable: Bool {
        guard let result = run("/usr/bin/xcode-select", ["-p"]), result.status == 0 else { return false }
        let developerDir = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        return FileManager.default.isExecutableFile(atPath: developerDir + "/usr/bin/python3")
    }

    /// The LaunchAgent's entry point: turns into `python3 chatsync.py --watch`.
    static func runAgent() -> Never {
        guard pythonAvailable else {
            _ = sleep(3600)  // launchd starts the agent again afterwards; maybe the tools are there by then
            exit(0)
        }
        let script = Bundle.main.resourceURL!.appendingPathComponent("chatsync.py").path
        let words = ["/usr/bin/python3", script, "--watch"]
        var arguments: [UnsafeMutablePointer<CChar>?] = words.map { strdup($0) }
        arguments.append(nil)
        execv("/usr/bin/python3", &arguments)
        perror("execv /usr/bin/python3")
        exit(1)
    }

    /// Registers the LaunchAgent for this copy of the app, unless it already is.
    static func install() {
        guard pythonAvailable, let executable = Bundle.main.executablePath,
              let resources = Bundle.main.resourceURL else { return }
        let fileManager = FileManager.default
        let home = fileManager.homeDirectoryForCurrentUser
        let plist = home.appendingPathComponent("Library/LaunchAgents/\(label).plist")
        let log = home.appendingPathComponent("Library/Logs/Tokenometr/chat-sync.log")
        let script = resources.appendingPathComponent("chatsync.py").path
        let job: [String: Any] = [
            "Label": label,
            "ProgramArguments": [executable, flag],
            "RunAtLoad": true,
            "KeepAlive": ["PathState": [script: true]],  // runs while this copy of the app exists
            "ProcessType": "Background",
            "ThrottleInterval": 30,
            "StandardOutPath": log.path,
            "StandardErrorPath": log.path,
            "AssociatedBundleIdentifiers": [Bundle.main.bundleIdentifier ?? "local.tokenometr"],
        ]
        let service = "gui/\(getuid())/\(label)"
        if let current = NSDictionary(contentsOf: plist), current.isEqual(to: job),
           run("/bin/launchctl", ["print", service])?.status == 0 {
            return
        }
        guard let data = try? PropertyListSerialization.data(fromPropertyList: job, format: .xml, options: 0) else {
            return
        }
        try? fileManager.createDirectory(at: plist.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fileManager.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard (try? data.write(to: plist, options: .atomic)) != nil else { return }
        _ = run("/bin/launchctl", ["bootout", service])
        var waited = 0
        while waited < 30, run("/bin/launchctl", ["print", service])?.status == 0 {
            Thread.sleep(forTimeInterval: 0.1)  // bootstrapping again before the old job is gone fails
            waited += 1
        }
        _ = run("/bin/launchctl", ["bootstrap", "gui/\(getuid())", plist.path])
    }

    private static func run(_ path: String, _ arguments: [String]) -> (status: Int32, output: String)? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        let out = Pipe()
        process.standardOutput = out
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }
}

final class DataBox: @unchecked Sendable {
    var data = Data()
}

extension NSMenu {
    func addItem(submenu: NSMenu, title: String) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.submenu = submenu
        addItem(item)
    }
}

if CommandLine.arguments.dropFirst().first == ChatSync.flag {
    ChatSync.runAgent()
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
