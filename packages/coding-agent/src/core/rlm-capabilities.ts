import {
	chmodSync,
	existsSync,
	lstatSync,
	mkdirSync,
	readdirSync,
	readFileSync,
	readlinkSync,
	realpathSync,
	renameSync,
	rmSync,
	type Stats,
	writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { delimiter, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { getPackageDir, isBunBinary } from "../config.js";
import type { KernelProcessLaunchDescriptor, KernelProcessWrapper, KernelResourceLimits } from "./kernel/index.js";
import type { PythonSkillRuntimeInfo } from "./skills.js";
import type { IpythonToolOptions } from "./tools/ipython.js";

const CAPABILITY_MANIFEST_BASENAME = "rlm-capabilities.json";
const SRT_CONFIG_BASENAME = "srt-settings.json";
const KERNEL_RUNTIME_BASENAME = "kernel-runtime";
const DEFAULT_WALL_TIME_MS = 20 * 60 * 1000;
const DEFAULT_MAX_PROCESSES = 64;
const require = createRequire(import.meta.url);

export interface RlmCapabilityManifest {
	filesystem: {
		read: string[];
		write: string[];
	};
	network: {
		allow: string[];
		deny_by_default: boolean;
	};
	secrets: {
		allow: string[];
	};
	process?: {
		/** Number of logical CPUs made available to the child. */
		cpu?: number;
		memory_bytes?: number;
		wall_time_ms?: number;
		max_processes?: number;
	};
}

export interface CreateCapabilityKernelOptionsInput {
	manifest: RlmCapabilityManifest;
	cwd: string;
	artifactDir: string;
	baseEnv: Record<string, string>;
	pythonSkills?: readonly PythonSkillRuntimeInfo[];
}

export type CapabilityKernelOptions = Pick<
	IpythonToolOptions,
	"inheritEnv" | "transport" | "runtimeDir" | "env" | "processWrapper" | "resourceLimits" | "pythonSkills"
>;

const FUNCTIONAL_ENV_NAMES: Record<string, true> = {
	HOME: true,
	LANG: true,
	LC_ALL: true,
	LC_CTYPE: true,
	PATH: true,
	PYTHONHOME: true,
	PYTHONPATH: true,
	RLM_DEPTH: true,
	RLM_HARNESS_STATE_DIR: true,
	RLM_MAX_DEPTH: true,
	RLM_SESSION_DIR: true,
	SSL_CERT_DIR: true,
	SSL_CERT_FILE: true,
	TEMP: true,
	TMP: true,
	TMPDIR: true,
	TZ: true,
	VIRTUAL_ENV: true,
};

function assertKnownKeys(value: Record<string, unknown>, allowed: readonly string[], label: string): void {
	const allowedSet = new Set(allowed);
	for (const key of Object.keys(value)) {
		if (!allowedSet.has(key)) throw new Error(`Unknown ${label} capability key: ${key}`);
	}
}

function stringArray(value: unknown, label: string): string[] {
	if (
		!Array.isArray(value) ||
		value.some((entry) => typeof entry !== "string" || entry.trim().length === 0 || entry.includes("\0"))
	) {
		throw new Error(`${label} must be an array of non-empty strings without NUL bytes`);
	}
	return value.map((entry) => (entry as string).trim());
}

function uniqueSorted(values: readonly string[]): string[] {
	return [...new Set(values)].sort();
}
function normalizePaths(value: unknown, cwd: string, label: string): string[] {
	return uniqueSorted(
		stringArray(value, label).map((path) => {
			const absolute = resolve(cwd, path);
			try {
				return realpathSync(absolute);
			} catch {
				return absolute;
			}
		}),
	);
}

function normalizeDomains(value: unknown): string[] {
	const domains = stringArray(value, "network.allow").map((entry) => entry.toLowerCase());
	const domainPattern =
		/^(?:\*\.)?(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
	for (const domain of domains) {
		if (!domainPattern.test(domain)) throw new Error(`Invalid network.allow domain: ${domain}`);
	}
	return uniqueSorted(domains);
}

function positiveInteger(value: unknown, label: string): number {
	if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
		throw new Error(`${label} must be a positive integer`);
	}
	return value;
}

function parseBytes(value: unknown, label: string): number {
	if (typeof value === "number") return positiveInteger(value, label);
	if (typeof value !== "string") throw new Error(`${label} must be a positive integer or size string`);
	const match = /^([1-9]\d*)\s*(b|kb|mb|gb|tb)$/i.exec(value.trim());
	if (!match) throw new Error(`${label} must use b, kb, mb, gb, or tb units`);
	const units: Record<string, number> = { b: 1, kb: 1024, mb: 1024 ** 2, gb: 1024 ** 3, tb: 1024 ** 4 };
	const result = Number(match[1]) * units[match[2]!.toLowerCase()]!;
	return positiveInteger(result, label);
}

function parseDuration(value: unknown, label: string): number {
	if (typeof value === "number") return positiveInteger(value, label);
	if (typeof value !== "string") throw new Error(`${label} must be a positive integer or duration string`);
	const match = /^([1-9]\d*)\s*(ms|s|m|h|d)$/i.exec(value.trim());
	if (!match) throw new Error(`${label} must use ms, s, m, h, or d units`);
	const units: Record<string, number> = { ms: 1, s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000 };
	const result = Number(match[1]) * units[match[2]!.toLowerCase()]!;
	return positiveInteger(result, label);
}

function cloneManifest(manifest: RlmCapabilityManifest): RlmCapabilityManifest {
	return {
		filesystem: { read: [...manifest.filesystem.read], write: [...manifest.filesystem.write] },
		network: { allow: [...manifest.network.allow], deny_by_default: manifest.network.deny_by_default },
		secrets: { allow: [...manifest.secrets.allow] },
		...(manifest.process ? { process: { ...manifest.process } } : {}),
	};
}

function freezeManifest(manifest: RlmCapabilityManifest): RlmCapabilityManifest {
	Object.freeze(manifest.filesystem.read);
	Object.freeze(manifest.filesystem.write);
	Object.freeze(manifest.filesystem);
	Object.freeze(manifest.network.allow);
	Object.freeze(manifest.network);
	Object.freeze(manifest.secrets.allow);
	Object.freeze(manifest.secrets);
	if (manifest.process) Object.freeze(manifest.process);
	return Object.freeze(manifest);
}

function isWithin(path: string, grant: string): boolean {
	const rel = relative(grant, path);
	return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function assertSubsetPaths(child: readonly string[], parent: readonly string[], label: string): void {
	for (const path of child) {
		if (!parent.some((grant) => isWithin(path, grant))) {
			throw new Error(`${label} capability widens parent manifest: ${path}`);
		}
	}
}

function domainCoveredBy(child: string, parent: string): boolean {
	if (child === parent) return true;
	if (!parent.startsWith("*.")) return false;
	const suffix = parent.slice(1).toLowerCase();
	return child.toLowerCase().endsWith(suffix);
}

function assertNoWidening(child: RlmCapabilityManifest, parent: RlmCapabilityManifest): void {
	assertSubsetPaths(child.filesystem.read, parent.filesystem.read, "filesystem.read");
	assertSubsetPaths(child.filesystem.write, parent.filesystem.write, "filesystem.write");
	if (parent.network.deny_by_default && !child.network.deny_by_default) {
		throw new Error("network.deny_by_default capability widens parent manifest");
	}
	if (parent.network.deny_by_default) {
		for (const domain of child.network.allow) {
			if (!parent.network.allow.some((grant) => domainCoveredBy(domain, grant))) {
				throw new Error(`network.allow capability widens parent manifest: ${domain}`);
			}
		}
	}
	for (const name of child.secrets.allow) {
		if (!parent.secrets.allow.includes(name)) {
			throw new Error(`secrets.allow capability widens parent manifest: ${name}`);
		}
	}
	for (const key of ["cpu", "memory_bytes", "wall_time_ms", "max_processes"] as const) {
		const parentLimit = parent.process?.[key];
		const childLimit = child.process?.[key];
		if (parentLimit !== undefined && (childLimit === undefined || childLimit > parentLimit)) {
			throw new Error(`process.${key} capability widens parent manifest`);
		}
	}
}

function readAliased(record: Record<string, unknown>, names: readonly string[]): unknown {
	const present = names.filter((name) => record[name] !== undefined);
	if (present.length > 1) throw new Error(`Specify only one of ${names.join(", ")}`);
	return present.length === 0 ? undefined : record[present[0]!];
}

export function normalizeRlmCapabilityManifest(
	value: unknown,
	cwd: string,
	parent?: RlmCapabilityManifest,
): RlmCapabilityManifest {
	const root = resolve(cwd);
	const baseline = parent
		? cloneManifest(parent)
		: {
				filesystem: { read: [root], write: [root] },
				network: { allow: [], deny_by_default: true },
				secrets: { allow: [] },
				process: { wall_time_ms: DEFAULT_WALL_TIME_MS, max_processes: DEFAULT_MAX_PROCESSES },
			};
	if (value === undefined) return freezeManifest(baseline);
	if (typeof value !== "object" || Array.isArray(value)) throw new Error("capabilities must be an object");
	const capabilities = value as Record<string, unknown>;
	assertKnownKeys(capabilities, ["filesystem", "network", "secrets", "process"], "top-level");

	if (capabilities.filesystem !== undefined) {
		if (
			typeof capabilities.filesystem !== "object" ||
			capabilities.filesystem === null ||
			Array.isArray(capabilities.filesystem)
		) {
			throw new Error("filesystem capabilities must be an object");
		}
		const filesystem = capabilities.filesystem as Record<string, unknown>;
		assertKnownKeys(filesystem, ["read", "write"], "filesystem");
		if (filesystem.read !== undefined) {
			baseline.filesystem.read = normalizePaths(filesystem.read, root, "filesystem.read");
		}
		if (filesystem.write !== undefined) {
			baseline.filesystem.write = normalizePaths(filesystem.write, root, "filesystem.write");
		}
	}

	if (capabilities.network === false) {
		baseline.network = { allow: [], deny_by_default: true };
	} else if (capabilities.network !== undefined) {
		if (
			typeof capabilities.network !== "object" ||
			capabilities.network === null ||
			Array.isArray(capabilities.network)
		) {
			throw new Error("network capabilities must be false or an object");
		}
		const network = capabilities.network as Record<string, unknown>;
		assertKnownKeys(network, ["allow", "deny_by_default", "denyByDefault"], "network");
		if (network.allow !== undefined) baseline.network.allow = normalizeDomains(network.allow);
		const deny = readAliased(network, ["deny_by_default", "denyByDefault"]);
		if (deny !== undefined) {
			if (typeof deny !== "boolean") throw new Error("network.deny_by_default must be a boolean");
			baseline.network.deny_by_default = deny;
		}
	}

	if (capabilities.secrets !== undefined) {
		if (
			typeof capabilities.secrets !== "object" ||
			capabilities.secrets === null ||
			Array.isArray(capabilities.secrets)
		) {
			throw new Error("secrets capabilities must be an object");
		}
		const secrets = capabilities.secrets as Record<string, unknown>;
		assertKnownKeys(secrets, ["allow"], "secrets");
		if (secrets.allow !== undefined) {
			baseline.secrets.allow = uniqueSorted(stringArray(secrets.allow, "secrets.allow"));
			for (const name of baseline.secrets.allow) {
				if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error(`Invalid secret environment name: ${name}`);
			}
		}
	}

	if (capabilities.process !== undefined) {
		if (
			typeof capabilities.process !== "object" ||
			capabilities.process === null ||
			Array.isArray(capabilities.process)
		) {
			throw new Error("process capabilities must be an object");
		}
		const processCapabilities = capabilities.process as Record<string, unknown>;
		assertKnownKeys(
			processCapabilities,
			[
				"cpu",
				"memory",
				"memory_bytes",
				"memoryBytes",
				"wall_time",
				"wall_time_ms",
				"wallTime",
				"wallTimeMs",
				"max_processes",
				"maxProcesses",
			],
			"process",
		);
		baseline.process ??= {};
		if (processCapabilities.cpu !== undefined) {
			baseline.process.cpu = positiveInteger(processCapabilities.cpu, "process.cpu");
		}
		const memory = readAliased(processCapabilities, ["memory", "memory_bytes", "memoryBytes"]);
		if (memory !== undefined) baseline.process.memory_bytes = parseBytes(memory, "process.memory_bytes");
		const wallTime = readAliased(processCapabilities, ["wall_time", "wall_time_ms", "wallTime", "wallTimeMs"]);
		if (wallTime !== undefined) baseline.process.wall_time_ms = parseDuration(wallTime, "process.wall_time_ms");
		const maxProcesses = readAliased(processCapabilities, ["max_processes", "maxProcesses"]);
		if (maxProcesses !== undefined) {
			baseline.process.max_processes = positiveInteger(maxProcesses, "process.max_processes");
		}
	}

	baseline.filesystem.read = uniqueSorted(baseline.filesystem.read);
	baseline.filesystem.write = uniqueSorted(baseline.filesystem.write);
	baseline.network.allow = uniqueSorted(baseline.network.allow);
	baseline.secrets.allow = uniqueSorted(baseline.secrets.allow);
	if (!baseline.network.deny_by_default) {
		throw new Error("network.deny_by_default=false is not supported for sandboxed RLM children");
	}
	if (parent) assertNoWidening(baseline, parent);
	return freezeManifest(baseline);
}

function atomicPrivateJson(path: string, value: unknown): void {
	mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
	const temporary = join(dirname(path), `.${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.tmp`);
	try {
		writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
		renameSync(temporary, path);
		chmodSync(path, 0o600);
	} finally {
		rmSync(temporary, { force: true });
	}
}

export function persistRlmCapabilityManifest(artifactDir: string, manifest: RlmCapabilityManifest): string {
	const path = join(resolve(artifactDir), CAPABILITY_MANIFEST_BASENAME);
	atomicPrivateJson(path, manifest);
	return path;
}

export function loadPersistedRlmCapabilityManifest(
	artifactDir: string,
	cwd: string,
): RlmCapabilityManifest | undefined {
	const path = join(resolve(artifactDir), CAPABILITY_MANIFEST_BASENAME);
	if (!existsSync(path)) return undefined;
	let value: unknown;
	try {
		value = JSON.parse(readFileSync(path, "utf8"));
	} catch (error) {
		throw new Error(`Failed to load persisted RLM capability manifest at ${path}`, { cause: error });
	}
	return normalizeRlmCapabilityManifest(value, cwd);
}

function assertPersistedManifestAttestation(path: string, cwd: string, expected: RlmCapabilityManifest): void {
	const stat = lstatSync(path);
	if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0) {
		throw new Error(`RLM capability manifest failed private-file attestation: ${path}`);
	}
	const persisted = loadPersistedRlmCapabilityManifest(dirname(path), cwd);
	if (!persisted || JSON.stringify(persisted) !== JSON.stringify(expected)) {
		throw new Error(`RLM capability manifest was modified after attestation: ${path}`);
	}
}

function sandboxRuntimeCli(): {
	cliPath: string;
	packageRoot: string;
	command: string;
	argsPrefix: string[];
	seccompPath?: string;
} {
	if (isBunBinary) {
		const packageRoot = getPackageDir();
		const cliPath = join(packageRoot, process.platform === "win32" ? "srt.exe" : "srt");
		if (!existsSync(cliPath)) {
			throw new Error(`Sandboxed RLM children require the bundled Sandbox Runtime executable: ${cliPath}`);
		}
		const seccompArchitecture = process.arch === "x64" ? "x64" : process.arch === "arm64" ? "arm64" : undefined;
		const seccompPath =
			process.platform === "linux" && seccompArchitecture
				? join(packageRoot, "vendor", "seccomp", seccompArchitecture, "apply-seccomp")
				: undefined;
		if (seccompPath && !existsSync(seccompPath)) {
			throw new Error(`Sandboxed RLM children require the bundled seccomp helper: ${seccompPath}`);
		}
		return { cliPath, packageRoot, command: cliPath, argsPrefix: [], seccompPath };
	}
	let packageJson: string;
	try {
		packageJson = require.resolve("@anthropic-ai/sandbox-runtime/package.json");
	} catch (error) {
		throw new Error("Sandboxed RLM children require an installed @anthropic-ai/sandbox-runtime CLI", {
			cause: error,
		});
	}
	const packageRoot = dirname(packageJson);
	const cliPath = join(packageRoot, "dist", "cli.js");
	if (!existsSync(cliPath)) {
		throw new Error(
			`Sandboxed RLM children require the @anthropic-ai/sandbox-runtime CLI, but it is missing: ${cliPath}`,
		);
	}
	return { cliPath, packageRoot, command: process.execPath, argsPrefix: [cliPath] };
}

function existingRealPath(path: string): string | undefined {
	try {
		return realpathSync(path);
	} catch {
		return undefined;
	}
}

function executableReadPaths(command: string, baseEnv: Record<string, string>): string[] {
	let executable = command;
	if (!isAbsolute(executable)) {
		for (const entry of (baseEnv.PATH ?? "").split(delimiter)) {
			const candidate = join(entry, executable);
			if (existsSync(candidate)) {
				executable = candidate;
				break;
			}
		}
	}
	const absolute = resolve(executable);
	const real = existingRealPath(absolute) ?? absolute;
	let directTarget = absolute;
	try {
		const link = readlinkSync(absolute);
		directTarget = resolve(dirname(absolute), link);
	} catch {
		// The executable itself is not a symbolic link.
	}
	return uniqueSorted([dirname(dirname(absolute)), dirname(dirname(directTarget)), dirname(dirname(real))]);
}

function existingUnixSocketPaths(paths: readonly string[]): string[] {
	if (process.platform !== "linux") return [];
	const sockets: string[] = [];
	const pending = [...paths];
	const visited = new Set<string>();
	while (pending.length > 0) {
		const path = pending.pop()!;
		if (visited.has(path)) continue;
		visited.add(path);
		let stat: Stats;
		try {
			stat = lstatSync(path);
		} catch (error) {
			if (error instanceof Error && "code" in error && error.code === "ENOENT") continue;
			throw error;
		}
		if (stat.isSymbolicLink()) continue;
		if (stat.isSocket()) {
			sockets.push(path);
			continue;
		}
		if (!stat.isDirectory()) continue;
		for (const entry of readdirSync(path, { withFileTypes: true })) {
			pending.push(join(path, entry.name));
		}
	}
	return uniqueSorted(sockets);
}

function srtConfig(
	manifest: RlmCapabilityManifest,
	readPaths: readonly string[],
	writePaths: readonly string[],
	manifestPath: string,
	configPath: string,
	runtimeDir: string,
	blockedUnixSockets: readonly string[],
	seccompPath?: string,
): Record<string, unknown> {
	return {
		filesystem: {
			denyRead: ["/", ...blockedUnixSockets],
			allowRead: uniqueSorted([
				...manifest.filesystem.read,
				...manifest.filesystem.write,
				...readPaths,
				...writePaths,
			]),
			allowWrite: uniqueSorted([...manifest.filesystem.write, ...writePaths]),
			denyWrite: [manifestPath, configPath, ...blockedUnixSockets],
			allowGitConfig: false,
		},
		network: {
			allowedDomains: [...manifest.network.allow],
			deniedDomains: [],
			strictAllowlist: true,
			allowUnixSockets: [runtimeDir],
			allowAllUnixSockets: process.platform === "linux",
			allowLocalBinding: true,
		},
		...(seccompPath ? { seccomp: { applyPath: seccompPath } } : {}),
	};
}

export function createCapabilityKernelOptions(input: CreateCapabilityKernelOptionsInput): CapabilityKernelOptions {
	const cwd = resolve(input.cwd);
	const manifest = normalizeRlmCapabilityManifest(input.manifest, cwd);
	const artifactDir = resolve(input.artifactDir);
	const runtimeDir = join(artifactDir, KERNEL_RUNTIME_BASENAME);
	const privateStateDir = join(runtimeDir, "state");
	mkdirSync(runtimeDir, { recursive: true, mode: 0o700 });
	chmodSync(runtimeDir, 0o700);
	mkdirSync(privateStateDir, { recursive: true, mode: 0o700 });
	chmodSync(privateStateDir, 0o700);
	const manifestPath = persistRlmCapabilityManifest(artifactDir, manifest);
	const configPath = join(artifactDir, SRT_CONFIG_BASENAME);
	const { cliPath, packageRoot, command: sandboxCommand, argsPrefix, seccompPath } = sandboxRuntimeCli();
	const skillPaths = (input.pythonSkills ?? []).flatMap((skill) => [
		resolve(cwd, skill.packagePath),
		resolve(cwd, skill.pyprojectPath),
	]);
	const systemPaths = [
		"/bin",
		"/dev",
		"/etc/ld.so.cache",
		"/etc/hosts",
		"/etc/nsswitch.conf",
		"/etc/resolv.conf",
		"/etc/ssl/certs",
		"/lib",
		"/lib64",
		"/proc",
		"/sys",
		"/usr",
		process.execPath,
		existingRealPath(process.execPath) ?? process.execPath,
		packageRoot,
		dirname(dirname(packageRoot)),
		cliPath,
	];
	const trustedRuntimeReadPaths = [input.baseEnv.RLM_HARNESS_STATE_DIR]
		.filter((path): path is string => Boolean(path))
		.map((path) => resolve(path));
	const trustedRuntimeWritePaths = [input.baseEnv.RLM_HARNESS_STATE_DIR]
		.filter((path): path is string => Boolean(path))
		.map((path) => resolve(path));
	const internalWritePaths = uniqueSorted([
		runtimeDir,
		join(artifactDir, "kernel-state.dill"),
		join(artifactDir, "kernel-state.dill.tmp"),
		join(artifactDir, "kernel-state.json"),
		...trustedRuntimeWritePaths,
	]);
	const baseReadPaths = uniqueSorted([
		...systemPaths,
		...skillPaths,
		...trustedRuntimeReadPaths,
		runtimeDir,
		manifestPath,
		configPath,
	]);
	const blockedUnixSockets = (): string[] =>
		existingUnixSocketPaths([...manifest.filesystem.read, ...manifest.filesystem.write]);
	atomicPrivateJson(
		configPath,
		srtConfig(
			manifest,
			baseReadPaths,
			internalWritePaths,
			manifestPath,
			configPath,
			runtimeDir,
			blockedUnixSockets(),
			seccompPath,
		),
	);
	const env: Record<string, string> = {};
	for (const [name, value] of Object.entries(input.baseEnv)) {
		if (FUNCTIONAL_ENV_NAMES[name] || manifest.secrets.allow.includes(name)) env[name] = value;
	}
	env.JUPYTER_RUNTIME_DIR = runtimeDir;
	env.IPYTHONDIR = join(runtimeDir, "ipython");
	env.XDG_STATE_HOME = privateStateDir;
	env.PYTHONNOUSERSITE = "1";

	const processWrapper: KernelProcessWrapper = (
		launch: KernelProcessLaunchDescriptor,
	): KernelProcessLaunchDescriptor => {
		assertPersistedManifestAttestation(manifestPath, cwd, manifest);
		const launchReadPaths = executableReadPaths(launch.command, input.baseEnv);
		atomicPrivateJson(
			configPath,
			srtConfig(
				manifest,
				uniqueSorted([...baseReadPaths, ...launchReadPaths]),
				internalWritePaths,
				manifestPath,
				configPath,
				runtimeDir,
				blockedUnixSockets(),
				seccompPath,
			),
		);
		return {
			command: sandboxCommand,
			args: [...argsPrefix, "--settings", configPath, launch.command, ...launch.args],
		};
	};

	const limits: KernelResourceLimits = {
		...(manifest.process?.cpu !== undefined ? { cpu: manifest.process.cpu } : {}),
		...(manifest.process?.memory_bytes !== undefined ? { memoryBytes: manifest.process.memory_bytes } : {}),
		...(manifest.process?.wall_time_ms !== undefined ? { wallTimeMs: manifest.process.wall_time_ms } : {}),
		...(manifest.process?.max_processes !== undefined ? { maxProcesses: manifest.process.max_processes } : {}),
	};
	return {
		inheritEnv: false,
		transport: "ipc",
		runtimeDir,
		env,
		processWrapper,
		resourceLimits: limits,
		pythonSkills: input.pythonSkills,
	};
}
