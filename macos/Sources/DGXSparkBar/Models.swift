import Foundation

// Mirrors the agent's JSON. Everything the agent may omit (no GPU, no docker,
// no ollama) is optional here — a Spark with half the stack missing must still
// render, not fail to decode.

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
    let cores: [Double]?
    let loadavg: [Double]?
    let tempC: Double?
}

struct MemoryInfo: Codable {
    let totalKb: Double?
    let usedKb: Double?
    let availKb: Double?
    let pct: Double?
    let swapUsedKb: Double?

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
    let tailscaleIp: String?
    let lanIp: String?
}

struct DiskInfo: Codable, Identifiable {
    let mount: String
    let totalGb: Double
    let usedGb: Double
    let freeGb: Double
    let pct: Double

    var id: String { mount }
}

struct ContainerInfo: Codable, Identifiable {
    let name: String
    let image: String?
    let state: String
    let status: String?

    var id: String { name }
    var isRunning: Bool { state == "running" }
}

struct LoadedModel: Codable, Identifiable {
    let name: String
    let memGb: Double
    var id: String { name }
}

struct OllamaInfo: Codable {
    let configured: Bool
    let reachable: Bool?
    let loaded: [LoadedModel]?
    let modelCount: Int?
}

struct JobInfo: Codable, Identifiable {
    let kind: String
    let name: String
    let running: Bool?
    let count: Int?
    let tail: [String]?

    var id: String { kind + ":" + name }
}

struct Sample: Codable, Hashable {
    let t: Double
    let cpu: Double
    let gpu: Double
    let mem: Double
    let rx: Double
    let w: Double?
    let mhz: Double?
}

struct Status: Codable {
    let app: String
    let version: String
    let host: String
    let machineId: String
    let uptimeSec: Double
    let level: String
    let findings: [Finding]
    let cpu: CPUInfo
    let memory: MemoryInfo
    let gpu: GPUInfo
    let net: NetInfo
    let disks: [DiskInfo]
    let docker: [ContainerInfo]
    let ollama: OllamaInfo
    let jobs: [JobInfo]
    let history: [Sample]
    let actions: [String]
    let webUiUrl: String?
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
