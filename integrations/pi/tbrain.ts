/**
 * Tbrain pi extension.
 *
 * pi has no built-in MCP; this extension connects pi to the Tbrain program through
 * its stable command line entry (`<install root>/bin/tbrain.cmd`):
 *
 *   - lifecycle events call `tbrain hook --host pi`   (session start / prompt / stop / compact / end)
 *   - tools call `tbrain tool <name>` (bootstrap / search / read / save / save_decision / resume)
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
const HOOK_TIMEOUT_MS = 15000;
const TOOL_TIMEOUT_MS = 60000;

const PROJECT_PROMPT_SPEC = `项目提示词规范（project_prompt 操作）：
- 最终目标最多3行，每行80字；硬规则最多12条，每条80字；当前阶段最多5行，每行80字；关键路径和命令最多10条，每条100字；全文4000字以内。
- 硬规则每条带来源：（来源：[[会话名]]#U3 或 U3 / E4）；无来源不写入。目标和硬规则写长期约定；当前阶段写“阶段名：完成标准”，随阶段更新。
- 关键路径和命令写“用途：路径或命令”；命令须已验证。只写确认的事实，不写尝试经过、临时流水、相对时间词或评价；主语写全称。
- 只提交变化的节，整节替换；旧内容自动进入变更日志。用户补充区和用户手改内容不覆盖。
- 更新已有章节（空标题也算）必须带读取时的base_sha，例如：{"op":"project_prompt","sections":{"当前阶段":"阶段名：完成标准"},"base_sha":{"当前阶段":"ab12cd34"}}。启动包标题提供sha，取自未过滤原节；继承规则不可用作本项目写入基准。真正缺失的新章节可省略。
- 缺基准或发生冲突时核对rejected中的head.text，再用head.sha作为该节base_sha重交；只重交失败节，不重复written。
- 公共池env取代已有卡也必须带base_sha：先search读取公共命中的sha，再提交{"op":"env","items":[{"type":"工具","name":"工具名","base_sha":"ab12cd34","body":"已确认的新内容"}]}；新卡可省略base_sha。公共卡冲突同样先核对head.text，再用head.sha作为该卡base_sha，只重交失败卡。
执行前核对项目提示词与相关文件；项目目标、阶段、关键路径或命令变化及旧说明失效时，用 save 的 project_prompt 更新对应节，无变化不改写。用户长期决定用 save_decision，勿重复抄入硬规则。空白节只补已确认内容，不推断用户偏好；保存后核对 written/rejected，只补交失败部分。`;

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
		if (event?.source === "extension") return;
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
		const saveReminder = changedOrTested || roundsSinceSave >= 3
			? response?.additional_context || response?.reason || (changedOrTested ? "本轮有文件修改或测试，请调用 tbrain_save 保存工作进展。" : "") : "";
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
			project: Type.Optional(Type.String({ description: "整段会话的项目归属；新会话留空按工作目录匹配。单项归档请在保存时指定项目" })),
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
		description: "主动检索本项目及允许读取的子项目的历史、长期决定与附件卡，再查公共池。项目开启“全局共享（读取非项目会话）”后，才额外检索非项目会话；非项目调用查公共池、全局附件与非项目池。返回带来源路径的少量结果，不改变自动接续推荐。公共池命中附sha，更新已有卡时作为env的base_sha。摘要不完整时用 tbrain_read 和 entry_ids 查完整条目与证据，未读原文不要断言没有记录。",
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
		name: "tbrain_read",
		label: "Tbrain Read",
		description: "只读历史，不接续任务。target 用搜索结果 read.target、会话 ID 或可见文档/已登记附件路径；entry_id 可选 U#/D#/K#/E#/G#/X#（含归档）、DL-xxxx。E# 加 source=true 读取可见项目目录内或已登记附件的 UTF-8 正文，其余文件返回引用位置。G# 返回 done。start 为字符偏移，limit 默认6000、最多20000；truncated=true 时按 next_start 续读。仅包含已保存记忆，不代表完整聊天记录。",
		parameters: Type.Object({
			target: Type.String(),
			entry_id: Type.Optional(Type.String()),
			start: Type.Optional(Type.Integer({ minimum: 0 })),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20000 })),
			source: Type.Optional(Type.Boolean()),
		}),
		async execute(_toolCallId: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			const result = await callTool("read", { ...params, session_id: `${HOST}:${sessionIdOf(ctx)}` });
			return { content: [{ type: "text", text: toolText(result) }], details: result };
		},
	});

	pi.registerTool({
		name: "tbrain_save_decision",
		label: "Tbrain Save Decision",
		description:
			"保存从用户原话提炼的项目方向和业务约定。自然表达清楚就记录，不要求正式确认；关键歧义先问。上下文只用于消解指代，不把模型建议、实施选择或推测补成用户决定。保留条件、否定、时间和适用范围，未定内容仍写未定。" +
			"程序自动附上真实来源原话，来源存在不代表程序验证了提炼含义。" +
			"source 为当前会话用户原话编号数组。why 仅填写用户说明的理由，未说明留空；text 与 why 合计最多200字。" +
			"supersedes 仅填写被用户明确取代的目标项目 DL-xxxx 编号；含糊冲突先问。临时要求用 tbrain_save，同一长期决定不重复写 K# 或项目硬规则。",
		parameters: Type.Object({
			text: Type.String({ description: "准确提炼的用户长期决定" }),
			project: Type.Optional(Type.String({ description: "事项所属的已有项目名，默认当前项目" })),
			source: Type.Array(Type.String(), { description: '当前会话用户原话编号，如 ["U2", "U4"]' }),
			why: Type.Optional(Type.String({ description: "用户说明的理由，未说明留空" })),
			supersedes: Type.Optional(Type.Array(Type.String(), { description: "被明确取代的目标项目 DL-xxxx 编号" })),
		}),
		async execute(_toolCallId: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			const result = await callTool("save_decision", {
				session_id: `${HOST}:${sessionIdOf(ctx)}`,
				text: params.text,
				project: params.project,
				source: params.source,
				why: params.why || "",
				supersedes: params.supersedes || [],
			});
			return { content: [{ type: "text", text: toolText(result) }], details: result };
		},
	});

	pi.registerTool({
		name: "tbrain_save",
		label: "Tbrain Save",
		description:
			"保存本轮工作进展、证据、问题和下一步；临时要求及本次技术决定也在这里记录。用户长期决定用 tbrain_save_decision，不在这里重复保存。" +
			'保存时间由程序写入，事实的发生时间、持续时间、数量和对象关系按原话保留。K#仅为任务选择，不是用户长期业务约定。历史K#被用户明确取代时，用 {"op":"set_status","status":"进行中","supersede":["K1","[[旧会话名]]#K2"]} 退役；包括同项目旧会话和归档条目，原文保留。含糊冲突先问，不自动作废。' +
			"operations 为 JSON 数组，元素形如 " +
			'{"op":"append","section":"done","items":[{"text":"完成内容 [证据:E1]"}]}、' +
			'{"op":"replace","section":"next_step","text":"动作 + 验证"} 或 ' +
			'{"op":"project_prompt","sections":{"当前阶段":"阶段名：完成标准"},"base_sha":{"当前阶段":"ab12cd34"}}。' +
			"操作顺序执行，先存证据再引用返回的 E#；正文连引用最多200字。ok=true表示全部通过，" +
			"ok=false时written仍是已保存部分，只补交rejected，不重复整批。\n\n" + PROJECT_PROMPT_SPEC,
		parameters: Type.Object({
			operations: Type.Array(Type.Any(), { description: "save 操作数组" }),
			project: Type.Optional(Type.String({ description: "本批进展所属的已有项目名，默认当前项目。不同项目分次保存" })),
			last_test: Type.Optional(Type.Any({ description: "仅无宿主采集结果时补报：{command, exit_code}" })),
		}),
		async execute(_toolCallId: string, params: any, _signal: any, _onUpdate: any, ctx: any) {
			const result = await callTool("save", {
				session_id: `${HOST}:${sessionIdOf(ctx)}`,
				operations: Array.isArray(params?.operations) ? params.operations : [],
				project: params.project,
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
		description: "继续旧任务，读取交接包并在当前会话写入接续关系。查询历史事实请用 tbrain_read；交接包不是完整历史。",
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
