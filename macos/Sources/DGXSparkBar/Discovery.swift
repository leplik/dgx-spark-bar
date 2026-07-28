import Foundation
import Network

/// Finds dgx-spark-bar agents without any configuration.
///
/// Two independent channels, because each covers the other's blind spot:
///   * tailnet — works from any network, through NAT, at a customer site;
///   * Bonjour — works when both machines are on one switch and the tailnet
///     control plane is unreachable.
enum Discovery {
    static let defaultPort = 8765

    // MARK: tailnet

    /// The Tailscale CLI ships inside the app bundle (App Store build) or as a
    /// plain binary (standalone/brew). Probing all of them beats guessing.
    private static let tailscaleBinaries = [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ]

    static func tailnetCandidates() -> [URL] {
        guard let binary = tailscaleBinaries.first(where: { FileManager.default.isExecutableFile(atPath: $0) })
        else { return [] }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: binary)
        process.arguments = ["status", "--json"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
        } catch {
            return []
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [] }

        var nodes: [[String: Any]] = []
        if let peers = root["Peer"] as? [String: Any] {
            nodes.append(contentsOf: peers.values.compactMap { $0 as? [String: Any] })
        }
        // Self too: someone may run the client on the Spark itself.
        if let own = root["Self"] as? [String: Any] { nodes.append(own) }

        return nodes.compactMap { node -> URL? in
            guard node["Online"] as? Bool == true,
                  let ips = node["TailscaleIPs"] as? [String],
                  // IPv4 only: the agent binds 0.0.0.0, so its v6 address would
                  // resolve and then refuse the connection.
                  let ip = ips.first(where: { !$0.contains(":") })
            else { return nil }
            return URL(string: "http://\(ip):\(defaultPort)")
        }
    }

    // MARK: Bonjour

    static func bonjourCandidates(timeout: TimeInterval = 3) async -> [URL] {
        let endpoints = await browse(timeout: timeout)
        var urls: [URL] = []
        for endpoint in endpoints {
            if let resolved = await resolve(endpoint), let url = URL(string: "http://\(resolved)") {
                urls.append(url)
            }
        }
        return urls
    }

    /// Browser callbacks and the deadline both touch the same state, so they all
    /// run on one serial queue and the state lives in a reference type — the
    /// alternative is a data race the compiler is right to complain about.
    private final class BrowseState {
        var finished = false
        var found: Set<NWEndpoint> = []
    }

    private static func browse(timeout: TimeInterval) async -> [NWEndpoint] {
        await withCheckedContinuation { continuation in
            let queue = DispatchQueue(label: "ai.dgx-spark-bar.browse")
            let state = BrowseState()
            let browser = NWBrowser(
                for: .bonjour(type: "_dgx-spark-bar._tcp", domain: nil),
                using: .init()
            )

            let finish = {
                guard !state.finished else { return }
                state.finished = true
                browser.cancel()
                continuation.resume(returning: Array(state.found))
            }

            browser.browseResultsChangedHandler = { results, _ in
                state.found = Set(results.map(\.endpoint))
            }
            browser.stateUpdateHandler = { browserState in
                if case .failed = browserState { finish() }
            }
            browser.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { finish() }
        }
    }

    /// A Bonjour result carries a service name, not an address. The sanctioned
    /// way to get one is to open a connection and read the path it settled on.
    private final class ResolveState {
        var finished = false
    }

    private static func resolve(_ endpoint: NWEndpoint) async -> String? {
        await withCheckedContinuation { continuation in
            let queue = DispatchQueue(label: "ai.dgx-spark-bar.resolve")
            let state = ResolveState()
            let connection = NWConnection(to: endpoint, using: .tcp)

            let finish: (String?) -> Void = { value in
                guard !state.finished else { return }
                state.finished = true
                connection.cancel()
                continuation.resume(returning: value)
            }

            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    guard case let .hostPort(host, port) = connection.currentPath?.remoteEndpoint else {
                        finish(nil)
                        return
                    }
                    var name = "\(host)"
                    // Strip the interface zone: "fe80::1%en0" is not a URL host.
                    if let zone = name.firstIndex(of: "%") { name = String(name[name.startIndex..<zone]) }
                    if name.contains(":") { name = "[\(name)]" }
                    finish("\(name):\(port.rawValue)")
                case .failed, .cancelled:
                    finish(nil)
                default:
                    break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + 3) { finish(nil) }
        }
    }

    // MARK: manual

    static var manualCandidates: [URL] {
        (UserDefaults.standard.array(forKey: "manualAgents") as? [String] ?? [])
            .compactMap(URL.init(string:))
    }

    static func addManual(_ raw: String) {
        var text = raw.trimmingCharacters(in: .whitespaces)
        if text.isEmpty { return }
        if !text.contains("://") { text = "http://" + text }
        if URL(string: text)?.port == nil { text += ":\(defaultPort)" }
        guard let url = URL(string: text) else { return }

        var stored = UserDefaults.standard.array(forKey: "manualAgents") as? [String] ?? []
        if !stored.contains(url.absoluteString) {
            stored.append(url.absoluteString)
            UserDefaults.standard.set(stored, forKey: "manualAgents")
        }
    }

    // MARK: combined

    /// Probes every candidate and keeps the ones that answer as dgx-spark-bar.
    /// Deduplicated by machine id: one box found on two channels is one row,
    /// and the tailnet address wins because it keeps working off-site.
    static func discover() async -> [Agent] {
        var candidates: [(URL, DiscoverySource)] = []
        let tailnet = tailnetCandidates()
        let bonjour = await bonjourCandidates()
        let manual = manualCandidates
        Log.debug("candidates — tailnet: \(tailnet.map(\.absoluteString)), "
                  + "bonjour: \(bonjour.map(\.absoluteString)), manual: \(manual.map(\.absoluteString))")
        candidates += tailnet.map { ($0, .tailnet) }
        candidates += bonjour.map { ($0, .bonjour) }
        candidates += manual.map { ($0, .manual) }

        var byMachine: [String: Agent] = [:]
        await withTaskGroup(of: (URL, DiscoverySource, Ping?).self) { group in
            for (url, source) in candidates {
                group.addTask {
                    do {
                        return (url, source, try await AgentClient.ping(url))
                    } catch {
                        Log.debug("probe \(url.absoluteString) error: \(error)")
                        return (url, source, nil)
                    }
                }
            }
            for await (url, source, ping) in group {
                Log.debug("probe \(url.absoluteString) [\(source.rawValue)] → \(ping.map { "\($0.host) (\($0.machineId))" } ?? "no answer")")
                guard let ping else { continue }
                let agent = Agent(machineId: ping.machineId, host: ping.host,
                                  baseURL: url, source: source)
                if let existing = byMachine[ping.machineId], existing.source == .tailnet { continue }
                byMachine[ping.machineId] = agent
            }
        }
        return byMachine.values.sorted { $0.host < $1.host }
    }
}
