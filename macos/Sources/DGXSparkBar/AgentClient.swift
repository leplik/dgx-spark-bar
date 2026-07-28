import Foundation

enum AgentError: LocalizedError {
    case badResponse(Int)
    case notSparkbar

    var errorDescription: String? {
        switch self {
        case .badResponse(let code): "Agent replied \(code)"
        case .notSparkbar: "Not a dgx-spark-bar agent"
        }
    }
}

struct AgentClient {
    /// Short, but not as short as a LAN round trip: the first connection over a
    /// tailnet has to warm the path (and may start out relayed through DERP),
    /// which measurably exceeds 2 s — at which point discovery finds the box on
    /// the LAN only, and the moment you leave the office it finds nothing.
    static let probeTimeout: TimeInterval = 5
    static let statusTimeout: TimeInterval = 5

    private static let decoder = JSONDecoder()

    private static func session(_ timeout: TimeInterval) -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }

    static func ping(_ baseURL: URL) async throws -> Ping {
        let (data, response) = try await session(probeTimeout)
            .data(from: baseURL.appendingPathComponent("ping"))
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw AgentError.badResponse((response as? HTTPURLResponse)?.statusCode ?? 0)
        }
        guard let ping = try? decoder.decode(Ping.self, from: data), ping.app == "dgx-spark-bar" else {
            throw AgentError.notSparkbar
        }
        return ping
    }

    static func status(_ agent: Agent) async throws -> Status {
        let request = URLRequest(url: agent.baseURL.appendingPathComponent("status"))
        let (data, response) = try await session(statusTimeout).data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else { throw AgentError.badResponse(code) }
        return try decoder.decode(Status.self, from: data)
    }

    @discardableResult
    static func run(_ action: String, on agent: Agent) async throws -> String {
        var request = URLRequest(url: agent.baseURL.appendingPathComponent("action"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["action": action])

        // A stack restart pulls images and waits on health checks; give it room.
        let (data, response) = try await session(120).data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else { throw AgentError.badResponse(code) }
        return String(data: data, encoding: .utf8) ?? ""
    }
}
