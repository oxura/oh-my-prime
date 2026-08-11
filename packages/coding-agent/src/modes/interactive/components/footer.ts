import { type Component, truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import type { ReadonlyFooterDataProvider } from "../../../core/footer-data-provider.js";
import { theme } from "../theme/theme.js";

/**
 * Low-noise workspace footer. The prompt tray carries live model and context
 * state; this row keeps durable environment state visible without competing
 * with the conversation.
 */
export class FooterComponent implements Component {
	private autoCompactEnabled = false;

	constructor(private readonly footerData: ReadonlyFooterDataProvider) {}

	setAutoCompactEnabled(enabled: boolean): void {
		this.autoCompactEnabled = enabled;
	}

	invalidate(): void {
		// Render output is read directly from the provider.
	}

	dispose(): void {
		// Watcher ownership remains with FooterDataProvider.
	}

	render(width: number): string[] {
		const safeWidth = Math.max(1, width);
		const branch = this.footerData.getGitBranch();
		const providerCount = this.footerData.getAvailableProviderCount();
		const extensionStatuses = [...this.footerData.getExtensionStatuses().values()].filter((status) => status.trim());
		const leftParts = [
			branch ? `${theme.fg("dim", "BRANCH")} ${theme.fg("muted", branch)}` : undefined,
			this.autoCompactEnabled ? `${theme.fg("dim", "COMPACT")} ${theme.fg("success", "AUTO")}` : undefined,
			...extensionStatuses.map((status) => theme.fg("muted", status)),
		].filter((part): part is string => part !== undefined);
		const left = leftParts.join(theme.fg("borderMuted", "  ·  "));
		const right =
			providerCount > 0 ? `${theme.fg("dim", "PROVIDERS")} ${theme.fg("muted", providerCount.toString())}` : "";
		if (!left && !right) return [];

		const gap = left && right ? 2 : 0;
		const rightWidth = Math.min(visibleWidth(right), Math.max(0, safeWidth - gap));
		const leftWidth = Math.max(0, safeWidth - rightWidth - gap);
		const renderedLeft = truncateToWidth(left, leftWidth, "…", true);
		const renderedRight = truncateToWidth(right, rightWidth, "…", true);
		const padding = " ".repeat(Math.max(0, safeWidth - visibleWidth(renderedLeft) - visibleWidth(renderedRight)));
		return [theme.fg("dim", `${renderedLeft}${padding}${renderedRight}`)];
	}
}
