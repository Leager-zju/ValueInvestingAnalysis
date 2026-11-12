# 平台适配指南

本技能遵循 [Agent Skills 开放规范](https://agentskills.io/specification)，`SKILL.md` 与 `references/` 中的分析规则、估值框架、`scripts/portfolio.py` 在所有平台完全一致。不同的只有**宿主 Agent 提供的能力名称**与**技能目录位置**。

> 规范只定义「技能文件夹里装什么」，不定义「文件夹放哪里」。各客户端自行决定安装路径，因此同一份 skill 在不同工具里要放到不同目录。

## 1. 能力占位符映射

正文里的工具名一律按「需要什么能力」理解，不要按字面去找同名工具。

| 占位符 | 需要的能力 | 典型实现 | 没有等价能力时 |
|---|---|---|---|
| `AskUserQuestion` | 带选项的结构化提问，一次问完并拿到选择 | Claude Code / WorkBuddy 的交互提问工具；多数 CLI 的确认提示 | 用普通对话逐题询问，**等用户回答再继续**，不要替用户默认 |
| `present_files` | 把本地文件作为卡片/预览推给用户 | WorkBuddy 的文件卡片、IDE 的预览面板 | 输出报告的绝对路径；报告本身是单文件 HTML，双击即可打开 |
| `data_*`（`data_quote` / `data_finance` / `data_kline` / `data_news` / `data_notice` / `data_consensus` …） | 结构化行情、财报、日 K、新闻、公告、一致预期 | 任意金融行情 MCP / 数据连接器 / API | 按 `data-sources.md` 的降级链改用检索能力，**并标注来源与日期** |
| `domain_search` | 一句话中文意图 → 结构化数据表 | 深度检索 / 领域搜索类工具 | 用平台自带的网页搜索，把关键数值抄进报告并注明出处 |
| `WebSearch` / `WebFetch` | 网页检索、抓取正文 | 各客户端内置搜索工具 | 请用户粘贴数据，或明确写「未取到」，**禁止用记忆值填充** |
| `westockdata` / `ifind-finance-data` | 备选金融数据技能 | 同类第三方技能 | 直接跳过，继续降级，不要在无人值守场景尝试扫码授权 |
| 定时任务 / 自动化 | 工作日定时触发工作流 B / C | 各客户端的 automation / cron / 计划任务 | 手动触发对应工作流；分析标准不变 |

**唯一硬依赖是 Python 3.9+**（仅标准库，无第三方包）。行情数据源可以降级，Python 不能绕过——取不到 Python 不等于取不到数据，两者不能混为一谈。

## 2. 各客户端安装路径

把整个仓库内容（含 `SKILL.md`）放进下表的目录，**文件夹名必须是 `value-investing-analysis`**，与 frontmatter 的 `name` 一致。

| 客户端 | 用户级 | 项目级 |
|---|---|---|
| Claude Code | `~/.claude/skills/value-investing-analysis/` | `.claude/skills/value-investing-analysis/` |
| OpenAI Codex | `~/.codex/skills/value-investing-analysis/` | `.codex/skills/value-investing-analysis/` 或 `.agents/skills/` |
| Cursor | `~/.cursor/skills/value-investing-analysis/` | `.cursor/skills/value-investing-analysis/` 或 `.agents/skills/` |
| GitHub Copilot / VS Code | `~/.copilot/skills/value-investing-analysis/` | `.github/skills/value-investing-analysis/` 或 `.agents/skills/` |
| Gemini CLI | `~/.gemini/skills/value-investing-analysis/` | `.gemini/skills/value-investing-analysis/` 或 `.agents/skills/` |
| 跨客户端通用约定 | `~/.agents/skills/value-investing-analysis/` | `.agents/skills/value-investing-analysis/` |
| WorkBuddy | `~/.workbuddy/skills/value-investing-analysis/` | 项目 `.workbuddy/skills/` |

`.agents/skills/` 是被多个客户端共同识别的开放约定；只维护一份时优先放这里。路径以各客户端官方文档为准，表格仅作速查。

macOS / Linux 安装示例（Claude Code）：

```bash
git clone https://github.com/Leager-zju/ValueInvestingAnalysis.git \
  ~/.claude/skills/value-investing-analysis
```

Windows PowerShell 示例：

```powershell
git clone https://github.com/Leager-zju/ValueInvestingAnalysis.git `
  "$HOME\.claude\skills\value-investing-analysis"
```

也可使用社区 CLI（`npx skills add Leager-zju/ValueInvestingAnalysis`），它会把技能放进检测到的客户端目录；不同版本对「仓库根目录即技能目录」的识别略有差异，失败时改用上面的 `git clone`。

## 3. 数据目录

持仓档案与报告**不放在技能目录里**，升级或重装技能不会丢数据。

优先级：`VI_DATA_DIR` > `~/.value-investing/` > 旧版 `~/.workbuddy/value-investing/`（仅当该目录已存在时自动沿用）。

```bash
python scripts/portfolio.py path      # 永远先跑这条，确认真实路径
```

自定义目录：

```bash
# macOS / Linux
export VI_DATA_DIR="$HOME/my-vi-data"

# Windows PowerShell
$env:VI_DATA_DIR = "$HOME\my-vi-data"
```

设置后**所有**子命令（`calc` / `new-report` / `lint-report` / `index` / 临时分析）都跟随它。一人多账户、临时分析隔离都靠这个变量实现。

## 4. 定时任务

本技能不自带自动化配置，需要使用者按平台自行创建三个工作日任务（时区按所在市场调整）：

| 任务 | 时间 | 说明 |
|---|---|---|
| 早盘竞价预案 | 09:30 | 工作流 B，输出「条件 → 动作」 |
| 尾盘决策（A 股） | 14:30 | 工作流 C，`--elapsed 210` |
| 尾盘决策（港股） | 15:30 | 工作流 C，`--elapsed 300` |

三个任务都先跑 `python scripts/portfolio.py markets` 做市场闸门，无对应标的时直接跳过。没有定时能力的客户端手动说一句「跑早盘预案 / 跑尾盘决策」即可触发同一套规则。

## 5. 报告交付

报告是单文件自包含 HTML（`assets/report-template.html` 版式），不依赖任何 CDN 或构建步骤：

- 有文件预览能力的客户端：把该 HTML 作为卡片推给用户；
- 没有：回复里给出绝对路径，用户用浏览器打开；
- `python scripts/portfolio.py index` 生成 `reports/index.html`，是所有标的与三个槽位的常驻入口，适合存成浏览器书签。

跨平台一致的三条硬闸门：`lint-report`（样式结构）、`selfcheck`（规则与代码是否脱节）、`check-order`（下单硬约束）。它们与宿主 Agent 无关，任何平台都必须跑。

## 6. 常见误区

- **「我这个客户端没有 `data_quote`，所以跳过取数」** —— 错。占位符指的是能力，换等价工具或走降级链，不能跳过。
- **「没有 `present_files` 就不生成报告」** —— 错。报告必须落盘，只是交付方式从卡片改成路径。
- **直接改脚本里的工具名** —— 错。脚本不调用任何行情工具，它只读写 `portfolio.json` 与本地 JSON，跨平台零改动。
- **把 `README.md` / `LICENSE` 当成技能内容** —— 它们给人类读，Agent 只加载 `SKILL.md` 及其引用的 `references/`。
