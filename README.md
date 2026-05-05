# Bangumi → Mikan → GuangYa 自动化离线下载流水线

全自动追番流水线：以 [Bangumi](https://bgm.tv/) 收藏为源头，自动从 [Mikan Project](https://mikanani.me/) 匹配种子，直接提交到 [光鸭云盘](https://www.guangyapan.com/) 离线下载，完成后即可在光鸭直接观看或挂载到播放器。

## 核心特性

- 📺 **以 Bangumi 为主数据源** — 直接读取你的「想看 / 在看 / 看过」收藏状态，无需额外维护列表
- 🔍 **自动 Mikan 匹配** — 对每部番自动搜索 Mikan，拉取 RSS 解析种子
- 🎯 **智能选种过滤** — 按字幕组优先级偏好、简体/繁体过滤、分辨率偏好自动选择最优版本
- ✂️ **单集去重** — 每一集只保留一个最优版本，避免同一集下载多个不同字幕组/分辨率版本
- 🧹 **干净命名** — 按 Bangumi 原名整理目录结构，文件名只保留纯集号 (`01.mkv`, `02.mp4`)，去除发布组噪音
- 📁 **按收藏状态分类** — `想看/` `在看/` `看过/` 自动分类，Bangumi 状态变更时自动移动目录
- 🗑️ **删除联动** — Bangumi 删除条目时自动删除 GuangYa 对应目录
- 📊 **状态持久化** — 避免重复提交，支持增量同步
- 🔄 **配额感知** — 识别光鸭每日配额耗尽错误并自动停止提交

## 目录结构

```
bangumi-mikan-guangya-automation/
├── config/                # 配置示例
│   ├── bangumi-sync.example.json      # qBittorrent 版配置模板
│   └── bangumi-guangya.example.json   # GuangYa 离线版配置模板
├── scripts/
│   ├── bangumi_mikan_qb_sync.py       # 基础版：Bangumi → Mikan → qBittorrent
│   ├── bangumi_cloud_download.py      # 离线版：Bangumi → Mikan → GuangYa （本项目主力）
│   └── check_new_completed.py         # 检查最近完成的下载，可用于通知推送
├── tests/
│   └── test_bangumi_cloud_download.py # 单元测试（核心逻辑）
├── systemd/
│   └── bangumi-mikan-sync.service     # systemd 服务示例
└── README.md
```

## 推荐命名格式

本项目使用以下目录结构，契合直接从 GuangYa 挂载播放的需求：

```
Anime/
├── 想看/
│   └── 葬送的芙莉莲/
│       ├── 01.mkv
│       ├── 02.mkv
│       └── 01.ass
├── 在看/
│   └── 药屋少女的呢喃 第2期/
│       ├── 01.mkv
│       └── ...
└── 看过/
    └── 孤独摇滚！/
        ├── 01.mkv
        └── ...
```

- 一级目录：Bangumi 收藏状态分类
- 二级目录：直接使用 Bangumi 显示名（天然区分季/部/Part）
- 文件名：纯集数，保留原扩展名 → `01.mkv`, `01-12.mkv`（合集）

## 快速开始

### 1. 前置依赖

- Python 3.8+
- requests
- 你的 GuangYa 账号（需要 access_token / refresh_token，可从 [guangyaclient](https://github.com/DDSRem-Dev/guangyaclient) 获取获取方式）
- GuangYa 客户端代码放在 `/opt/guangya-webdav/`（本脚本依赖其 `GuangYaClient`）

### 2. 克隆项目

```bash
git clone https://github.com/YOUR_USERNAME/bangumi-mikan-guangya-automation.git
cd bangumi-mikan-guangya-automation
```

### 3. 复制并编辑配置

```bash
cp config/bangumi-guangya.example.json config/bangumi-sync.json
# 编辑填入你的 Bangumi 用户名、access_token 等信息
nano config/bangumi-sync.json
```

配置项说明：

| 配置项 | 说明 |
|--------|------|
| `dry_run` | `true` 为试运行，不真实提交任务；测试没问题后改 `false` |
| `tracked_collection_types` | 跟踪哪些 Bangumi 收藏类型，默认 `[1, 2, 3]` = 想看/看过/在看 |
| `download_collection_types` | 对哪些类型执行下载，默认 `[3]` = 只下载「在看」 |
| `max_new_cloud_tasks_per_run` | 单次运行最多提交多少新任务，`0` 表示不限；首次迁移建议限流 |
| `bangumi.username` | 你的 Bangumi 用户名 |
| `bangumi.access_token` | 你的 Bangumi 开发者 access_token |
| `filters.preferred_subgroups` | 字幕组优先级排序，排在前面优先选 |
| `filters.exclude_keywords` | 排除关键词，默认排除繁体/BIG5/粤语/外挂 |
| `guangya.root_dir` | GuangYa 根目录名称，默认 `Anime` |

### 4. 试运行（dry-run）

```bash
# 用系统 Python 即可跑通 dry-run，不需要 GuangYa 依赖
python scripts/bangumi_cloud_download.py --config config/bangumi-sync.json --dry-run
```

查看输出统计，确认匹配和选种逻辑符合预期。

### 5. 真实运行

确保 `dry_run` 已经改成 `false`，然后：

```bash
# 需要用 GuangYa 客户端虚拟环境的 Python 来运行
/opt/guangya-webdav/venv/bin/python scripts/bangumi_cloud_download.py --config config/bangumi-sync.json
```

### 6. 定时同步

推荐用 systemd timer 或 cron 每小时或每 30 分钟自动同步：

```cron
# 示例：每小时同步一次（北京时间）
0 * * * * /opt/guangya-webdav/venv/bin/python /path/to/bangumi-mikan-guangya-automation/scripts/bangumi_cloud_download.py --config /path/to/config/bangumi-sync.json >> /path/to/state/sync.log 2>&1
```

## 默认字幕组优先级

项目内置符合中文用户习惯的字幕组优先级：

```json
[
  "北宇治字幕组", "喵萌奶茶屋", "LoliHouse", "千夏字幕组",
  "桜都字幕组", "SweetSub", "澄空学园&动漫国字幕组", "雪飘工作室",
  "豌豆字幕组", "幻樱字幕组", "风之圣殿", "北宇治字幕组",
  "悠哈璃羽字幕社", "霜庭云花Sub", "绿茶字幕组", "ANi",
  "Nekomoe kissaten", "Lilith-Raws", "Skymoon-Raws"
]
```

优先 1080p，排除繁体资源。可在配置中修改。

## 鸣谢

本项目站在巨人的肩膀上，感谢以下项目和作者：

- [DDSRem-Dev/guangyaclient](https://github.com/DDSRem-Dev/guangyaclient) — GuangYa API 客户端实现，为光鸭接口调用提供了基础
- [ShukeBta/Guangyadisk](https://github.com/ShukeBta/Guangyadisk) — 另一个光鸭云盘 WebDAV 项目，提供了接口参考
- [Bangumi](https://bgm.tv/) — 提供开放 API 和专业的动画条目数据库
- [Mikan Project](https://mikanani.me/) — 提供开放的番剧 RSS 种子搜索服务

感谢所有开源社区的贡献者！

## 关于光鸭配额

光鸭云盘免费用户每日离线添加任务有配额限制（大约数百个/天）。本脚本检测到配额用尽（错误码 354）后会立即停止提交，等待次日自然恢复后继续。

## 许可证

MIT License

## 相关项目

如果你需要本地下载后整理再同步到 Google Drive，可以参考原始的 [Bangumi → Mikan → qBittorrent → Jellyfin → GDrive](https://github.com/...) 完整链路。
