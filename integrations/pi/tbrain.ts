/**
 * Tbrain pi extension.
 *
 * pi has no built-in MCP; this extension connects pi to the Tbrain program through
 * its stable command line entry (`<install root>/bin/tbrain.cmd`):
 *
 *   - lifecycle events call `tbrain hook --host pi`   (session start / prompt / stop / compact / end)
 *   - the four tools call `tbrain tool <name>`         (bootstrap / search / save / resume)
 *
 * JSON crosses the process boundary on stdin; the entry therefore never needs
 * shell-quoting for user text. Installed by `tbrain connect pi` into
 * `~/.pi/agent/extensions/tbrain.ts`; a new pi session or `/reload` picks it up.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
import { Type } from "typebox";
import * as os from "node:os";
import * as path from "node:path";

const HOST = "pi";
const HOOK_TIMEOUT_MS = 8000;
const TOOL_TIMEOUT_MS = 60000;

function installRoot(): string {
	const configured = (process.env.TBRAIN_INSTALL_ROOT || "").trim();
	if (configured) return configured;
	const local = (process.env.LOCALAPPDATA || "").trim() ||
		path.join(os.homedir(), "AppData", "Local");
	return path.join(local, "Programs", "Tbrain");
}

function entryPath(): string {
	const override = (process.env.TBRAIN_ENTRY || "").trim();
	if (override) return override;
	return path.join(installRoot(), "bin", "tbrain.cmd");
}

/** Run the fixed entry with JSON on stdin and return its stdout. */
function callTbrain(args: string[], input: unknown, timeout: number): Promise<string> {
	const entry = entryPath();
	const serialized = JSON.stringify(input ?? {});
	const options = {
		encoding: "utf8" as const,
		timeout,
		windowsHide: true,
		maxBuffer: 16 * 1024 * 1024,
	};
	return new Promise((resolve, reject) => {
		// A .cmd entry needs cmd.exe; keep JSON on stdin and the host event loop free.
		const windows = process.platform === "win32";
		const child = execFile(windows ? "cmd.exe" : entry,
			windows ? ["/d", "/c", entry, ...args] : args, options,
			(error, stdout) => error ? reject(error) : resolve(stdout));
		child.stdin!.on("error", reject);
		child.stdin!.end(serialized);
	});
}

function parseJson(text: string): any {
	const trimmed = (text || "").trim();
	if (!trimmed) return {};
	try {
		return JSON.parse(trimmed);
	} catch {
		return { text: trimmed };
	}
}

function sessionIdOf(ctx: any): string {
	try {
		return String(ctx?.sessionManager?.getSessionId?.() ?? "");
	} catch {
		return "";
	}
}

function cwdOf(ctx: any): string {
	try {
		return String(ctx?.cwd || process.cwd());
	} catch {
		return process.cwd();
	}
}

function usageOf(ctx: any): { tokens: number | null; window: number | null } {
	try {
		const usage = ctx?.getContextUsage?.();
		if (!usage) return { tokens: null, window: null };
		const tokens = Number(usage.tokens);
		const window = Number(usage.contextWindow);
		return {
			tokens: Number.isFinite(tokens) ? Math.round(tokens) : null,
			window: Number.isFinite(window) ? Math.round(window) : null,
		};
	} catch {
		return { tokens: null, window: null };
	}
}

const PI_SOURCE: Record<string, string> = {
	startup: "startup",
	new: "startup",
	reload: "startup",
	resume: "resume",
	fork: "fork",
	compact: "compact",
	clear: "clear",
};

async function hookEvent(eventName: string, payload: Record<string, unknown>): Promise<Record<string, any>> {
	try {
		const raw = await callTbrain(["hook", "--host", HOST], { hook_event_name: eventName, ...payload }, HOOK_TIMEOUT_MS);
		return parseJson(raw);
	} catch {
		// Hook I/O is a host boundary: never break the pi turn because Tbrain is unavailable.
		return {};
	}
}

function toolText(result: any): string {
	if (result && typeof result === "object") {
		if (typeof result.text === "string" && result.text.trim()) return result.text;
		if (typeof result.markdown === "string" && result.markdown.trim()) return result.markdown;
	}
	return JSON.stringify(result ?? {}, null, 2);
}

