import Foundation
import SwiftUI

@MainActor
final class Store: ObservableObject {
    /// One instance shared by the app delegate (which starts polling) and the
    /// menu view (which renders it).
    static let shared = Store()

    @Published private(set) var agents: [Agent] = []
    @Published private(set) var statuses: [String: Status] = [:]
    @Published private(set) var failures: [String: String] = [:]
    @Published private(set) var lastRefresh: Date?
    @Published private(set) var isSearching = false
    @Published var busy: String?
    @Published var actionResult: String?

    private let pollInterval: TimeInterval = 5
    private let rediscoverEvery: TimeInterval = 30
    private var loop: Task<Void, Never>?

    // MARK: lifecycle

    func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            var lastDiscovery = Date.distantPast
            while !Task.isCancelled {
                guard let self else { return }
                if Date().timeIntervalSince(lastDiscovery) >= self.rediscoverEvery || self.agents.isEmpty {
                    await self.rediscover()
                    lastDiscovery = Date()
                }
                await self.refresh()
                try? await Task.sleep(nanoseconds: UInt64(self.pollInterval * 1_000_000_000))
            }
        }
    }

    func stop() {
        loop?.cancel()
        loop = nil
    }

    func rediscover() async {
        isSearching = agents.isEmpty
        let found = await Discovery.discover()
        agents = found
        // Forget statuses of boxes that went away, so the menu never shows a
        // reading that is minutes old as if it were current.
        let live = Set(found.map(\.machineId))
        statuses = statuses.filter { live.contains($0.key) }
        failures = failures.filter { live.contains($0.key) }
        isSearching = false
    }

    func refresh() async {
        let current = agents
        Log.debug("refresh: \(current.count) agent(s)")
        await withTaskGroup(of: (String, Result<Status, Error>).self) { group in
            for agent in current {
                group.addTask {
                    do {
                        return (agent.machineId, .success(try await AgentClient.status(agent)))
                    } catch {
                        return (agent.machineId, .failure(error))
                    }
                }
            }
            for await (machineId, result) in group {
                switch result {
                case .success(let status):
                    statuses[machineId] = status
                    failures[machineId] = nil
                case .failure(let error):
                    Log.debug("status \(machineId) failed: \(error.localizedDescription)")
                    failures[machineId] = error.localizedDescription
                    statuses[machineId] = nil
                }
            }
        }
        lastRefresh = Date()
    }

    // MARK: derived state

    /// Worst state across all boxes — the menu bar has room for exactly one dot.
    var health: Health {
        guard !agents.isEmpty else { return .unknown }
        if agents.contains(where: { failures[$0.machineId] != nil }) { return .error }
        let levels = agents.compactMap { statuses[$0.machineId]?.level }
        if levels.isEmpty { return .unknown }
        if levels.contains("error") { return .error }
        if levels.contains("warn") { return .warn }
        return .ok
    }

    func status(for agent: Agent) -> Status? { statuses[agent.machineId] }
    func failure(for agent: Agent) -> String? { failures[agent.machineId] }

    // MARK: actions

    func perform(_ action: String, on agent: Agent) {
        busy = "\(agent.machineId):\(action)"
        Task {
            defer { busy = nil }
            do {
                _ = try await AgentClient.run(action, on: agent)
                actionResult = "\(action): sent"
                // poweroff/reboot take the box away; a refresh right now would
                // only race the shutdown, so give it a moment either way.
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                await refresh()
            } catch {
                actionResult = "\(action): \(error.localizedDescription)"
            }
        }
    }
}
