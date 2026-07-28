import SwiftUI

extension Health {
    var color: Color {
        switch self {
        case .ok: .green
        case .warn: .yellow
        case .error: .red
        case .unknown: .secondary
        }
    }
}

/// Percentage series are drawn against a fixed 0–100 scale on purpose:
/// auto-scaling makes an idle box look as busy as a loaded one.
struct Sparkline: View {
    let values: [Double]
    let scale: Double
    let tint: Color

    var body: some View {
        Canvas { context, size in
            guard values.count > 1 else { return }
            let top = max(scale, 1)
            var path = Path()
            for (index, value) in values.enumerated() {
                let x = size.width * CGFloat(index) / CGFloat(values.count - 1)
                let y = size.height * (1 - CGFloat(min(value, top) / top))
                index == 0 ? path.move(to: CGPoint(x: x, y: y)) : path.addLine(to: CGPoint(x: x, y: y))
            }
            context.stroke(path, with: .color(tint), lineWidth: 1.5)
        }
        .frame(height: 18)
    }
}

struct MetricRow: View {
    let label: String
    let value: String
    var detail: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(label)
                .foregroundStyle(.secondary)
                .frame(width: 62, alignment: .leading)
            Text(value)
            if let detail {
                Text(detail).foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
        .font(.system(size: 12))
    }
}

