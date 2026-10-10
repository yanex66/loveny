import UIKit
import WebKit

@main
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?
    private weak var webView: WKWebView?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        setupScreenCaptureObserver()
        return true
    }

    private func setupScreenCaptureObserver() {
        NotificationCenter.default.addObserver(
            forName: UIScreen.capturedDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            let isCaptured = UIScreen.main.isCaptured
            self?.notifyWebViewCaptureStatus(isCaptured: isCaptured)
        }
        NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.notifyWebViewCaptureStatus(isCaptured: UIScreen.main.isCaptured)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.notifyWebViewCaptureStatus(isCaptured: UIScreen.main.isCaptured)
        }
    }

    private func notifyWebViewCaptureStatus(isCaptured: Bool) {
        let script = "window.dispatchEvent(new CustomEvent('captureStatusChanged', { detail: { isRecording: \(isCaptured), isCaptured: \(isCaptured) } }));"
        (webView ?? findWebView())?.evaluateJavaScript(script) { _, error in
            if let error = error {
                NSLog("Could not deliver screen-capture status to the web view: %@", error.localizedDescription)
            }
        }
    }

    private func findWebView() -> WKWebView? {
        for scene in UIApplication.shared.connectedScenes {
            guard let windowScene = scene as? UIWindowScene else { continue }
            for sceneWindow in windowScene.windows {
                if let webView = findWebView(in: sceneWindow.rootViewController?.view) {
                    return webView
                }
            }
        }
        return nil
    }

    private func findWebView(in view: UIView?) -> WKWebView? {
        guard let view = view else { return nil }
        if let webView = view as? WKWebView {
            return webView
        }
        for child in view.subviews {
            if let webView = findWebView(in: child) {
                return webView
            }
        }
        return nil
    }
}
