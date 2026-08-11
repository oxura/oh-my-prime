import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { KernelManager } from "../src/core/kernel/index.js";

let tempDir = "";

function writeExecutable(filePath: string, content: string): void {
	writeFileSync(filePath, content);
	chmodSync(filePath, 0o755);
}

describe("KernelManager startup", () => {
	beforeEach(() => {
		tempDir = mkdtempSync(join(tmpdir(), "prime-agent-kernel-startup-"));
	});

	afterEach(() => {
		if (tempDir) {
			rmSync(tempDir, { recursive: true, force: true });
			tempDir = "";
		}
	});

	it("surfaces kernels that exit before resolving ports", async () => {
		const python = join(tempDir, "python");
		writeExecutable(python, ["#!/bin/sh", 'echo "fake kernel died before binding" >&2', "exit 42", ""].join("\n"));
		const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
		const manager = new KernelManager({ python, cwd: tempDir });

		try {
			await expect(manager.execute("print(1)")).rejects.toThrow(
				/Kernel exited before resolving ports[\s\S]*fake kernel died before binding/,
			);
		} finally {
			errorSpy.mockRestore();
			await manager.dispose();
		}
	});

	it("uses the configured process wrapper for direct spawn instead of falling back unsandboxed", async () => {
		const wrapper = join(tempDir, "sandbox-wrapper");
		writeExecutable(wrapper, ["#!/bin/sh", 'echo "sandbox wrapper invoked" >&2', "exit 73", ""].join("\n"));
		let wrappedLaunch = false;
		const manager = new KernelManager({
			python: "/nonexistent/unsandboxed-python",
			cwd: tempDir,
			processWrapper: (launch) => {
				wrappedLaunch = true;
				expect(launch.command).toBe("/nonexistent/unsandboxed-python");
				expect(launch.args).toContain("-m");
				expect(launch.args).toContain("ipykernel_launcher");
				return { command: wrapper, args: [] };
			},
		});
		const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

		try {
			await expect(manager.execute("print(1)")).rejects.toThrow(
				/Kernel exited before resolving ports[\s\S]*sandbox wrapper invoked/,
			);
			expect(wrappedLaunch).toBe(true);
		} finally {
			errorSpy.mockRestore();
			await manager.dispose();
		}
	});
});
