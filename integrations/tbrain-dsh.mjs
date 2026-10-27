/** Tbrain DSH lifecycle adapter. Uses only Node builtins and injected host services. */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";

export const name = "tbrain-native";
export const inject = ["agents", "tokenMeter", "sessionProjections"];

const HOOK_TIMEOUT_MS = 10000;
const CONTEXT_RATIO = 0.65;
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
				const command = [settings.command, ...args].map(value => `"${value}"`).join(" ");
				child = spawn("cmd.exe", ["/d", "/s", "/c", `"${command}"`], {
					windowsVerbatimArguments: true,
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
	const trackedRoots = new Set();
	const reminded = new Set();
	const lastHint = new Map();
	const isRoot = agent => ctx.agents.roots().includes(agent);
	const payload = (agent, event) => ({
		hook_event_name: event,
		session_id: agent.session.header.id,
		cwd: agent.session.header.cwd,
	});

	ctx.on("tools/post-execute", async (exec, result, next) => {
		if (exec.agent && isRoot(exec.agent) && ["bash", "pwsh"].includes(exec.name) &&
			/\b(pytest|unittest|test|check|build)\b/i.test(exec.arguments?.command || "")) {
			const output = (result.content || []).filter(block => block.type === "text").map(block => block.text).join("");
			const exit = /\n\[exit code: (\d+)\]\s*$/.exec(output);
			if (exit && !/\[(?:killed|stopped|shell exited|shell killed|running|background)/.test(output)) {
				await callHook(settings, { ...payload(exec.agent, "ToolResult"),
					last_test: { command: exec.arguments.command, exit_code: Number(exit[1]) } });
			}
		}
		return next();
	});

	ctx.on("agent/created", async ({ agent, source }) => {
		if (!isRoot(agent)) return;
		trackedRoots.add(agent.session.header.id);
		const result = await callHook(settings, { ...payload(agent, "SessionStart"), source: sourceOf(source) });
		injectContext(agent, result.additional_context);
	});

	ctx.on("agent/pre-step", async ({ agent, messages }, next) => {
		if (!isRoot(agent)) return next();
		trackedRoots.add(agent.session.header.id);
		const contexts = [];
		for (const incoming of messages) {
			if (incoming.role !== "user" || incoming.source?.kind === "tbrain-native") continue;
			const prompt = incoming.content.filter(block => block.type === "text").map(block => block.text).join("");
			if (!prompt) continue;
			reminded.delete(agent.session.header.id);
			const result = await callHook(settings, { ...payload(agent, "UserPromptSubmit"), prompt });
			if (result.additional_context) contexts.push(message(result.additional_context));
		}
		const pressure = ctx.sessionProjections.snapshot(agent.session).values.contextPressure;
		const tokens = ctx.tokenMeter.measure(agent.session).totalTokens;
		const window = pressure?.contextWindow;
		const id = agent.session.header.id;
		if (window && tokens / window >= CONTEXT_RATIO && Date.now() - (lastHint.get(id) || 0) >= CONTEXT_COOLDOWN_MS) {
			const result = await callHook(settings, { ...payload(agent, "Context"), context_tokens: tokens, context_window: window });
			if (result.additional_context) {
				contexts.push(message(result.additional_context));
				lastHint.set(id, Date.now());
			}
		}
		const downstream = await next();
		return downstream.kind === "enter" ? { ...downstream, messages: [...downstream.messages, ...contexts] } : downstream;
	});

	ctx.on("agent/turn-stopping", async ({ agent }) => {
		if (!isRoot(agent)) return;
		const id = agent.session.header.id;
		const result = await callHook(settings, { ...payload(agent, "Stop"), stop_hook_active: reminded.has(id) });
		if (result.decision === "block") {
			reminded.add(id);
			agent.steer(message(result.reason));
		}
	});

	ctx.on("session/event", async (session, event) => {
		if (event.type !== "compaction/start") return;
		const agent = ctx.agents.roots().find(root => root.session === session);
		if (!agent) return;
		const result = await callHook(settings, payload(agent, "PreCompact"));
		injectContext(agent, result.additional_context);
	});

	const end = async session => {
		const id = session?.header?.id;
		if (!trackedRoots.delete(id)) return;
		reminded.delete(id);
		lastHint.delete(id);
		await callHook(settings, { hook_event_name: "SessionEnd", session_id: id, cwd: session.header.cwd });
	};
	ctx.on("agent/disposed", ({ agent }) => end(agent.session));
}
