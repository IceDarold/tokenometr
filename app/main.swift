import Cocoa
import WebKit

/// One window with the usage page. Every 30 seconds (and whenever the app comes to the front)
/// it runs usage.py and hands the fresh numbers to the page. Every 5 minutes it also takes an
/// official limit reading through Claude Code CLI (`usage.py --probe`).
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

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
