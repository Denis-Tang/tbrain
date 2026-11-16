"""Read Markdown directly; emit aggregate sizes without a service or index."""

import argparse
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_baseline import changed_paths, vault_hashes
from tbrain.store import PUBLIC_KINDS, PROMPT_SECTIONS, _parse_sections, parse_blocks


def distribution(values):
    def group(items):
        ordered = sorted(items)
        return {"n": len(ordered),
                "p50": ordered[math.ceil(.5 * len(ordered)) - 1] if ordered else None,
                "p90": ordered[math.ceil(.9 * len(ordered)) - 1] if ordered else None,
                "max": ordered[-1] if ordered else None}

    return {"all": group(values), "empty": group([n for n in values if n == 0]),
            "nonempty": group([n for n in values if n > 0])}


def measure(vault):
    before = vault_hashes(vault)
    rows = []
    for path in (vault / "项目").glob("*/项目提示词.md"):
        text = path.read_text(encoding="utf-8-sig")
        blocks = parse_blocks(text)
        sections = _parse_sections(blocks.get("model", {}).get("body", "") if blocks else text)
        user = blocks.get("user", {}).get("body", "")
        rows.append({**{name: len(body) for name, body in sections.items()},
                     "user_content": len(re.sub(r"^##\s*用户补充\s*", "", user.strip()).strip()),
                     "user_raw": len(user), "model_total": sum(map(len, sections.values())),
                     "four_sections_with_headings": len("\n\n".join(
                         f"## {name}\n{sections[name]}" for name in PROMPT_SECTIONS))})
    global_path = vault / "全局提示词.md"
    global_text = global_path.read_text(encoding="utf-8-sig") if global_path.is_file() else ""
    global_blocks = parse_blocks(global_text)
    global_user = global_blocks.get("user", {}).get("body", "")
    public_pending = vault / "公共池" / "待用户确认.md"
    cards = {kind: sum(1 for path in (vault / "公共池" / kind).glob("*.md") if path.is_file())
             for kind in PUBLIC_KINDS}
    result = {
        "project_prompt_files": len(rows),
        "sections": {name: distribution([row[name] for row in rows]) for name in (
            *PROMPT_SECTIONS, "user_content", "user_raw", "model_total", "four_sections_with_headings")},
        "global": {"files": int(global_path.is_file()), "file_chars": len(global_text),
                   "user_block_chars": len(global_user),
                   "model_block_chars": len(global_blocks.get("model", {}).get("body", "")),
                   "current_injected_user_chars": len(global_user.strip()) if "user" in global_blocks
                   else len(global_text.strip())},
        "project_pending": distribution([len(path.read_text(encoding="utf-8-sig"))
                                         for path in (vault / "项目").glob("*/待用户确认.md")]),
        "public_pending_chars": len(public_pending.read_text(encoding="utf-8-sig")) if public_pending.is_file() else 0,
        "public_cards_by_kind": cards, "public_card_files": sum(cards.values()),
        "public_kind_dirs_with_cards": sum(count > 0 for count in cards.values()),
    }
    after = vault_hashes(vault)
    result["hash_check"] = {"managed_files_before": len(before), "managed_files_after": len(after),
                            "changed_files": len(changed_paths(before, after))}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault", type=Path)
    args = parser.parse_args()
    if not args.vault.is_dir():
        parser.error("vault must be an existing directory")
    print(json.dumps(measure(args.vault), ensure_ascii=False, indent=2))
