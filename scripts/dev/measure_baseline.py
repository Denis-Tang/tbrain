"""Measure frozen memory outcomes; only aggregate results leave the TEMP vault copy."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time


OWNED = ("会话总结", "项目", "公共池", "独立会话池", "附件", ".tbrain", "全局提示词.md")
QUERIES = ("项目", "问题", "测试", "记忆", "配置", "安装", "继续", "修复")
CORRECTIONS = ("不是这个", "理解错了", "我说的是", "你搞错", "不是这样", "我不是这个意思")
SNAPSHOTS = Path("snapshots")


def vault_hashes(vault: Path) -> dict:
    result = {}
    for name in OWNED:
        root = vault / name
        files = sorted(root.rglob("*")) if root.is_dir() else [root] if root.is_file() else []
        for path in files:
            if path.is_file():
                result[path.relative_to(vault).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return result


def changed_paths(before: dict, after: dict) -> list[str]:
    return [name for name in before.keys() | after.keys()
            if before.get(name, (None,))[0] != after.get(name, (None,))[0]]


def bigrams(text: str) -> set[str]:
    text = "".join(re.findall(r"\w", text.casefold()))
    return {text[i:i + 2] for i in range(len(text) - 1)}


def section_present(package: str, name: str, expected: str) -> bool:
    pattern = rf"(?m)^## {re.escape(name)}(?:[ \t]*〔sha:[^〕]+〕)?[ \t]*\n"
    for heading in re.finditer(pattern, package):
        body = package[heading.end():]
        boundary = re.search(r"(?m)^## |^<!-- /owner:model -->", body)
        if body[:boundary.start() if boundary else len(body)].strip() == expected.strip():
            return True
    return False


def heuristic_metrics(vault: Path, handoff) -> dict:
    grouped = defaultdict(list)
    sessions = corrections = 0
    for path in (vault / "会话总结").rglob("*.md"):
        summary = handoff.parse(path.read_text(encoding="utf-8-sig"))
        if not summary.session_id:
            continue
        sessions += 1
        corrections += sum(any(phrase in user["text"] for phrase in CORRECTIONS)
                           for user in summary.users)
        if summary.project:
            grouped[summary.project].append(summary)
    overlap = []
    for summaries in grouped.values():
        summaries.sort(key=lambda item: (item.header.get("started_at", ""), item.session_id))
        for previous, current in zip(summaries, summaries[1:]):
            if not previous.target.strip() or not current.users:
                continue
            left = bigrams(previous.target)
            right = bigrams(current.users[0]["text"])
            if left and right:
                overlap.append(len(left & right) / len(left | right))
    return {
        "M5": {"eligible_pairs": len(overlap), "thresholds": {
            str(threshold): {"matched": sum(value >= threshold for value in overlap),
                             "ratio": sum(value >= threshold for value in overlap) / len(overlap) if overlap else None}
            for threshold in (0.3, 0.5, 0.7)}},
        "M6": {"sessions": sessions, "correction_messages": corrections,
               "messages_per_session": corrections / sessions if sessions else None},
    }


def measure_copy(real_vault: Path, work: Path, handoff, flow, prompt_mod, service_type) -> dict:
    vault = work / "vault"
    vault.mkdir(parents=True)
    for name in OWNED:
        source = real_vault / name
        if source.is_dir():
            shutil.copytree(source, vault / name)
        elif source.is_file():
            shutil.copy2(source, vault / name)
    home = work / "home"
    home.mkdir()
    os.environ["TBRAIN_HOME"] = str(home)
    os.environ["TBRAIN_INSTALL_ROOT"] = str(work / "install")
    service = service_type(home, vault=vault)
    service.settings.save({"vault_path": str(vault), "timezone": "Asia/Shanghai",
                           "ntp_enabled": False, "timeout_hours": 0, "task_mode": False})
    service.refresh_index()
    projects = service.store.projects()
    by_project = defaultdict(list)
    for row in service.index.sessions():
        if row["project"]:
            by_project[row["project"]].append(row)
    result = {"project_count": len(projects), "projects_without_sessions": 0,
              "projects_with_prompt": 0, "section_input_counts": {},
              "M1": {}, "M2": {}, "M3": {}}
    section_counts = Counter()
    for host in ("codex", "pi"):
        result["M1"][host] = {"cases": 0, "complete_four": 0, "over_budget": 0,
                              "project_text_over_budget": 0, "by_input_content": {}}
        result["M2"][host] = {"queries": 0, "queries_with_hits": 0, "total_hits": 0,
                              "other_project_hits": 0, "standalone_hits": 0}
        result["M3"][host] = {"rules": 0, "injected": 0, "source_unresolved": 0,
                              "silent_missing": 0}
    for number, project in enumerate(projects):
        name = project["name"]
        rows = sorted(by_project[name], key=lambda row: (row["started_at"], row["session_id"]))
        if rows:
            summary, _ = flow.load_session(service, {**rows[-1], "abs_path": vault / rows[-1]["path"]})
        else:
            result["projects_without_sessions"] += 1
            summary = handoff.new_summary(
                name=f"baseline-empty-{number}", project=name, session_id=f"codex:baseline-empty-{number}",
                agent="codex", cwd=str(work), host_record="", started_at="2026-11-12T00:00:00Z",
                raw_command="")
            flow.write_session(service, summary, service.store.session_path(name, datetime.now(timezone.utc), summary.name))
        data = service.store.project_prompt(name)
        sections = data.get("sections") or {}
        has_prompt = service.store.project_prompt_path(name).is_file()
        if has_prompt:
            result["projects_with_prompt"] += 1
            filled = sum(bool(sections.get(section, "").strip()) for section in prompt_mod.SECTION_ORDER)
            section_counts[filled] += 1
        rules = [line.strip() for line in sections.get("硬规则", "").splitlines() if line.strip().startswith("- ")]
        visible = {name, *service.child_projects(name)}
        for host in ("codex", "pi"):
            package = flow.assemble_startup(service, summary, host, resume=True, related_query=summary.raw_command)
            if has_prompt:
                metrics = result["M1"][host]
                metrics["cases"] += 1
                complete = all(
                    section_present(package, section, str(sections.get(section) or ""))
                    for section in prompt_mod.SECTION_ORDER)
                metrics["complete_four"] += complete
                content_group = "empty" if filled == 0 else "four_nonempty" if filled == 4 else "partial"
                group = metrics["by_input_content"].setdefault(content_group, {"cases": 0, "complete_four": 0})
                group["cases"] += 1
                group["complete_four"] += complete
                metrics["over_budget"] += len(package) > flow.HOST_BUDGETS[host]
                metrics["project_text_over_budget"] += len(prompt_mod.render(sections, data.get("user", ""))) > flow.HOST_BUDGETS[host]
                actual_lines = {line.strip() for line in package.splitlines()}
                rule_metrics = result["M3"][host]
                for rule in rules:
                    rule_metrics["rules"] += 1
                    if rule in actual_lines:
                        rule_metrics["injected"] += 1
                    else:
                        text = re.sub(r"（来源：.*?）", "", rule).strip()
                        flagged = any("来源待核对" in line and text in line for line in actual_lines)
                        rule_metrics["source_unresolved" if flagged else "silent_missing"] += 1
            # Alias the same real input in the temporary index; do not add duplicated searchable notes.
            host_session = f"baseline-{number}"
            service.index.set_session_link(host, host_session, summary.session_id)
            for query in QUERIES:
                response = service.search(query, session_id=f"{host}:{host_session}")
                hits = response.get("results", [])
                metrics = result["M2"][host]
                metrics["queries"] += 1
                metrics["queries_with_hits"] += bool(hits)
                metrics["total_hits"] += len(hits)
                for hit in hits:
                    if hit.get("kind") != "session":
                        continue
                    hit_project = hit.get("project") or ""
                    metrics["other_project_hits"] += bool(hit_project and hit_project not in visible)
                    metrics["standalone_hits"] += not hit_project
    result["section_input_counts"] = dict(sorted(section_counts.items()))
    for metrics in result["M1"].values():
        metrics["complete_ratio"] = metrics["complete_four"] / metrics["cases"] if metrics["cases"] else None
        for group in metrics["by_input_content"].values():
            group["complete_ratio"] = group["complete_four"] / group["cases"]
    cards = [card for card in service.store.public_cards() if not card.get("model_fingerprint")]
    failures = Counter()
    accepted = 0
    for card in cards:
        try:
            service.store.save_public_card(card["kind"], card["name"], {"body": card["body"]})
            accepted += 1
        except ValueError as exc:
            failures["missing_fingerprint_or_user_modified" if "指纹" in str(exc) or "用户修改" in str(exc) else "other_validation"] += 1
    result["M4"] = {"attempted_missing_fingerprint_cards": len(cards), "accepted": accepted,
                    "blocked": sum(failures.values()), "reasons": dict(failures)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Explicit src directory; baseline uses TEMP export of v0.7.3")
    parser.add_argument("--vault", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from tbrain import __version__, handoff, prompt as prompt_mod, session as flow
    from tbrain.service import TbrainService

    real_vault = args.vault.resolve(strict=True)
    work = Path(tempfile.mkdtemp(prefix="tbrain-baseline-"))
    audit = []
    for attempt in range(2):
        started = time.time_ns()
        before = vault_hashes(real_vault)
        heuristics = heuristic_metrics(real_vault, handoff)
        metrics = measure_copy(real_vault, work / f"attempt-{attempt + 1}", handoff, flow, prompt_mod, TbrainService)
        after = vault_hashes(real_vault)
        ended = time.time_ns()
        changed = changed_paths(before, after)
        active_only = bool(changed) and all(
            name.split("/", 1)[0] in {"会话总结", ".tbrain"}
            and started <= after.get(name, before.get(name))[1] <= ended for name in changed)
        audit.append({"hashed_files_before": len(before), "hashed_files_after": len(after),
                      "changed_files": len(changed), "suspected_active_session": active_only})
        if not changed:
            status = "unchanged"
            break
        if not active_only:
            status = "changed_outside_active_session_rule"
            break
        status = "active_session_changes_remain_after_retry"
    result = {"version": __version__, "source": str(args.source.resolve()), "temporary_copy": str(work),
              "real_vault_audit": audit, "real_vault_status": status, **metrics, **heuristics}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-%fZ")
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    output = SNAPSHOTS / f"基线指标-{stamp}.md"
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("# 记忆闭环基线指标\n\n口径：docs/FREEZE-baseline.md。M1–M4在TEMP副本执行。M5/M6只读聚合，不作通过判据。\n\n")
        handle.write("M1按源内容分组：empty为四节全空，partial为1–3节有正文，four_nonempty为四节均有正文。空标题匹配不能证明实质接续。\n\n")
        handle.write("```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```\n")
        handle.write("\n本报告没有运行真实宿主或模型，不证明真实宿主Hook行为。未读取凭据。\n")
    print(json.dumps({"report": str(output), **result}, ensure_ascii=False))
    return 0 if status == "unchanged" else 1


if __name__ == "__main__":
    raise SystemExit(main())
