#!/usr/bin/env python3
"""Export a local library inventory JSON as a complete Markdown cloud-tree report.

This utility reads only the supplied JSON inventory. It never loads runtime
configuration, credentials, or network clients. Missing episodes are rendered
as comparison results, never as cloud files.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


DEFAULT_INVENTORY = Path("/var/lib/bmguangya/library.json")
CATEGORIES = ("在看", "未标记", "想看", "看过")
SECRET_PATTERN = re.compile(r"(?i)(token|password|authorization|cookie)(\s*[:=]\s*)([^\s,;]+)")


class ExportError(RuntimeError):
    """Invalid inventory or report-output error."""


def _single_line(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("\x00", "")
    return SECRET_PATTERN.sub(r"\1\2[redacted]", text)


def _md_text(value: Any) -> str:
    text = html.escape(_single_line(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def _md_code(value: Any) -> str:
    text = _single_line(value)
    max_run = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * (max_run + 1)
    if text.startswith("`") or text.endswith("`"):
        text = " " + text + " "
    return fence + text + fence


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _label(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("episode_key") or value.get("name") or value.get("relative_path") or "未知")
    return str(value)


def _label_list(values: Any) -> list[str]:
    return sorted({_label(value) for value in _list(values) if value is not None})


def _issue_text(value: Any) -> str:
    if isinstance(value, dict):
        code = value.get("code")
        message = value.get("message") or value.get("reason")
        subject = value.get("episode_key") or value.get("episode_id")
        parts = []
        if subject is not None:
            parts.append(str(subject))
        if code:
            parts.append(str(code))
        if message:
            parts.append(str(message))
        return ": ".join(_single_line(part) for part in parts) or "未提供原因细节"
    return _single_line(value) or "未提供原因细节"


def _freshness(inventory: dict[str, Any], *, now: dt.datetime | None = None) -> tuple[str, str]:
    raw = inventory.get("freshness")
    raw = raw if isinstance(raw, dict) else {}
    generated = inventory.get("generated_at") or inventory.get("updated_at")
    state = str(raw.get("state") or "unknown")
    age_text = "未知"
    ttl = raw.get("ttl_seconds")
    try:
        timestamp = dt.datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.timezone.utc)
        age = int((current.astimezone(dt.timezone.utc) - timestamp.astimezone(dt.timezone.utc)).total_seconds())
        if age < -120:
            state, age_text = "future_timestamp", "时间戳在未来"
        else:
            age = max(0, age)
            age_text = "{} 秒".format(age)
            try:
                ttl_seconds = int(ttl)
            except (TypeError, ValueError):
                ttl_seconds = 0
            state = "fresh" if (inventory.get("complete") is True
                                 and not _list(inventory.get("errors"))
                                 and inventory.get("cloud_complete") is not False
                                 and ttl_seconds > 0 and age <= ttl_seconds) else "stale"
    except (TypeError, ValueError, OverflowError):
        state, age_text = "unknown", "未知"
    if inventory.get("complete") is not True or _list(inventory.get("errors")) or inventory.get("cloud_complete") is False:
        state = "incomplete"
    return state, age_text


def _size(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size < 0:
        return ""
    amount = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return "{} {}".format(int(amount) if unit == "B" else "{:.1f}".format(amount), unit)
        amount /= 1024
    return ""


def _tree_lines(nodes: Iterable[Any], *, prefix: str = "") -> list[str]:
    rows = [node for node in nodes if isinstance(node, dict)]
    output: list[str] = []
    for index, node in enumerate(rows):
        last = index == len(rows) - 1
        branch = "└── " if last else "├── "
        name = _md_code(node.get("name") or "(未命名)")
        is_directory = node.get("type") == "directory" or node.get("is_dir") is True
        path = node.get("path") or node.get("relative_path")
        suffix = "/" if is_directory else ""
        detail = []
        if path:
            detail.append("路径 {}".format(_md_code(path)))
        size = _size(node.get("size"))
        if size:
            detail.append(size)
        if node.get("modified"):
            detail.append("修改时间 {}".format(_md_code(node["modified"])))
        if node.get("incomplete") is True:
            detail.append("目录扫描不完整")
        detail_text = " — " + "；".join(detail) if detail else ""
        output.append(prefix + branch + name + suffix + detail_text)
        if is_directory:
            children = _list(node.get("children"))
            next_prefix = prefix + ("    " if last else "│   ")
            if children:
                output.extend(_tree_lines(children, prefix=next_prefix))
            elif node.get("incomplete") is True:
                output.append(next_prefix + "└── (目录内容未能完整采集)")
            else:
                output.append(next_prefix + "└── (空目录)")
    return output


def _show_labels(show: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
    episodes = [item for item in _list(show.get("episodes")) if isinstance(item, dict)]
    present = _label_list(show.get("present_eps"))
    if not present:
        present = sorted({str(item.get("episode_key")) for item in episodes
                          if item.get("present") is True and item.get("episode_key")})
    missing = _label_list(show.get("missing_eps"))
    extras = _label_list(show.get("extra_eps"))
    duplicates = set(_label_list(show.get("duplicate_eps")))
    for item in episodes:
        if item.get("episode_key") and isinstance(item.get("present_files"), list) and len(item["present_files"]) > 1:
            duplicates.add(str(item["episode_key"]))
    if not extras:
        extras = sorted({str(item.get("episode_key")) for item in episodes
                         if item.get("air_state") == "extra" and item.get("episode_key")})
    return present, missing, extras, sorted(duplicates)


def _unchecked_reasons(show: dict[str, Any], inventory: dict[str, Any], expected: list[str]) -> list[str]:
    reasons: list[str] = []
    if inventory.get("complete") is not True or _list(inventory.get("errors")):
        reasons.append("全局库存采集不完整")
    freshness_state, _age = _freshness(inventory)
    if freshness_state != "fresh":
        reasons.append("库存快照新鲜度为 {}，不能据此确认当前缺集".format(freshness_state))
    if inventory.get("cloud_complete") is False:
        reasons.append("Alist 云盘树扫描不完整")
    if show.get("incomplete") is True:
        reasons.append("该云盘目录扫描不完整")
    if show.get("ambiguous") is True:
        reasons.append("Bangumi 番名与云盘目录匹配有歧义")
    if show.get("coverage_unknown") is True:
        issues = [_issue_text(issue) for issue in _list(show.get("coverage_issues"))]
        if not issues:
            issues = [_issue_text(item.get("mapping_issue")) for item in _list(show.get("episodes"))
                      if isinstance(item, dict) and item.get("mapping_issue")]
        reasons.extend(issues or ["Bangumi 集号元数据不完整"])
    if show.get("unmatched") is True and show.get("subject_id") and (show.get("source_path") or show.get("path")):
        reasons.append("此 Bangumi 目标尚未唯一匹配到云盘目录")
    if not expected:
        if show.get("subject_id"):
            reasons.append("没有可核对的 Bangumi episode 元数据")
        else:
            reasons.append("未关联 Bangumi 目标，无法判定预期集数")
    return list(dict.fromkeys(reasons))


def _show_report(show: dict[str, Any], inventory: dict[str, Any], *, category: str) -> list[str]:
    title = show.get("name") or show.get("cloud_name") or show.get("subject_id") or "未命名条目"
    lines = ["### {}".format(_md_text(title))]
    subject_id = show.get("subject_id")
    if subject_id:
        lines.append("- Bangumi ID：{}".format(_md_code(subject_id)))
    else:
        lines.append("- Bangumi ID：无（云盘条目未匹配到 Bangumi）")
    source_path = show.get("source_path") or show.get("path")
    if source_path:
        lines.append("- 云盘目录：{}".format(_md_code(source_path)))
    else:
        lines.append("- 云盘目录：**云盘尚无目录**")

    expected = _label_list(show.get("expected_eps"))
    present, missing, extras, duplicates = _show_labels(show)
    unchecked = _unchecked_reasons(show, inventory, expected)
    lines.append("- Bangumi 预期集（{}）：{}".format(len(expected),
        ", ".join(_md_code(item) for item in expected) if expected else "未知"))
    lines.append("- 云盘实存集（{}）：{}".format(len(present),
        ", ".join(_md_code(item) for item in present) if present else "无可识别集号"))
    if unchecked:
        lines.append("- 缺集状态：未核对")
        lines.append("- 未核对原因：{}".format("；".join(_md_text(reason) for reason in unchecked)))
    else:
        # This is a difference report only; these labels are deliberately never
        # inserted into the actual file tree below.
        lines.append("- 缺集（仅库存差集，不代表云盘文件）：{}".format(
            ", ".join(_md_code(item) for item in missing) if missing else "无"))
        lines.append("- 未核对原因：无（该条目有可用的集数对照）")
    lines.append("- 额外集/云盘视频（{}）：{}".format(len(extras),
        ", ".join(_md_code(item) for item in extras) if extras else "无"))
    lines.append("- 重复集号（{}）：{}".format(len(duplicates),
        ", ".join(_md_code(item) for item in duplicates) if duplicates else "无"))
    future = _label_list(show.get("future_eps"))
    if future:
        lines.append("- 未播集：{}".format(", ".join(_md_code(item) for item in future)))
    unknown_airdate = _label_list(show.get("unknown_airdate_eps"))
    if unknown_airdate:
        lines.append("- 播出时间未知：{}".format(", ".join(_md_code(item) for item in unknown_airdate)))

    children = _list(show.get("children"))
    if source_path or children:
        lines.append("#### 云盘实际目录树")
        lines.extend(_tree_lines(children) if children else ["- (目录为空或库存未包含子项)"])
    else:
        lines.append("#### 云盘实际目录树")
        lines.append("- 云盘尚无该 Bangumi 目标的目录；上面的缺集列表只是 Bangumi 与库存的差集，不是云盘文件。")
    lines.append("")
    return lines


def render_markdown(inventory: dict[str, Any], *, now: dt.datetime | None = None) -> str:
    tree = inventory.get("tree")
    if not isinstance(tree, list):
        raise ExportError("inventory.tree must be an array")
    by_category = {item.get("category"): item for item in tree if isinstance(item, dict)}
    generated = inventory.get("generated_at") or inventory.get("updated_at") or "未知"
    freshness_state, age_text = _freshness(inventory, now=now)
    source = inventory.get("source") if isinstance(inventory.get("source"), dict) else {}
    stats = inventory.get("stats") if isinstance(inventory.get("stats"), dict) else {}
    counts = inventory.get("counts") if isinstance(inventory.get("counts"), dict) else {}
    errors = _list(inventory.get("errors"))
    complete = inventory.get("complete") is True and not errors
    lines = [
        "# Bangumi / Alist 全树库存报告",
        "",
        "- 快照生成时间：{}".format(_md_code(generated)),
        "- 库存完整性：{}".format("完整" if complete else "不完整/未确认"),
        "- 当前新鲜度：{}（快照年龄 {}）".format(_md_text(freshness_state), _md_text(age_text)),
        "- 云盘根目录：{}".format(_md_code(source.get("cloud_root") or "未知")),
        "- Bangumi 目标番数：{}；云盘实际目录数：{}；云盘实际视频文件数：{}；库存报告缺集数：{}".format(
            _md_text(counts.get("bangumi_targets", stats.get("bangumi_target_shows", "未知"))),
            _md_text(counts.get("cloud_directories", stats.get("cloud_directories", "未知"))),
            _md_text(counts.get("cloud_video_files", stats.get("total_vids", "未知"))),
            _md_text(counts.get("missing_episodes", stats.get("missing_episodes", "未知"))),
        ),
        "",
        "> 本报告按 Bangumi 目标与 Alist 实际文件树分别列示。缺集只表示已知集数与当前库存的差集，不会作为云盘文件生成。",
        "",
    ]

    if errors:
        lines.extend(["## 库存采集错误", ""])
        for issue in errors:
            if isinstance(issue, dict):
                parts = [issue.get(key) for key in ("stage", "code", "path", "message") if issue.get(key)]
                lines.append("- {}".format(_md_text(" — ".join(_single_line(part) for part in parts))))
            else:
                lines.append("- {}".format(_md_text(issue)))
        lines.append("")

    for category in CATEGORIES:
        section = by_category.get(category)
        lines.append("## {}".format(_md_text(category)))
        if section is None:
            lines.extend(["- 库存未提供此分类。", ""])
            continue
        shows = [item for item in _list(section.get("shows")) if isinstance(item, dict)]
        lines.append("- Bangumi/云盘条目数：{}；分类根目录：{}".format(
            len(shows), _md_code(section.get("path") or category)))
        loose = _list(section.get("loose_files")) + _list(section.get("unmarked_root_files"))
        if loose:
            lines.append("- 分类根目录散落文件：")
            lines.extend(_tree_lines(loose))
        if not shows:
            lines.append("- 无番剧目录/目标记录。")
        else:
            lines.append("")
            for show in shows:
                lines.extend(_show_report(show, inventory, category=category))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def read_inventory(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError("cannot read inventory JSON: {}".format(path)) from exc
    if not isinstance(payload, dict):
        raise ExportError("inventory root must be an object")
    return payload


def write_report(path: Path, content: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".library-tree-", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY,
                        help="inventory JSON (read-only; no config or network access)")
    parser.add_argument("--output", type=Path, required=True,
                        help="Markdown report destination")
    args = parser.parse_args(argv)
    try:
        inventory_path = args.inventory.expanduser().resolve()
        output_path = args.output.expanduser().absolute()
        if inventory_path == output_path.resolve():
            raise ExportError("output path must differ from the inventory input")
        content = render_markdown(read_inventory(inventory_path))
        write_report(output_path, content)
    except (ExportError, OSError, ValueError) as exc:
        parser.exit(1, "error: {}\n".format(exc))
    print("Wrote Markdown library tree report: {}".format(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
