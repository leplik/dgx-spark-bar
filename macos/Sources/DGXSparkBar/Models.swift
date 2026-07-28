import Foundation

// Mirrors the agent's JSON. Anything the agent may omit (a box with no GPU, a
// kernel with no thermal zones) is optional here — a partial Spark must still
// render, not fail to decode. Fields the agent sends but nothing draws are
// simply absent: Codable ignores what it was not asked for.

struct Ping: Codable {
    let app: String
    let version: String
    let host: String
    let machineId: String
}

struct Finding: Codable, Identifiable, Hashable {
    let id: String
    let severity: String
    let title: String
    let detail: String
    let hint: String?

    var isCritical: Bool { severity == "critical" }
}

struct CPUInfo: Codable {
    let pct: Double?
    let tempC: Double?
}

struct MemoryInfo: Codable {
    let totalKb: Double?
    let usedKb: Double?

    var usedGb: Double { (usedKb ?? 0) / 1e6 }
    var totalGb: Double { (totalKb ?? 0) / 1e6 }
}

struct GPUInfo: Codable {
    let present: Bool
    let name: String?
    let utilPct: Double?
    let tempC: Double?
    let powerW: Double?
    let smClockMhz: Double?
}

struct NetInfo: Codable {
    let iface: String?
    let rxMbs: Double?
    let txMbs: Double?
}

struct DiskInfo: Codable, Identifiable {
    let mount: String
    let totalGb: Double
    let usedGb: Double
    let freeGb: Double
    let pct: Double

    var id: String { mount }
}

/// One poll's worth of the two series the menu draws. The sparkline plots by
/// position, so the sample's timestamp is not decoded either.
struct Sample: Codable {
    let cpu: Double
    let gpu: Double
}

struct Status: Codable {
    let uptimeSec: Double
    let level: String
    let findings: [Finding]
    let cpu: CPUInfo
    let memory: MemoryInfo
    let gpu: GPUInfo
    let net: NetInfo
    let disks: [DiskInfo]
    let history: [Sample]
    let actions: [String]
}

enum Health: String {
    case ok, warn, error, unknown

    init(level: String?) {
        self = level.flatMap { Health(rawValue: $0) } ?? .unknown
    }
}

/// Where an agent came from. The tailnet path works from any network; the LAN
/// path is what saves you when the tailnet control plane is unreachable.
enum DiscoverySource: String, Codable {
    case tailnet
    case bonjour
    case manual
}

struct Agent: Identifiable, Hashable {
    let machineId: String
    let host: String
    let baseURL: URL
    let source: DiscoverySource

    var id: String { machineId }
}
