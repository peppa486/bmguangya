#!/usr/bin/env python3
import json

# Read main state file
with open('/opt/anime-stack/state/bangumi-sync-state.json', 'r', encoding='utf-8') as f:
    main_state = json.load(f)

# Read report checkpoint
try:
    with open('/opt/anime-stack/state/bangumi-completed-report-state.json', 'r', encoding='utf-8') as f:
        report_state = json.load(f)
except FileNotFoundError:
    report_state = {"reported_guids": []}

reported_guids = set(report_state.get("reported_guids", []))
cloud_tasks = main_state.get("cloud_tasks", {})

# Find newly completed tasks
new_completed = []
for guid, task in cloud_tasks.items():
    if task.get("status") == "completed" and guid not in reported_guids:
        new_completed.append((guid, task))

# Group by show
grouped = {}
for guid, task in new_completed:
    show_name = task.get("show", "未知番剧")
    # Get filename preference
    filename = task.get("remote_file_name") or task.get("target_name") or guid
    if show_name not in grouped:
        grouped[show_name] = []
    grouped[show_name].append(filename)

# Generate message
if not grouped:
    message = "这 10 分钟内没有新下好的番剧。"
else:
    message = "## 光鸭新下好内容\n"
    for show, files in grouped.items():
        files_str = "、".join(files)
        message += f"- {show}：{files_str}\n"
    message = message.strip()

# Update reported guids
for guid, _ in new_completed:
    reported_guids.add(guid)

# Save updated report state
report_state["reported_guids"] = list(reported_guids)
with open('/opt/anime-stack/state/bangumi-completed-report-state.json', 'w', encoding='utf-8') as f:
    json.dump(report_state, f, indent=2, ensure_ascii=False)

# Output message
print(message)
