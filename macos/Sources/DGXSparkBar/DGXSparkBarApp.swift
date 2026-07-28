import AppKit
import SwiftUI

/// The polling loop starts here, not in the menu's `onAppear`: a MenuBarExtra in
/// window style builds its content only when the user opens it, so anything
/// hung off the view would leave the icon grey until someone clicked it — the
/// exact opposite of what a status indicator is for.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated { Store.shared.start() }
    }
}

@main
struct DGXDGXSparkBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store = Store.shared

    var body: some Scene {
        MenuBarExtra {
            MenuView(store: store)
        } label: {
            // A plain SF Symbol would be template-rendered, i.e. monochrome, and
            // the whole point is that the colour is readable at a glance. Drawing
            // the dot ourselves with isTemplate = false keeps it.
            Image(nsImage: StatusDot.image(for: store.health))
        }
        .menuBarExtraStyle(.window)
    }
}

enum StatusDot {
    static func image(for health: Health) -> NSImage {
        let color: NSColor = switch health {
        case .ok: .systemGreen
        case .warn: .systemYellow
        case .error: .systemRed
        case .unknown: .systemGray
        }

        let size = NSSize(width: 14, height: 14)
        let image = NSImage(size: size, flipped: false) { rect in
            color.setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: 2.5, dy: 2.5)).fill()
            // Hollow ring while nothing is known yet: an absent box must not look
            // like a healthy one that simply happens to be grey.
            if health == .unknown {
                NSColor.clear.setFill()
                let inner = rect.insetBy(dx: 4.5, dy: 4.5)
                NSGraphicsContext.current?.compositingOperation = .destinationOut
                NSBezierPath(ovalIn: inner).fill()
                NSGraphicsContext.current?.compositingOperation = .sourceOver
            }
            return true
        }
        image.isTemplate = false
        return image
    }
}
