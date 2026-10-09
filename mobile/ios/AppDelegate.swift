import UIKit
import WebKit

@main
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?
    var webView: WKWebView?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        setupScreenCaptureObserver()
        return true
    }

    private func setupScreenCaptureObserver() {
        // Monitor iOS screen recording, AirPlay mirroring, and video capture
        NotificationCenter.default.addObserver(
            forName: UIScreen.capturedDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            let isCaptured = UIScreen.main.isCaptured
            self?.notifyWebViewCaptureStatus(isCaptured: isCaptured)
        }
    }

    private func notifyWebViewCaptureStatus(isCaptured: Bool) {
        // Dispatch capture status directly to JavaScript call privacy observer
        let script = "window.dispatchEvent(new CustomEvent('captureStatusChanged', { detail: { isCaptured: \(isCaptured) } }));"
        webView?.evaluateJavaScript(script, completionHandler: nil)
    }
}

