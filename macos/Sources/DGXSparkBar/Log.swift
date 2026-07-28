import Foundation

/// Discovery is the part that fails silently in the field — wrong network,
/// no Tailscale, Bonjour permission not granted. Run the binary with
/// DGX_SPARK_BAR_DEBUG=1 and it says which channel found what.
enum Log {
    static let enabled = ProcessInfo.processInfo.environment["DGX_SPARK_BAR_DEBUG"] != nil

    static func debug(_ message: @autoclosure () -> String) {
        guard enabled else { return }
        FileHandle.standardError.write(Data(("[dgx-spark-bar] " + message() + "\n").utf8))
    }
}
