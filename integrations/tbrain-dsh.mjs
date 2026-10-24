/**
 * Tbrain native plugin for DeepSeek Harness (cordis plugin, engine 0.2.x).
 *
 * The official Claude-hooks bridge (@deepseek-ai/dsh-hooks-claude-code) already
 * covers SessionStart / UserPromptSubmit / Stop / PostToolUse. This small plugin
 * adds what the bridge cannot see:
 *
 *   - PreCompact reminders      -> session/event "compaction/start"
 *   - SessionEnd status          -> agent/disposed && session/disposed
 *   - context usage / switch hint-> agent/pre-step + ctx.tokenMeter
 *
 * Configuration comes from the profile patch entry `tbrain-native`:
 *   config: { command: <fixed entry>, home: <app home> }
 *
 * Every call is fire-and-forget with a hard timeout: a missing Tbrain program must
 * never break the DSH session.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";

export const name = "tbrain-native";
export const inject = ["agents", "tokenMeter"];

const HOOK_TIMEOUT_MS = 4000;
const CONTEXT_RATIO = 0.75;
const CONTEXT_COOLDOWN_MS = 120000;

function defaultEntry() {
	const local = (process.env.LOCALAPPDATA || "").trim();
	const root = (process.env.TBRAIN_INSTALL_ROOT || "").trim() ||
		(local ? join(local, "Programs", "Tbrain") : join(homedir(), "AppData", "Local", "Programs", "Tbrain"));
	return join(root, "bin", "tbrain.cmd");
}

function resolveSettings(config) {
	return {
		command: String(config?.command || process.env.TBRAIN_ENTRY || defaultEntry()),
		home: String(config?.home || process.env.TBRAIN_HOME || join(homedir(), ".tbrain")),
	};
}

/** Run `tbrain hook --host dsh` once; resolves to the host-neutral dict (or {}). */
function callHook(settings, payload) {
	return new Promise((resolve) => {
		let child;
		const args = ["hook", "--host", "dsh", "--home", settings.home];
		try {
			if (process.platform === "win32") {
				// A .cmd file must go through cmd.exe when spawned from Node.
				child = spawn("cmd.exe", ["/d", "/c", settings.command, ...args], {
					windowsHide: true,
					stdio: ["pipe", "pipe", "ignore"],
				});
			} else {
				child = spawn(settings.command, args, { stdio: ["pipe", "pipe", "ignore"] });
			}
		} catch {
			resolve({});
			return;
		}
		let output = "";
		let finished = false;
		const finish = (value) => {
			if (finished) return;
			finished = true;
			clearTimeout(timer);
			try {
				child.kill();
			} catch {
				/* already gone */
			}
			resolve(value || {});
		};
		const timer = setTimeout(() => finish({}), HOOK_TIMEOUT_MS);
		child.stdout.on("data", (chunk) => {
			output += String(chunk);
			if (output.length > 1024 * 1024) finish({});
		});
		child.on("error", () => finish({}));
		child.on("close", () => {
			if (finished) return;
			finished = true;
			clearTimeout(timer);
			try {
				resolve(JSON.parse(output.trim() || "{}") || {});
			} catch {
				resolve({});
			}
		});
		try {
			child.stdin.on("error", () => {});
			child.stdin.end(JSON.stringify(payload || {}));
		} catch {
			finish({});
		}
	});
}

function message(text) {
	return Object.freeze({
		id: randomUUID(),
		role: "user",
		source: { kind: "tbrain-native" },
		content: [{ type: "text", text }],
	});
}

function injectContext(agent, text) {
	if (!agent || !String(text || "").trim()) return;
	try {
		agent.inject(message(String(text)));
	} catch {
		/* agent may be disposed */
	}
}

/** Convert a DSH source value to the neutral session-start source enum. */
function sourceOf(value) {
	const text = String(value || "").toLowerCase();
	return ["startup", "resume", "clear", "compact", "fork"].includes(text) ? text : "startup";
}

export function apply(ctx, config = {}) {
	const settings = resolveSettings(config);
	const endedSessions = new Set();
	const lastHint = new Map();
	const rootAgents = () => {
		try {
			return (ctx.agents && ctx.agents.roots && ctx.agents.roots()) || [];
		} catch {
			return [];
		}
	};
	const agentForSession = (session) => {
		const id = session?.header?.id;
		if (!id) return null;
		return rootAgents().find((agent) => agent?.session?.header?.id === id) || null;
	};

	// SessionEnd: the bridge has no Claude-shaped SessionEnd hook.
	const fireSessionEnd = (session) => {
		const id = session?.header?.id || "";
		if (!id || endedSessions.has(id)) return;
		endedSessions.add(id);
		callHook(settings, {
			hook_event_name: "SessionEnd",
			session_id: id,
			cwd: String(session?.header?.cwd || ""),
			reason: "disposed",
		}).catch(() => {});
	};
	ctx.on("agent/disposed", (payload) => fireSessionEnd(payload?.agent?.session || payload?.session));
	ctx.on("session/disposed", (session) => fireSessionEnd(session));

	// Session events: PreCompact (compaction/start) and user messages.
	ctx.on("session/event", (session, event) => {
		const type = event?.type;
		if (type === "compaction/start") {
			const agent = agentForSession(session);
			callHook(settings, {
				hook_event_name: "PreCompact",
				session_id: String(session?.header?.id || ""),
				cwd: String(session?.header?.cwd || ""),
				source: "compact",
			}).then((response) => injectContext(agent, response?.additional_context)).catch(() => {});
		}
	});

	// Context usage / switch hint: DSH exposes ctx.tokenMeter and context pressure.
	ctx.on("agent/pre-step", ({ agent }) => {
		if (!agent || !rootAgents().includes(agent)) return;
		let tokens = null;
		let window = null;
		try {
			const measured = ctx.tokenMeter.measure(agent.session);
			tokens = Number(measured?.totalTokens ?? measured?.surfaceTokens ?? 0) || 0;
		} catch {
			/* tokenMeter unavailable */
		}
		try {
			window = ctx.llm?.resolveModelInfo?.()?.context ?? null;
		} catch {
			window = null;
		}
		if (!window || !Number.isFinite(window) || window <= 0) return;
		const ratio = tokens / window;
		if (ratio < CONTEXT_RATIO) return;
		const sessionId = String(agent?.session?.header?.id || "");
		const key = sessionId || "unknown";
		const last = lastHint.get(key) || 0;
		if (Date.now() - last < CONTEXT_COOLDOWN_MS) return;
		lastHint.set(key, Date.now());
		callHook(settings, {
			hook_event_name: "Context",
			session_id: sessionId,
			cwd: String(agent?.session?.header?.cwd || ""),
			context_tokens: Math.round(tokens),
			context_window: Math.round(window),
		}).then((response) => injectContext(agent, response?.additional_context)).catch(() => {});
	});
}
