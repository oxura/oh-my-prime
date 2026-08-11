import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ensureKernelPython } from "../src/core/kernel/bootstrap.js";
import { KernelManager } from "../src/core/kernel/index.js";
import {
	createCapabilityKernelOptions,
	loadPersistedRlmCapabilityManifest,
	normalizeRlmCapabilityManifest,
	persistRlmCapabilityManifest,
	type RlmCapabilityManifest,
} from "../src/core/rlm-capabilities.js";

const TWENTY_MINUTES_MS = 20 * 60 * 1000;
const FOUR_GIB_BYTES = 4 * 1024 * 1024 * 1024;
function defaultManifest(cwd: string): RlmCapabilityManifest {
	return {
		filesystem: { read: [cwd], write: [cwd] },
		network: { allow: [], deny_by_default: true },
		secrets: { allow: [] },
		process: { wall_time_ms: TWENTY_MINUTES_MS, max_processes: 64 },
	};
}

describe("RLM child capability manifests", () => {
	let tempDir: string;
	let cwd: string;

	beforeEach(() => {
		tempDir = mkdtempSync(join(tmpdir(), "prime-rlm-capabilities-"));
		cwd = join(tempDir, "project");
		mkdirSync(join(cwd, "src"), { recursive: true });
	});

	afterEach(() => {
		rmSync(tempDir, { recursive: true, force: true });
	});

	it("defaults every child to project-only filesystem access, deny-all network, no secrets, and bounded processes", () => {
		expect(normalizeRlmCapabilityManifest(undefined, cwd)).toEqual(defaultManifest(cwd));
		expect(normalizeRlmCapabilityManifest({ network: false }, cwd)).toEqual(defaultManifest(cwd));
	});

	it("canonicalizes relative filesystem paths and human-readable resource limits", () => {
		expect(
			normalizeRlmCapabilityManifest(
				{
					filesystem: { read: [".", "./src"], write: ["./src"] },
					network: { allow: ["api.example.com"], deny_by_default: true },
					secrets: { allow: ["CHILD_API_TOKEN"] },
					process: { cpu: 2, memory_bytes: "4gb", wall_time_ms: "20m", max_processes: 12 },
				},
				cwd,
			),
		).toEqual({
			filesystem: { read: [cwd, join(cwd, "src")], write: [join(cwd, "src")] },
			network: { allow: ["api.example.com"], deny_by_default: true },
			secrets: { allow: ["CHILD_API_TOKEN"] },
			process: {
				cpu: 2,
				memory_bytes: FOUR_GIB_BYTES,
				wall_time_ms: TWENTY_MINUTES_MS,
				max_processes: 12,
			},
		});
	});

	it.each([
		{ label: "non-object", value: "sandbox" },
		{ label: "null", value: null },
		{ label: "unknown top-level key", value: { filesystem: {}, typo: true } },
		{ label: "unknown filesystem key", value: { filesystem: { execute: ["."] } } },
		{ label: "unknown network key", value: { network: { allow: [], default_deny: true } } },
		{ label: "unknown secrets key", value: { secrets: { allow: [], inherit: true } } },
		{ label: "unknown process key", value: { process: { memory_limit: "4gb" } } },
		{ label: "malformed memory", value: { process: { memory_bytes: "four gb" } } },
		{ label: "malformed wall time", value: { process: { wall_time_ms: "eventually" } } },
		{ label: "fractional CPUs", value: { process: { cpu: 1.5 } } },
		{ label: "zero process count", value: { process: { max_processes: 0 } } },
	])("rejects $label instead of silently dropping it", ({ value }) => {
		expect(() => normalizeRlmCapabilityManifest(value, cwd)).toThrow();
	});

	it.each([
		{ label: "empty path", value: { filesystem: { read: [""], write: [] } } },
		{ label: "NUL path", value: { filesystem: { read: ["src\0escape"], write: [] } } },
		{ label: "URL where a domain is required", value: { network: { allow: ["https://example.com"] } } },
		{ label: "domain containing a path", value: { network: { allow: ["example.com/v1"] } } },
		{ label: "domain containing a port", value: { network: { allow: ["example.com:443"] } } },
		{ label: "invalid environment name", value: { secrets: { allow: ["API-KEY"] } } },
		{ label: "environment assignment", value: { secrets: { allow: ["API_KEY=value"] } } },
		{ label: "non-string environment name", value: { secrets: { allow: [42] } } },
	])("fails closed on $label", ({ value }) => {
		expect(() => normalizeRlmCapabilityManifest(value, cwd)).toThrow();
	});

	it("inherits an omitted nested manifest exactly and permits only narrowing", () => {
		const parent = normalizeRlmCapabilityManifest(
			{
				filesystem: { read: [cwd], write: [cwd] },
				network: { allow: ["api.example.com", "static.example.com"], deny_by_default: true },
				secrets: { allow: ["PARENT_TOKEN", "SHARED_TOKEN"] },
				process: { cpu: 4, memory_bytes: "4gb", wall_time_ms: "20m", max_processes: 32 },
			},
			cwd,
		);

		expect(normalizeRlmCapabilityManifest(undefined, cwd, parent)).toEqual(parent);
		expect(
			normalizeRlmCapabilityManifest(
				{
					filesystem: { read: ["./src"], write: ["./src"] },
					network: { allow: ["api.example.com"], deny_by_default: true },
					secrets: { allow: ["SHARED_TOKEN"] },
					process: { cpu: 2, memory_bytes: "2gb", wall_time_ms: "10m", max_processes: 16 },
				},
				cwd,
				parent,
			),
		).toEqual({
			filesystem: { read: [join(cwd, "src")], write: [join(cwd, "src")] },
			network: { allow: ["api.example.com"], deny_by_default: true },
			secrets: { allow: ["SHARED_TOKEN"] },
			process: {
				cpu: 2,
				memory_bytes: 2 * 1024 * 1024 * 1024,
				wall_time_ms: 10 * 60 * 1000,
				max_processes: 16,
			},
		});
	});

	it.each([
		{
			label: "filesystem",
			value: { filesystem: { read: [".."], write: ["."] } },
		},
		{
			label: "network",
			value: { network: { allow: ["new.example.com"], deny_by_default: true } },
		},
		{
			label: "network default",
			value: { network: { allow: [], deny_by_default: false } },
		},
		{
			label: "secrets",
			value: { secrets: { allow: ["NEW_TOKEN"] } },
		},
		{
			label: "CPU",
			value: { process: { cpu: 5 } },
		},
		{
			label: "memory",
			value: { process: { memory_bytes: "8gb" } },
		},
		{
			label: "wall time",
			value: { process: { wall_time_ms: "30m" } },
		},
		{
			label: "process count",
			value: { process: { max_processes: 65 } },
		},
	])("rejects nested $label escalation", ({ value }) => {
		const parent = normalizeRlmCapabilityManifest(
			{
				filesystem: { read: [cwd], write: [cwd] },
				network: { allow: ["api.example.com"], deny_by_default: true },
				secrets: { allow: ["PARENT_TOKEN"] },
				process: { cpu: 4, memory_bytes: "4gb", wall_time_ms: "20m", max_processes: 64 },
			},
			cwd,
		);
		expect(() => normalizeRlmCapabilityManifest(value, cwd, parent)).toThrow(/widen|escalat|parent|not supported/i);
	});

	it("atomically persists a private canonical manifest and rejects a tampered file", () => {
		const artifactDir = join(tempDir, "artifact");
		const manifest = normalizeRlmCapabilityManifest(
			{
				filesystem: { read: [cwd], write: ["./src"] },
				network: false,
				secrets: { allow: [] },
			},
			cwd,
		);

		persistRlmCapabilityManifest(artifactDir, manifest);
		const files = readdirSync(artifactDir);
		const manifestFiles = files.filter((name) => name.endsWith(".json"));
		expect(manifestFiles).toHaveLength(1);
		expect(files.filter((name) => name.endsWith(".tmp"))).toEqual([]);
		const manifestPath = join(artifactDir, manifestFiles[0]!);
		expect(statSync(manifestPath).mode & 0o777).toBe(0o600);
		expect(loadPersistedRlmCapabilityManifest(artifactDir, cwd)).toEqual(manifest);

		const persisted = JSON.parse(readFileSync(manifestPath, "utf8")) as Record<string, unknown>;
		writeFileSync(manifestPath, JSON.stringify({ ...persisted, unexpected_grant: true }), { mode: 0o600 });
		expect(() => loadPersistedRlmCapabilityManifest(artifactDir, cwd)).toThrow();
	});

	it("passes only explicitly granted secrets into a non-inheriting kernel environment", () => {
		const artifactDir = join(tempDir, "filtered-env");
		const manifest = normalizeRlmCapabilityManifest(
			{
				filesystem: { read: [cwd], write: [cwd] },
				network: false,
				secrets: { allow: ["GRANTED_CHILD_TOKEN"] },
			},
			cwd,
		);
		const options = createCapabilityKernelOptions({
			manifest,
			cwd,
			artifactDir,
			baseEnv: {
				PATH: "/test/bin",
				HOME: "/test/home",
				GRANTED_CHILD_TOKEN: "allowed-value",
				UNGRANTED_PARENT_TOKEN: "must-not-leak",
				ANTHROPIC_API_KEY: "must-not-leak-either",
				PRIME_AGENT_CODING_AGENT_DIR: "/test/private-agent",
				RLM_GLOBAL_HARNESS_STATE_DIR: "/test/global-harness",
			},
			pythonSkills: [],
		});

		expect(options.inheritEnv).toBe(false);
		expect(options.transport).toBe("ipc");
		expect(options.env?.GRANTED_CHILD_TOKEN).toBe("allowed-value");
		expect(options.env).not.toHaveProperty("UNGRANTED_PARENT_TOKEN");
		expect(options.env).not.toHaveProperty("ANTHROPIC_API_KEY");
		expect(options.env).not.toHaveProperty("PRIME_AGENT_CODING_AGENT_DIR");
		expect(options.env).not.toHaveProperty("RLM_GLOBAL_HARNESS_STATE_DIR");
		expect(options.env?.XDG_STATE_HOME).toBe(join(artifactDir, "kernel-runtime", "state"));
	});

	it("builds independent per-child SRT configs and wraps every kernel launch with the CLI", () => {
		const firstArtifactDir = join(tempDir, "child-a");
		const secondArtifactDir = join(tempDir, "child-b");
		const childSessionDir = join(tempDir, "child-a-control");
		const childHarnessDir = join(childSessionDir, "harness");
		const first = createCapabilityKernelOptions({
			manifest: normalizeRlmCapabilityManifest(
				{
					network: { allow: ["one.example.com"], deny_by_default: true },
					process: {
						cpu: 2,
						memory_bytes: "4gb",
						wall_time_ms: "20m",
						max_processes: 9,
					},
				},
				cwd,
			),
			cwd,
			artifactDir: firstArtifactDir,
			baseEnv: {
				PATH: process.env.PATH ?? "/usr/bin:/bin",
				RLM_SESSION_DIR: childSessionDir,
				RLM_HARNESS_STATE_DIR: childHarnessDir,
			},
			pythonSkills: [],
		});
		const second = createCapabilityKernelOptions({
			manifest: normalizeRlmCapabilityManifest(
				{
					network: { allow: ["two.example.com"], deny_by_default: true },
					process: {
						cpu: 2,
						memory_bytes: "4gb",
						wall_time_ms: "20m",
						max_processes: 9,
					},
				},
				cwd,
			),
			cwd,
			artifactDir: secondArtifactDir,
			baseEnv: { PATH: process.env.PATH ?? "/usr/bin:/bin" },
			pythonSkills: [],
		});
		if (!first.processWrapper || !second.processWrapper) {
			throw new Error("Capability kernel options omitted the required sandbox process wrapper");
		}
		const launch = { command: "/usr/bin/python3", args: ["-m", "ipykernel_launcher"] };
		const firstLaunch = first.processWrapper(launch);
		const secondLaunch = second.processWrapper(launch);
		const firstConfigPath = join(firstArtifactDir, "srt-settings.json");
		const secondConfigPath = join(secondArtifactDir, "srt-settings.json");

		expect(firstLaunch).toMatchObject({
			command: process.execPath,
			args: [
				expect.stringMatching(/@anthropic-ai[/\\]sandbox-runtime[/\\]dist[/\\]cli\.js$/),
				"--settings",
				firstConfigPath,
				launch.command,
				...launch.args,
			],
		});
		expect(secondLaunch.args[2]).toBe(secondConfigPath);
		expect(secondLaunch.args[2]).not.toBe(firstLaunch.args[2]);
		expect(firstLaunch.command).not.toBe(launch.command);
		expect(first.transport).toBe("ipc");
		expect(second.transport).toBe("ipc");
		expect(first.resourceLimits).toEqual({
			cpu: 2,
			memoryBytes: FOUR_GIB_BYTES,
			wallTimeMs: TWENTY_MINUTES_MS,
			maxProcesses: 9,
		});

		const firstConfig = JSON.parse(readFileSync(firstConfigPath, "utf8")) as {
			filesystem?: { allowRead?: string[]; allowWrite?: string[] };
			network?: {
				allowedDomains?: string[];
				strictAllowlist?: boolean;
				allowUnixSockets?: string[];
				allowAllUnixSockets?: boolean;
			};
		};
		const secondConfig = JSON.parse(readFileSync(secondConfigPath, "utf8")) as {
			network?: {
				allowedDomains?: string[];
				strictAllowlist?: boolean;
				allowUnixSockets?: string[];
				allowAllUnixSockets?: boolean;
			};
		};
		expect(firstConfig.network).toMatchObject({
			allowedDomains: ["one.example.com"],
			strictAllowlist: true,
			allowUnixSockets: [join(firstArtifactDir, "kernel-runtime")],
			allowAllUnixSockets: true,
		});
		expect(firstConfig.filesystem?.allowRead).not.toContain(childSessionDir);
		expect(firstConfig.filesystem?.allowWrite).toContain(childHarnessDir);
		expect(firstConfig.filesystem?.allowWrite).not.toContain(childSessionDir);
		expect(secondConfig.network).toMatchObject({
			allowedDomains: ["two.example.com"],
			strictAllowlist: true,
			allowUnixSockets: [join(secondArtifactDir, "kernel-runtime")],
			allowAllUnixSockets: true,
		});
		expect(statSync(firstConfigPath).mode & 0o777).toBe(0o600);
		expect(statSync(secondConfigPath).mode & 0o777).toBe(0o600);
	});
	it.skipIf(
		process.platform !== "linux" ||
			!existsSync("/usr/bin/bwrap") ||
			!existsSync("/usr/bin/socat") ||
			!existsSync("/usr/bin/rg"),
	)(
		"runs a real persistent kernel with filesystem, network, and secret denial",
		async () => {
			const artifactDir = join(tempDir, "live-sandbox");
			const childSessionDir = join(tempDir, "live-child-control");
			const childHarnessDir = join(childSessionDir, "harness");
			mkdirSync(childHarnessDir, { recursive: true, mode: 0o700 });
			const childPolicyPath = join(childSessionDir, "srt-settings.json");
			writeFileSync(childPolicyPath, "host-owned", { mode: 0o600 });
			const pythonSkills = [
				{
					name: "flow",
					importName: "flow",
					packagePath: join(process.cwd(), "skills", "flow"),
					pyprojectPath: join(process.cwd(), "skills", "flow", "pyproject.toml"),
				},
			];
			const outside = join(tempDir, "outside-secret.txt");
			writeFileSync(outside, "must-not-read", { mode: 0o600 });
			const hostSocket = join(cwd, "host-service.sock");
			const server = createServer((socket) => socket.destroy());
			await new Promise<void>((resolveListening, rejectListening) => {
				server.once("error", rejectListening);
				server.listen(0, "127.0.0.1", resolveListening);
			});
			const unixServer = createServer((socket) => socket.destroy());
			await new Promise<void>((resolveListening, rejectListening) => {
				unixServer.once("error", rejectListening);
				unixServer.listen(hostSocket, resolveListening);
			});
			const address = server.address();
			if (!address || typeof address === "string") {
				throw new Error("Sandbox test server did not bind a TCP port");
			}
			const capabilityOptions = createCapabilityKernelOptions({
				manifest: normalizeRlmCapabilityManifest(undefined, cwd),
				cwd,
				artifactDir,
				baseEnv: {
					PATH: process.env.PATH ?? "/usr/bin:/bin",
					HOME: process.env.HOME ?? tempDir,
					UNGRANTED_PARENT_TOKEN: "must-not-leak",
					PRIME_AGENT_CODING_AGENT_DIR: tempDir,
					RLM_GLOBAL_HARNESS_STATE_DIR: join(tempDir, "global-harness"),
					RLM_SESSION_DIR: childSessionDir,
					RLM_HARNESS_STATE_DIR: childHarnessDir,
				},
				pythonSkills,
			});
			const python = await ensureKernelPython({ pythonSkills });
			const manager = new KernelManager({
				cwd,
				python,
				...capabilityOptions,
			});
			try {
				const result = await manager.execute(`
import json
import os
import flow
import socket
from pathlib import Path

outside_read = True
try:
    Path(${JSON.stringify(outside)}).read_text()
except OSError:
    outside_read = False

network_connected = True
try:
    with socket.create_connection(("127.0.0.1", ${address.port}), timeout=0.2):
        pass
except OSError:
    network_connected = False

unix_connected = True
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(${JSON.stringify(hostSocket)})
except OSError:
    unix_connected = False

state_root = Path(os.environ["XDG_STATE_HOME"])
state_root.mkdir(parents=True, exist_ok=True)
(state_root / "flow-state.json").write_text("private")
try:
    Path(os.environ["RLM_SESSION_DIR"], "srt-settings.json").write_text("attacker")
    (Path(os.environ["RLM_SESSION_DIR"]) / "sub-attacker").mkdir()
except OSError:
    pass

Path(os.environ["RLM_HARNESS_STATE_DIR"], "local-memory.json").write_text("allowed")


Path("inside.txt").write_text("allowed")
print(json.dumps({
    "inside": Path("inside.txt").read_text(),
    "outside_read": outside_read,
    "network_connected": network_connected,
    "unix_connected": unix_connected,
    "private_state": (state_root / "flow-state.json").read_text(),
    "local_harness": Path(os.environ["RLM_HARNESS_STATE_DIR"], "local-memory.json").read_text(),
    "agent_dir": os.environ.get("PRIME_AGENT_CODING_AGENT_DIR"),
    "flow_module": flow.__name__,
    "global_harness": os.environ.get("RLM_GLOBAL_HARNESS_STATE_DIR"),
    "secret": os.environ.get("UNGRANTED_PARENT_TOKEN"),
}))
`);
				expect(result.status, JSON.stringify(result)).toBe("ok");
				expect(JSON.parse(result.stdout.trim())).toEqual({
					inside: "allowed",
					outside_read: false,
					network_connected: false,
					unix_connected: false,
					local_harness: "allowed",
					private_state: "private",
					agent_dir: null,
					global_harness: null,
					flow_module: "flow",
					secret: null,
				});
				expect(readFileSync(childPolicyPath, "utf8")).toBe("host-owned");
				expect(existsSync(join(childSessionDir, "sub-attacker"))).toBe(false);
			} finally {
				await manager.dispose();
				await new Promise<void>((resolveClosed, rejectClosed) => {
					server.close((error) => (error ? rejectClosed(error) : resolveClosed()));
				});
				await new Promise<void>((resolveClosed, rejectClosed) => {
					unixServer.close((error) => (error ? rejectClosed(error) : resolveClosed()));
				});
			}
		},
		60_000,
	);
});