/** Call one Tbrain tool; failures become a model-readable error instead of a throw. */
async function callTool(name: string, payload: Record<string, unknown>): Promise<any> {
	try {
		return parseJson(await callTbrain(["tool", name], payload, TOOL_TIMEOUT_MS));
	} catch (error) {
		return { ok: false, error: `Tbrain 不可用（${name}）：${String(error)}` };
	}
}

export default function tbrainExtension(pi: ExtensionAPI): void {
	let pendingContext = "";
	let roundsSinceSave = 0;
	let changedOrTested = false;
	let compactNeedsContinuation = false;

	const remember = (response: any) => {
		const context = String(response?.additional_context || "");
		if (context.trim()) pendingContext = context;
	};

	const eventPayload = (event: any, ctx: any) => {
		const usage = usageOf(ctx);
		return {
			session_id: sessionIdOf(ctx),
			cwd: cwdOf(ctx),
			host_record: ctx.sessionManager.getSessionFile() || "",
			context_tokens: usage.tokens,
			context_window: usage.window,
			...event,
		};
	};

	// --- lifecycle hooks -------------------------------------------------
	pi.on("session_start", async (event: any, ctx: any) => {
		const source = PI_SOURCE[String(event?.reason || "startup")] || "startup";
		remember(await hookEvent("SessionStart", eventPayload({ source }, ctx)));
	});

	pi.on("input", async (event: any, ctx: any) => {
		const text = typeof event?.text === "string" ? event.text : "";
		if (text.trim()) {
			roundsSinceSave++;
			remember(await hookEvent("UserPromptSubmit", eventPayload({ prompt: text }, ctx)));
		}
	});

	pi.on("before_agent_start", async () => {
		compactNeedsContinuation = false;
		if (!pendingContext.trim()) return undefined;
		const message = {
			customType: "tbrain",
			content: pendingContext,
			display: true,
		};
		pendingContext = "";
		return { message };
	});

	pi.on("agent_end", async (_event: any, ctx: any) => {
		const response = await hookEvent("Stop", eventPayload({ stop_hook_active: false }, ctx));
		const hint = await hookEvent("Context", eventPayload({}, ctx));
		const saveReminder = changedOrTested || roundsSinceSave >= 3 ? response?.additional_context || response?.reason : "";
		const reminder = [saveReminder, hint?.additional_context].filter(Boolean).join("\n");
		if (!reminder.trim()) return;
		try {
			// pi has no Stop-block equivalent: degrade to a next-turn reminder (design D5).
			pi.sendMessage(
				{ customType: "tbrain", content: reminder, display: true },
				{ deliverAs: "nextTurn" },
			);
		} catch {
			// Older pi builds may not support queued custom messages; a missed reminder is fine.
		}
	});

	pi.on("session_before_compact", async (_event: any, ctx: any) => {
		remember(await hookEvent("PreCompact", eventPayload({}, ctx)));
	});
	pi.on("session_compact", async (event: any, ctx: any) => {
		const beforeCompact = pendingContext;
		remember(await hookEvent("SessionStart", eventPayload({ source: "compact" }, ctx)));
		if (beforeCompact) pendingContext = [pendingContext, beforeCompact].filter(Boolean).join("\n\n");
		if (!beforeCompact || event?.willRetry) return;
		if (event?.reason === "manual") {
			setTimeout(() => pi.sendMessage(
				{ customType: "tbrain", content: "压缩已完成，请根据提醒保存进展。", display: true },
				{ triggerTurn: true },
			), 0);
		} else {
			compactNeedsContinuation = true;
		}
	});
	pi.on("agent_before_settle", async (event: any) => {
		if (!compactNeedsContinuation || event.outcome !== "completed") return;
		compactNeedsContinuation = false;
		return { entries: [{ type: "custom_message", customType: "tbrain", content: "压缩已完成，请根据提醒保存进展。", display: true }], continue: true };
	});
	pi.on("tool_result", async (event: any, ctx: any) => {
		const command = event.input?.command || "";
		const ranTest = ["bash", "powershell"].includes(event.toolName) && /\b(pytest|unittest|test|check|build)\b/i.test(command);
		if ((["edit", "write"].includes(event.toolName) && !event.isError) || ranTest) changedOrTested = true;
		if (ranTest &&
			typeof event.details?.exitCode === "number") {
			await hookEvent("ToolResult", eventPayload({ last_test: {
				command, exit_code: event.details.exitCode,
			} }, ctx));
		}
	});

	pi.on("session_shutdown", async (event: any, ctx: any) => {
		await hookEvent("SessionEnd", eventPayload({ reason: String(event?.reason || "quit") }, ctx));
	});

	// --- tools -----------------------------------------------------------
	pi.registerTool({
		name: "tbrain_bootstrap",
		label: "Tbrain Bootstrap",
		description:
			"加载 Tbrain 启动包。有 Hook 时已自动完成；仅无 Hook 或需要刷新项目提示词时调用。",
		parameters: Type.Object({
			cwd: Type.Optional(Type.String({ description: "工作目录，默认当前会话目录" })),
			host_record: Type.Optional(Type.String({ description: "宿主会话记录地址（可为空）" })),
			project: Type.Optional(Type.String({ description: "项目名；留空按工作目录匹配" })),
		}),
		async execute(_toolCallId: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			const result = await callTool("bootstrap", {
				session_id: `${HOST}:${sessionIdOf(ctx)}`,
				agent: HOST,
				cwd: String(params?.cwd || cwdOf(ctx)),
				host_record: String(params?.host_record || ""),
				project: String(params?.project || ""),
			});
			return { content: [{ type: "text", text: toolText(result) }], details: result };
		},
	});

	pi.registerTool({
		name: "tbrain_search",
		label: "Tbrain Search",
		description: "检索 Tbrain 会话总结与公共池卡片；返回带来源路径的少量结果。",
		parameters: Type.Object({
			query: Type.String({ description: "检索词" }),
			limit: Type.Optional(Type.Number({ description: "最多返回条数，默认 5" })),
		}),
		async execute(_toolCallId: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			const result = await callTool("search", {
				query: String(params?.query || ""),
				session_id: `${HOST}:${sessionIdOf(ctx)}`,
				limit: Number(params?.limit || 5),
			});
			return { content: [{ type: "text", text: toolText(result) }], details: result };
		},
	});

	pi.registerTool({
		name: "tbrain_save",
		label: "Tbrain Save",
		description:
			"保存会话总结与进度。operations 为 JSON 数组，元素形如 " +
			'{"op":"append","section":"decision","items":[{"text":"决定 | 理由"}]}、' +
			'{"op":"replace","section":"next_step","text":"动作 + 验证"} 或 ' +
			'{"op":"project_prompt","sections":{"当前阶段":"阶段名：完成标准"}}。' +
			"按启动包规范维护项目提示词：目标或长期规则、阶段、关键路径命令变化及旧内容失效时更新，无变化不改。" +
			"操作顺序执行，先存证据再引用返回的 E#；正文连引用最多200字。ok=true表示全部通过，" +
			"ok=false时written仍是已保存部分，只补交rejected，不重复整批。",
		parameters: Type.Object({
			operations: Type.Array(Type.Any(), { description: "save 操作数组" }),
			last_test: Type.Optional(Type.Any({ description: "仅无宿主采集结果时补报：{command, exit_code}" })),
		}),
		async execute(_toolCallId: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			const result = await callTool("save", {
				session_id: `${HOST}:${sessionIdOf(ctx)}`,
				operations: Array.isArray(params?.operations) ? params.operations : [],
				last_test: params?.last_test ?? null,
			});
			if (result.ok) {
				roundsSinceSave = 0;
				changedOrTested = false;
			}
			return { content: [{ type: "text", text: toolText(result) }], details: result };
		},
	});

	pi.registerTool({
		name: "tbrain_resume",
		label: "Tbrain Resume",
		description: "读取某个会话的接续视图（下一步、未完成目标、最近进展）。",
		parameters: Type.Object({
			target: Type.String({ description: "会话名或会话 ID" }),
			fork_user_count: Type.Optional(Type.Number({ description: "历史分支的用户轮次；省略为普通接续" })),
			fork_revision: Type.Optional(Type.String({ description: "精确分叉保存版本，取自 save/bootstrap 返回值" })),
		}),
		async execute(_toolCallId: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			const result = await callTool("resume", {
				session_id: `${HOST}:${sessionIdOf(ctx)}`,
				target: String(params?.target || ""),
				fork_user_count: params?.fork_user_count,
				fork_revision: params?.fork_revision || "",
			});
			return { content: [{ type: "text", text: toolText(result) }], details: result };
		},
	});
}