struct MenuView: View {
    @ObservedObject var store: Store
    @State private var manualEntry = ""
    @State private var confirming: (agent: Agent, action: String)?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if store.agents.isEmpty {
                emptyState
            } else {
                ForEach(store.agents) { agent in
                    agentSection(agent)
                    if agent.id != store.agents.last?.id { Divider() }
                }
            }
            Divider()
            footer
        }
        .padding(12)
        .frame(width: 340)
        .confirmationDialog(
            confirming.map { "\($0.action == "poweroff" ? "Shut down" : "Reboot") \($0.agent.host)?" } ?? "",
            isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
            titleVisibility: .visible
        ) {
            Button(confirming?.action == "poweroff" ? "Shut down" : "Reboot", role: .destructive) {
                if let confirming { store.perform(confirming.action, on: confirming.agent) }
                confirming = nil
            }
            Button("Cancel", role: .cancel) { confirming = nil }
        } message: {
            // The one irreversible detail: nothing on the network can turn a
            // powered-off Spark back on.
            Text(confirming?.action == "poweroff"
                 ? "Only the physical button can turn it back on."
                 : "It will be back in about a minute.")
        }
    }

    // MARK: sections

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                if store.isSearching { ProgressView().controlSize(.small) }
                Text(store.isSearching ? "Looking for a Spark…" : "No Spark found")
                    .font(.system(size: 13, weight: .medium))
            }
            Text("Searched the tailnet and the local network. If the box is reachable at a known address, add it:")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                TextField("192.168.1.50:8765", text: $manualEntry)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11))
                Button("Add") {
                    Discovery.addManual(manualEntry)
                    manualEntry = ""
                    Task { await store.rediscover() }
                }
                .disabled(manualEntry.isEmpty)
            }
        }
    }

    @ViewBuilder
    private func agentSection(_ agent: Agent) -> some View {
        let status = store.status(for: agent)
        let health = store.failure(for: agent) != nil ? Health.error : Health(level: status?.level)

        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Circle().fill(health.color).frame(width: 9, height: 9)
                Text(agent.host).font(.system(size: 13, weight: .semibold))
                Text(agent.source == .tailnet ? "tailnet" : agent.source == .bonjour ? "LAN" : "manual")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                Spacer()
                if let status {
                    Text(uptime(status.uptimeSec)).font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }

            if let failure = store.failure(for: agent) {
                Text(failure).font(.system(size: 11)).foregroundStyle(.red)
            } else if let status {
                metrics(status)
                if !status.findings.isEmpty { findings(status.findings) }
                services(status)
                actions(agent, status)
            } else {
                Text("…").foregroundStyle(.secondary).font(.system(size: 11))
            }
        }
    }

    @ViewBuilder
    private func metrics(_ status: Status) -> some View {
        let gpuSeries = status.history.map(\.gpu)
        let cpuSeries = status.history.map(\.cpu)

        VStack(alignment: .leading, spacing: 3) {
            if status.gpu.present {
                MetricRow(
                    label: "GPU",
                    value: "\(Int(status.gpu.utilPct ?? 0))%",
                    detail: [
                        status.gpu.tempC.map { "\(Int($0))°C" },
                        status.gpu.powerW.map { String(format: "%.0f W", $0) },
                        status.gpu.smClockMhz.map { "\(Int($0)) MHz" },
                    ].compactMap { $0 }.joined(separator: " · ")
                )
                Sparkline(values: gpuSeries, scale: 100, tint: .green)
            }
            MetricRow(
                label: "CPU",
                value: "\(Int(status.cpu.pct ?? 0))%",
                detail: status.cpu.tempC.map { "\(Int($0))°C" }
            )
            Sparkline(values: cpuSeries, scale: 100, tint: .blue)

            // On GB10 there is no separate VRAM — saying so here stops the
            // "where is my VRAM reading" question before it starts.
            MetricRow(
                label: "Memory",
                value: String(format: "%.0f / %.0f GB", status.memory.usedGb, status.memory.totalGb),
                detail: "shared with GPU"
            )
            ForEach(status.disks) { disk in
                MetricRow(
                    label: "Disk",
                    value: String(format: "%.0f%% used", disk.pct),
                    detail: String(format: "%.0f GB free", disk.freeGb)
                )
            }
            MetricRow(
                label: "Network",
                value: status.net.iface ?? "—",
                detail: String(format: "↓%.1f ↑%.1f MB/s", status.net.rxMbs ?? 0, status.net.txMbs ?? 0)
            )
        }
    }

    private func findings(_ items: [Finding]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(items) { item in
                HStack(alignment: .top, spacing: 5) {
                    Image(systemName: item.isCritical ? "exclamationmark.triangle.fill" : "exclamationmark.circle")
                        .foregroundStyle(item.isCritical ? .red : .yellow)
                        .font(.system(size: 10))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(item.title).font(.system(size: 11, weight: .medium))
                        Text(item.detail).font(.system(size: 10)).foregroundStyle(.secondary)
                        if let hint = item.hint, !hint.isEmpty {
                            Text(hint).font(.system(size: 10)).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
        }
        .padding(6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.yellow.opacity(0.10), in: RoundedRectangle(cornerRadius: 5))
    }

    @ViewBuilder
    private func services(_ status: Status) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(status.docker) { container in
                HStack(spacing: 5) {
                    Circle()
                        .fill(container.isRunning ? Color.green : Color.red)
                        .frame(width: 5, height: 5)
                    Text(container.name).font(.system(size: 11))
                    Text(container.status ?? container.state)
                        .font(.system(size: 10)).foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            if let loaded = status.ollama.loaded, !loaded.isEmpty {
                ForEach(loaded) { model in
                    Text("\(model.name) — \(String(format: "%.1f", model.memGb)) GB resident")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
            } else if status.ollama.reachable == true {
                Text("no model loaded · \(status.ollama.modelCount ?? 0) on disk")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            ForEach(status.jobs.filter { $0.running == true }) { job in
                Text("running: \(job.name)")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }
        }
    }

    @ViewBuilder
    private func actions(_ agent: Agent, _ status: Status) -> some View {
        let running = store.busy?.hasPrefix(agent.machineId) == true

        HStack(spacing: 6) {
            if status.actions.contains("restart-stack") {
                Button("Restart stack") { store.perform("restart-stack", on: agent) }
            }
            if status.actions.contains("reboot") {
                Button("Reboot") { confirming = (agent, "reboot") }
            }
            if status.actions.contains("poweroff") {
                Button("Shut down") { confirming = (agent, "poweroff") }
            }
            if running { ProgressView().controlSize(.small) }
        }
        .font(.system(size: 11))
        .disabled(running)

        if let url = status.webUiUrl, !url.isEmpty {
            Button("Open web chat") { openWebUI(agent, path: url) }
                .buttonStyle(.link)
                .font(.system(size: 11))
        }
    }

    private var footer: some View {
        HStack {
            if let result = store.actionResult {
                Text(result).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            } else if let last = store.lastRefresh {
                Text("updated \(last.formatted(date: .omitted, time: .standard))")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Spacer()
            Button("Refresh") { Task { await store.rediscover(); await store.refresh() } }
                .font(.system(size: 11))
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .font(.system(size: 11))
        }
    }

    // MARK: helpers

    /// The agent reports the web UI as its own localhost URL; rewrite the host
    /// to whichever address we actually reached the box on.
    private func openWebUI(_ agent: Agent, path: String) {
        guard var components = URLComponents(string: path) else { return }
        components.host = agent.baseURL.host
        guard let url = components.url else { return }
        NSWorkspace.shared.open(url)
    }

    private func uptime(_ seconds: Double) -> String {
        let total = Int(seconds)
        let days = total / 86400, hours = (total % 86400) / 3600, minutes = (total % 3600) / 60
        return days > 0 ? "up \(days)d \(hours)h" : "up \(hours)h \(minutes)m"
    }
}
