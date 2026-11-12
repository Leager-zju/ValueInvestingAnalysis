#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价值投资持仓与决策辅助工具（对话与定时任务共享同一份数据）。

数据目录：默认 ~/.value-investing/（可用环境变量 VI_DATA_DIR 覆盖；旧版目录自动兼容）
  - portfolio.json      持仓快照：每个标的的「总资金 / 持股数 / 最新持仓成本」+ 投资逻辑与估值锚
  - analysis_log.jsonl  当日分析埋点（非交易记录）：按「标的+任务+日期」唯一，同日重复触发覆盖；含区间与四锚快照，供日后复盘比对
  - reports/            分析报告输出目录

子命令：
  path                         打印数据目录与文件路径
  show [--code C] [--json]     查看持仓
  upsert --code C [...]        建档/更新持仓（持股数与成本价照抄券商，不推算；含底仓/波段仓划分）
  set-prefs --regret-aversion  设置踏空厌恶程度（low|mid|high），影响建仓阶梯
  set-type --code C --primary  确认公司形态（林奇六类）与估值范式，须附反方证据；决定分析侧重与尺子
  set-prefs ...                全局画像：踏空厌恶 / 承受浮亏（属于人，跨标的不变）
  plan-swing --code C --price P --ratio R     按可用资金算最多可买股数，并按比例划分底仓/波段仓（取整到手）
  set-profile --code C ...    标的画像：建仓风格 / 窗口 / 波段仓（属于这只票，首次分析时定）
  set-drivers --code C --file  登记关键驱动因子（能繁存栏、猪价、批价、运价…），早盘须先查因子再定预案
  set-thesis --code C --file F 合并写入投资逻辑/估值锚/上行期权/卖出触发条件（JSON 文件）
  option --code C --file F     登记上行期权清单并算出每股期权价值（含 30% 上限折扣）
  remove --code C              删除标的
  markets                      列出持仓涉及的市场（用于决定需要哪些定时任务）
  calc --code C --price P      计算盈亏、五档估值区、建仓阶梯、期权价值
  volprice --file F            量价定性（五选一），输入日K JSON
  check-order --code C ...     按硬约束校验一笔拟下单
  log --code C ...             写当日分析记录（同标的+同任务+同日只留最新一条，重复触发覆盖旧记录）
  last-log [--code C] [--n N]  读取最近的分析结论
  new-report --code C --session 取报表落盘路径（固定槽位 open/close/deep，同名覆盖），并返回现成的 markdown_link
  index                        生成 reports/index.html 报告索引（关掉任务结果后仍可再打开的入口）
  lint-report --code C --session S  检查报表 HTML 的结构性错误（样式静默失效类问题）

估值锚（五档，顺序必须递增）：
  deep_value_price < core_entry_price < fair_high_price < overvalued_above_price
  · deep_value_price   深度低估线：core_entry × (1 − 安全边际)，重仓用
  · core_entry_price   底仓线 / 合理区下沿：基础价值下沿，不含期权
  · fair_high_price    合理上沿：基础价值上沿 + 期权价值 × 0.6（买入侧期权打折）
  · overvalued_above   透支线：基础价值上沿 + 期权价值 × 1.0 再 × 1.15（卖出侧期权全额）
  · 期权价值超过基础价值中枢 30% 的部分按 50% 计入，防止"讲故事式估值"
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

def _resolve_data_dir():
    """平台中立的数据目录；旧版目录已存在时自动沿用，避免升级丢数据。

    优先级：VI_DATA_DIR > ~/.value-investing/ > 旧版 ~/.workbuddy/value-investing/
    """
    override = os.environ.get("VI_DATA_DIR")
    if override:
        return Path(override).expanduser()
    preferred = Path.home() / ".value-investing"
    legacy = Path.home() / ".workbuddy" / "value-investing"
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


DATA_DIR = _resolve_data_dir()
PORTFOLIO = DATA_DIR / "portfolio.json"
LOG = DATA_DIR / "analysis_log.jsonl"
REPORTS = DATA_DIR / "reports"

# 单日交易分钟数与 14:30 / 15:30 时点已交易分钟数
SESSION_MINUTES = {"A": 240, "HK": 330, "US": 390}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(msg, code=1):
    out({"ok": False, "error": msg})
    sys.exit(code)


# ---------------------------------------------------------------- 代码规范化
def normalize_code(raw):
    c = raw.strip().lower().replace(" ", "")
    for suf, pre in ((".sh", "sh"), (".ss", "sh"), (".sz", "sz"), (".bj", "bj"), (".hk", "hk")):
        if c.endswith(suf):
            c = pre + c[: -len(suf)]
    if c[:2] in ("sh", "sz", "bj", "hk", "us"):
        body = c[2:]
        if c.startswith("hk"):
            return "hk" + body.zfill(5)
        return c
    if c.isdigit():
        if len(c) == 6:
            if c[0] in "659":
                return "sh" + c
            if c[0] in "03" or c[0] == "1":
                return "sz" + c
            if c[0] in "48":
                return "bj" + c
        if len(c) <= 5:
            return "hk" + c.zfill(5)
    if c.isalpha() or "." in c:
        return "us" + c.upper()
    return c


def market_of(code):
    if code.startswith(("sh", "sz", "bj")):
        return "A"
    if code.startswith("hk"):
        return "HK"
    return "US"


def default_lot(market):
    return 100 if market == "A" else (1 if market == "US" else None)


def price_limit_pct(code, name=""):
    """A 股涨跌幅限制；港美股返回 None（无涨跌停）。"""
    if market_of(code) != "A":
        return None
    n = (name or "").upper()
    body = code[2:]
    if code.startswith("bj"):
        return 0.30
    if body.startswith(("688", "689", "300", "301")):
        return 0.20
    if "ST" in n:
        return 0.05
    return 0.10


# ---------------------------------------------------------------- 偏好与存储
DEFAULT_PREFS = {
    "regret_aversion": "mid",    # 踏空厌恶：low|mid|high，决定空仓时的建仓门槛
    "drawdown_tolerance": 0.25,  # 单标的可承受的最大浮亏（0.1–0.6），用于情绪防护提示
    "entry_style": "mixed",      # 建仓风格：left 左侧摊薄 / swing 纪律波段 / right 右侧确认 / mixed 混合
    "entry_zone_ratio": 0.33,    # 「时间换价格」只在合理区下部这一段内启用（0=只在底仓线，0.33=下三分之一，1=整个合理区）
    "profile_note": "",          # 画像问卷的原始答案摘要（换机/复核时用得上）
}

# 踏空厌恶 → 建仓策略参数
#   tier1_zone : 建底仓所要求的估值区（deep=只在深度低估；entry=低估区；fair=合理区也允许）
#   time_entry : 价格长期不回落到核心区时，是否允许「时间换价格」分批建立底仓
REGIME = {
    "low":  {"tier1_zone": "deep",  "time_entry": False, "tranche": "1/3", "rounds": 0},
    "mid":  {"tier1_zone": "entry", "time_entry": False, "tranche": "1/3", "rounds": 0},
    "high": {"tier1_zone": "fair",  "time_entry": True,  "tranche": "1/3", "rounds": 3},
}

# 安全边际随质地变化：质地越高，要求的折价越小（好公司很少给你深折价）
MARGIN_BY_QUALITY = [(8, 0.12), (6, 0.20), (0, 0.30)]

# 公司性质：类型不同，分析侧重与估值方法都不同（详见 references/company-type.md）
TYPE_ENUM = {
    "slow_grower": "缓慢增长型",
    "stalwart": "稳健增长型",
    "fast_grower": "快速增长型",
    "cyclical": "周期型",
    "turnaround": "困境反转型",
    "asset_play": "隐蔽资产型",
}

# 估值范式与形态分离：形态说"增长从哪来"，范式说"用哪把尺子量"
MODEL_ENUM = {
    "pe_band": "PE 历史分位",
    "peg": "PEG",
    "cape_pb": "CAPE + PB 分位",
    "dividend_yield": "股息率锚",
    "pb_roe": "PB–ROE",
    "rnav": "重估净资产 RNAV",
    "normalized_eps": "正常化盈利",
}
MODEL_DEFAULT = {"slow_grower": "pe_band", "stalwart": "pe_band", "fast_grower": "peg",
                 "cyclical": "cape_pb", "turnaround": "normalized_eps", "asset_play": "rnav"}

# 建仓风格：决定"钱怎么进场、跌了怎么办"
STYLE_ENUM = {
    "left": "左侧摊薄型",
    "swing": "纪律波段型",
    "right": "右侧确认型",
    "mixed": "混合/基本面优先",
}
STYLE_RATIO = {"left": 1.0, "swing": 0.5, "right": 0.0, "mixed": 0.33}
STYLE_NOTE = {
    "left": ["允许在整个合理区内分批承接（窗口 100%）",
             "但摊薄有硬上限：累计不超过该标的 budget、每次加仓价须比上次低 5% 以上、最多 3 轮",
             "跌破深度低估线后停止摊薄，改为观察 A 类信号"],
    "swing": ["合理区下半段（窗口 50%）可建底仓",
              "买贵了用『先卖后买』的波段修复，规则见 decision-rules 第 7 节",
              "波段买不回就接受少赚，不追高"],
    "right": ["本框架不提供右侧追入：窗口 0%，只在底仓线及以下建仓",
              "若确要做趋势仓，须单独记账、≤总资金 10%、并配移动止损，不计入价值仓"],
    "mixed": ["合理区下三分之一（窗口 33%）建底仓",
              "每次加仓前先复核基本面，基本面未变才执行"],
}

# 属性标签：描述"是什么行业/有什么特征"，不等同于形态
TAG_ENUM = {
    "dividend": "红利",
    "financial": "金融",
    "resource": "资源",
    "platform": "平台",
    "policy": "政策敏感",
    "state_owned": "国企",
    "small_cap": "中小市值",
    "pre_profit": "未盈利",
}

# 旧 key → 新 key（历史数据迁移，仅用于 primary）
TYPE_ALIAS = {"growth": "fast_grower", "compounder": "stalwart",
              "dividend": "slow_grower", "financial": "stalwart",
              "周期": "cyclical", "成长": "fast_grower"}

TYPE_FOCUS = {
    "slow_grower": ["股息率与分红可持续性（FCF 覆盖 ≥1.2 倍）",
                    "份额是否在流失",
                    "资本开支是否在无谓扩张",
                    "有没有用成长股的价钱买它"],
    "stalwart": ["ROE 的持续性而非单年高低",
                 "毛利率与提价的可持续性",
                 "份额与渠道库存",
                 "涨到 30–50% 后是否该换股（成长有天花板）"],
    "fast_grower": ["增速的质量（是否靠补贴/并表/低价换量）",
                    "资本开支到收入的转化周期",
                    "增长故事能否具体到店/产品/时点",
                    "一致预期方向（连续下修是危险信号）"],
    "cyclical": ["产能周期位置（在建/现有）与库存",
                 "成本曲线位置（是否处于行业左侧）",
                 "价差与销量的边际变化",
                 "CAPE/PB 分位，当期 PE 是陷阱"],
    "turnaround": ["资产负债表能撑多久（现金/短债/到期结构）",
                   "正常化盈利的假设是否可验证",
                   "现金流转正的时点",
                   "是否会二次恶化（失败要快认）"],
    "asset_play": ["资产是否真的可变现（土地性质/矿权年限/股权流通性）",
                   "负债会不会吞掉资产价值",
                   "有无明确的价值释放催化剂",
                   "大股东有没有动机释放"],
}

TYPE_BREAK = {
    "slow_grower": "分红率下调且无资本开支解释 / FCF 覆盖 <1 / 份额持续流失",
    "stalwart": "ROE 中枢下移 >3pct / 毛利率连续 3 期累计降 ≥5pct 且份额流失",
    "fast_grower": "增速连续 2 季降到个位数 / 预期连续下修 / capex 上升而收入不加速",
    "cyclical": "在建产能 > 现有 30% / 成本曲线右移 / 价格跌破行业现金成本",
    "turnaround": "现金流转正再推迟 2 个季度 / 债务展期失败 / 重组方退出",
    "asset_play": "催化剂落空（出售/分立中止）/ 资产减值证实不可变现",
}

# 驱动因子：股价由哪些行业数据驱动（详见 references/drivers.md）
DRIVER_FREQ_DAYS = {"daily": 1, "weekly": 7, "monthly": 35, "quarterly": 100}  # event 类不计滞后
DRIVER_LEVELS = ("core", "secondary", "watch")
DRIVER_CATEGORIES = ("industry", "company", "policy", "rate")
POLICY_DIRECTIONS = ("bullish", "bearish", "neutral")
POLICY_MAX_MONTHS = 36          # 政策影响窗口上限：超过 3 年的"影响"基本无法验证
DRIVER_LEADS = ("leading", "coincident", "lagging")

TYPE_KEYWORDS = [("cyclical", ("周期",)), ("turnaround", ("反转", "困境")),
                 ("asset_play", ("隐蔽资产", "重估", "RNAV")),
                 ("fast_grower", ("成长", "快速")), ("stalwart", ("稳健", "核心资产", "消费", "品牌")),
                 ("slow_grower", ("缓慢", "红利", "高股息", "公用"))]


def margin_for(quality):
    try:
        q = float(quality)
    except (TypeError, ValueError):
        return 0.20
    for lo, m in MARGIN_BY_QUALITY:
        if q >= lo:
            return m
    return 0.30


# 旧「四锚」→ 新「双层估值四锚」的字段映射（只做兼容，不臆造数字）
LEGACY_ANCHORS = {"buy_below_price": "deep_value_price", "fair_low_price": "core_entry_price"}


def normalize_valuation(h):
    """把旧 schema 的锚定价映射到新字段；新锚齐全时清除旧字段与迁移标记。"""
    v = h.get("valuation") or {}
    moved = []
    complete = all(v.get(k) for k in
                   ("deep_value_price", "core_entry_price", "fair_high_price", "overvalued_above_price"))
    if complete:  # 已按新框架重建，清掉旧键与迁移痕迹
        for old in LEGACY_ANCHORS:
            if old in v:
                v.pop(old, None)
                moved.append(f"清除 {old}")
        if v.pop("_schema", None):
            moved.append("清除 _schema")
        v.pop("_legacy_note", None)
    else:         # 尚未重建，做只读映射并留标记
        for old, new in LEGACY_ANCHORS.items():
            if old in v and new not in v:
                v[new] = v[old]
                moved.append(f"{old}→{new}")
        if moved:
            v["_schema"] = "legacy-4anchor"
            v["_legacy_note"] = ("旧四锚已映射为新字段，但未含上行期权（option_value）与基础价值三档；"
                                 "建议用 option + set-thesis 按双层估值重建，否则买入侧仍会系统性偏低")
    h["valuation"] = v
    return v, moved


def load():
    if not PORTFOLIO.exists():
        return {"version": 2, "updated_at": None, "prefs": dict(DEFAULT_PREFS), "holdings": {}}
    with open(PORTFOLIO, "r", encoding="utf-8") as f:
        data = json.load(f)
    prefs = data.get("prefs") or {}
    for k, v in DEFAULT_PREFS.items():
        prefs.setdefault(k, v)
    data["prefs"] = prefs
    for h in (data.get("holdings") or {}).values():
        normalize_valuation(h)
        normalize_type(h)
        normalize_drivers(h)
        normalize_split(h)
    return data


def regime_of(data):
    ra = (data.get("prefs") or {}).get("regret_aversion", "mid")
    return ra if ra in REGIME else "high", REGIME.get(ra, REGIME["high"])


def save(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = now()
    if PORTFOLIO.exists():
        bak = PORTFOLIO.with_suffix(".json.bak")
        bak.write_bytes(PORTFOLIO.read_bytes())
    tmp = PORTFOLIO.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PORTFOLIO)


def get_holding(data, code):
    code = normalize_code(code)
    h = data["holdings"].get(code)
    if h is None:
        die(f"持仓文件中没有 {code}，请先在对话中确认总资金与持仓成本后 upsert。")
    return code, h


# ---------------------------------------------------------------- 命令
def cmd_path(a):
    out({"ok": True, "data_dir": str(DATA_DIR), "portfolio": str(PORTFOLIO), "analysis_log": str(LOG),
         "reports": str(REPORTS), "portfolio_exists": PORTFOLIO.exists()})


def cmd_show(a):
    data = load()
    ra, rg = regime_of(data)
    if a.code:
        code, h = get_holding(data, a.code)
        out({"regret_aversion": ra, "code": code, "holding": h})
        return
    if a.json:
        out(data)
        return
    print(f"持仓文件：{PORTFOLIO}  更新于 {data.get('updated_at')}")
    pf = data.get("prefs") or {}
    print(f"投资者画像：踏空厌恶={ra}（底仓门槛={rg['tier1_zone']}，时间换价格={'开' if rg['time_entry'] else '关'}）"
          f"｜承受浮亏≤{float(pf.get('drawdown_tolerance', 0.25)) * 100:.0f}%")
    if pf.get("profile_note"):
        print(f"  画像备注：{pf['profile_note']}")
    if not data["holdings"]:
        print("（持仓为空）")
        return
    print("| 代码 | 名称 | 市场 | 总资金 | 持股数 | 持仓成本 | 成本金额 | 已投入比例 | 可用资金 | 更新时间 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for code, h in data["holdings"].items():
        basis = h.get("shares", 0) * h.get("cost", 0)
        budget = h.get("budget", 0) or 0
        ratio = f"{basis / budget:.1%}" if budget else "-"
        print(f"| {code} | {h.get('name','')} | {h.get('market','')} | {budget:,.2f} | {h.get('shares',0)} | "
              f"{h.get('cost',0):.4f} | {basis:,.2f} | {ratio} | {budget - basis:,.2f} | {h.get('updated_at','')} |")


def cmd_upsert(a):
    """建档或更新持仓：只写用户提供的真值（持股数/成本价照抄券商），不做任何推算。"""
    data = load()
    code = normalize_code(a.code)
    h = data["holdings"].get(code, {})
    before = {"shares": h.get("shares"), "cost": h.get("cost"), "budget": h.get("budget")}
    mkt = a.market or h.get("market") or market_of(code)
    h["market"] = mkt
    if a.name is not None:
        h["name"] = a.name
    if a.budget is not None:
        if a.budget <= 0:
            die("budget 必须大于 0")
        h["budget"] = float(a.budget)
    if a.shares is not None:
        if a.shares < 0:
            die("持股数不能为负")
        h["shares"] = int(a.shares)
    if a.cost is not None:
        if a.cost < 0:
            die("成本价不能为负")
        h["cost"] = float(a.cost)
    if a.lot_size is not None:
        h["lot_size"] = int(a.lot_size)
    elif "lot_size" not in h and default_lot(mkt):
        h["lot_size"] = default_lot(mkt)
    if a.currency is not None:
        h["currency"] = a.currency
    elif "currency" not in h:
        h["currency"] = {"A": "CNY", "HK": "HKD", "US": "USD"}[mkt]
    h.setdefault("shares", 0)
    h.setdefault("cost", 0.0)
    if int(h["shares"]) > 0 and float(h["cost"] or 0) <= 0:
        die("持股大于 0 时成本价必须大于 0；请照抄券商显示的『成本价』")

    if a.swing_shares is not None:
        h["swing_shares"] = int(a.swing_shares)
        h.pop("core_shares", None)
        h["split_configured"] = True
    if a.core_shares is not None:
        h["core_shares"] = int(a.core_shares)
        h.pop("swing_shares", None)
        h["split_configured"] = True
    if not (0 <= h.get("swing_shares", 0) <= h["shares"]):
        die(f"swing_shares 必须在 0–{h['shares']} 之间（不能超过持股数）")
    if not (0 <= h.get("core_shares", h["shares"]) <= h["shares"]):
        die(f"core_shares 必须在 0–{h['shares']} 之间")

    missing = [k for k in ("name", "budget") if k not in h]
    if mkt == "HK" and not h.get("lot_size"):
        missing.append("lot_size(港股每手股数)")
    h["updated_at"] = now()
    data["holdings"][code] = h
    core, swing = normalize_split(h)
    basis = int(h["shares"]) * float(h.get("cost", 0) or 0)
    budget = float(h.get("budget", 0) or 0)
    save(data)
    warn = []
    if budget and basis > budget * 1.0001:
        warn.append("持仓成本 %.2f 已超过该标的 budget %.2f（超支 %.2f）："
                    "请确认是 budget 设小了，还是挪用了其他标的的资金"
                    % (basis, budget, basis - budget))
    out({"ok": True, "code": code, "holding": h, "missing_fields": missing, "warnings": warn,
         "source": "用户提供的真值（未做任何推算）",
         "before": before,
         "after": {"shares": int(h["shares"]), "cost": round(float(h.get("cost", 0) or 0), 4),
                   "cost_basis": round(basis, 2),
                   "invested_ratio_pct": round(basis / budget * 100, 2) if budget else None,
                   "available_cash": round(budget - basis, 2) if budget else None,
                   "core_shares": core, "swing_shares": swing}})


def normalize_split(h):
    """底仓股数为锚，波段仓 = 总持股 − 底仓（自动派生）。
    兼容旧数据：只写了 swing_shares 时反推 core_shares。"""
    def _i(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d
    total = max(0, _i(h.get("shares", 0)))
    if "core_shares" in h and h.get("core_shares") is not None:
        core = max(0, min(_i(h["core_shares"]), total))
    elif "swing_shares" in h and h.get("swing_shares") is not None:
        core = max(0, total - _i(h["swing_shares"]))
    else:
        core = total                      # 未划分 → 全部视为底仓
    h["core_shares"] = core
    h["swing_shares"] = total - core
    if not h.get("split_configured") and (h.get("profile") or {}).get("split_set_on"):
        h["split_configured"] = True     # 兼容：修复前设置过的持仓
    return core, total - core


def normalize_type(h):
    """把公司性质统一成 {primary, valuation_model, tags, label, basis, counter_evidence, confirmed}。"""
    t = h.get("company_type")
    if isinstance(t, dict):
        prim = t.get("primary")
        if prim in TYPE_ALIAS:            # 旧 key 迁移
            prim = TYPE_ALIAS[prim]
        if prim not in TYPE_ENUM:
            prim = None
        tags = []
        for x in (t.get("tags") or []):
            if x in TAG_ENUM:
                keep = x
            elif x in TYPE_ENUM:
                keep = x
            else:
                keep = TYPE_ALIAS.get(x)          # 旧 primary key 当标签用的情形
            if keep and keep != prim and keep not in tags:
                tags.append(keep)
        t.update({"primary": prim, "tags": tags})
        t.setdefault("confirmed", False)
        t.setdefault("basis", "")
        t.setdefault("counter_evidence", "")
        if prim:
            t["label"] = TYPE_ENUM[prim]
            t.setdefault("valuation_model", MODEL_DEFAULT[prim])
        if t.get("valuation_model") not in MODEL_ENUM:
            t["valuation_model"] = MODEL_DEFAULT.get(prim)
        h["company_type"] = t
        return t
    if isinstance(t, str) and t.strip():
        prim = None
        for key, kws in TYPE_KEYWORDS:
            if any(k in t for k in kws):
                prim = key
                break
        h["company_type"] = {"primary": prim, "tags": [], "label": t, "confirmed": False,
                             "basis": "由旧字段自动映射，待确认",
                             "counter_evidence": "",
                             "valuation_model": MODEL_DEFAULT.get(prim)}
        return h["company_type"]
    h["company_type"] = {"primary": None, "tags": [], "confirmed": False, "basis": "",
                         "counter_evidence": "", "valuation_model": None}
    return h["company_type"]


def normalize_drivers(h):
    d = h.get("drivers")
    if not isinstance(d, dict):
        d = {"industry": "", "note": "", "items": []}
    d.setdefault("items", [])
    d["items"] = [x for x in d["items"] if isinstance(x, dict) and x.get("name")]
    for x in d["items"]:
        x.setdefault("category", "industry")
    h["drivers"] = d
    return d


def summarize_drivers(d, today=None):
    """返回因子摘要：核心因子、滞后未更新的因子、权重最高的三条。"""
    today = today or datetime.now().date()
    items = d.get("items") or []
    stale, core, missing = [], [], []
    for it in items:
        lv = it.get("level", "secondary")
        if lv == "core":
            core.append(it.get("name"))
        if lv == "watch":      # watch 级不参与滞后判定
            continue
        freq = it.get("freq")
        if freq in DRIVER_FREQ_DAYS:
            last = it.get("latest_date")   # 只认读数日期，不认"登记过定义"
            if not last:
                missing.append(it.get("name"))
            else:
                try:
                    y, m, dd = [int(x) for x in str(last)[:10].split("-")]
                    age = (today - datetime(y, m, dd).date()).days
                    if age > DRIVER_FREQ_DAYS[freq]:
                        stale.append({"name": it.get("name"), "days": age, "freq": freq})
                except ValueError:
                    missing.append(it.get("name"))
    policy, expired = [], []
    today_d = today
    for it in items:
        if it.get("category") != "policy":
            continue
        hm = it.get("horizon_months")
        base = it.get("latest_date")
        exp = None
        if base and hm:
            try:
                y, m, dd = [int(x) for x in str(base)[:10].split("-")]
                exp = (datetime(y, m, dd) + timedelta(days=int(hm) * 30)).date()
            except ValueError:
                exp = None
        entry = {"name": it.get("name"), "direction": it.get("direction"),
                 "channel": it.get("channel"), "horizon_months": hm,
                 "expires_on": exp.isoformat() if exp else None,
                 "verifiable": it.get("verifiable"),
                 "latest_value": it.get("latest_value"), "latest_date": base}
        if exp and today_d > exp:
            entry["expired"] = True
            expired.append(entry)
        else:
            entry["expired"] = False
            policy.append(entry)
    rate = []
    for it in items:
        if it.get("category") != "rate":
            continue
        rate.append({"name": it.get("name"), "level": it.get("level"),
                     "beta_rate": it.get("beta_rate"), "channel": it.get("channel"),
                     "latest_value": it.get("latest_value"),
                     "latest_date": it.get("latest_date")})
    return {"industry": d.get("industry", ""), "note": d.get("note", ""),
            "count": len(items), "core": core,
            "stale": stale, "never_updated": missing,
            "policy_active": policy, "policy_expired": expired,
            "rate_factors": rate,
            "top": sorted(items, key=lambda x: -(x.get("weight") or 0))[:3]}


# ---------------------------------------------------------------- 锚复评触发
REANCHOR_TARGETS = ("deep_value_price", "core_entry_price", "fair_high_price",
                    "overvalued_above_price", "all")
REANCHOR_DIRS = ("up", "down", "review_only")
REANCHOR_OPS = (">=", "<=")


def check_reanchor(v, h, today=None):
    """
    锚复评触发器：因子越过事先写死的阈值 → 强制重跑锚，而不是在旧锚上找买点。

    存在的理由：A/B/C 三条通道全部由价格触发，`core_entry ~ fair_high` 之间没有任何
    买入入口（「买入真空区」）。若主升浪在该区间启动，框架会一动不动。解法不是把买点
    调高（那是追高、没有退出依据），而是让锚先动：因子兑现 → 锚上修 → tier1 自随之上移，
    价格不动、买点就够得着了。这是唯一不违反入场纪律的防踏空机制。

    每条触发器自带 observed / observed_date，不复用 drivers.latest_value：后者是给人读的
    描述文字（含粗体、区间、口径说明），不可计算；触发器要的是数字，两者定位不同。
    """
    today = today or datetime.now().date()
    trig = [x for x in (v.get("reanchor_triggers") or []) if isinstance(x, dict)]
    known = {it.get("name") for it in (h.get("drivers") or {}).get("items") or []}
    triggered, near, stale, due, unmatched, broken = [], [], [], [], [], []

    def _days(d):
        try:
            y, m, dd = [int(x) for x in str(d)[:10].split("-")]
            return (today - datetime(y, m, dd).date()).days
        except (ValueError, TypeError):
            return None

    for t in trig:
        tid = t.get("id") or t.get("desc") or "?"
        if t.get("type") == "event":
            n = _days(t.get("due_date"))
            if n is not None and n >= 0:
                due.append({"id": tid, "desc": t.get("desc"), "due_date": t.get("due_date"),
                            "overdue_days": n, "reanchor": t.get("reanchor", "all")})
            continue
        rows, ok = [], True
        for c in (t.get("all_of") or [t]):
            name = c.get("driver")
            if name not in known:
                unmatched.append({"id": tid, "driver": name})
                ok = False
                continue
            try:
                obs = float(c.get("observed"))
            except (TypeError, ValueError):
                broken.append({"id": tid, "driver": name,
                               "why": "observed 缺失或非数值：先 --observe 回填读数"})
                ok = False
                continue
            op = c.get("op", ">=")
            try:
                val = float(c.get("value"))
            except (TypeError, ValueError):
                broken.append({"id": tid, "driver": name, "why": "value 非数值"})
                ok = False
                continue
            passed = (obs >= val - 1e-9) if op == ">=" else (obs <= val + 1e-9)
            dist = abs(obs - val) / abs(val) if val else 0.0
            rows.append({"driver": name, "observed": obs, "op": op, "value": val,
                         "unit": c.get("unit", ""), "pass": passed,
                         "distance_pct": round(dist * 100, 1)})
            if not passed:
                ok = False
            age = _days(c.get("observed_date"))
            if age is not None and age > 30:
                stale.append({"id": tid, "driver": name, "age_days": age})
        item = {"id": tid, "desc": t.get("desc"), "reanchor": t.get("reanchor", "all"),
                "direction": t.get("direction", "review_only"), "basis": t.get("basis"),
                "metrics": rows}
        if ok and rows:
            triggered.append(item)
        elif rows:
            gap = max(m["distance_pct"] for m in rows)   # 多条件须全部逼近才算 near
            if gap <= 10:
                item["closest_distance_pct"] = gap
                near.append(item)

    if triggered:
        verdict = ("锚复评已触发 %d 条：必须先重跑估值锚（option + set-thesis），"
                   "任何沿用旧锚算出的买点自动失效；重跑后 tier1 会随锚上移。" % len(triggered))
    elif due:
        verdict = "有 %d 个事件型触发器已到期：先重评锚，再谈买点。" % len(due)
    elif near:
        verdict = "暂无触发；%d 条接近阈值（10%% 内），继续盯读数。" % len(near)
    else:
        verdict = "未触发。"
    if unmatched:
        verdict += " 另有 %d 条触发器引用了未登记的因子名，等于没设防。" % len(unmatched)
    return {
        "configured": len(trig), "triggered": triggered, "near": near,
        "event_due": due, "observation_stale": stale,
        "unmatched": unmatched, "invalid": broken, "verdict": verdict,
        "rule": ("只有 leading 因子可当触发器，lagging 只能确认；阈值必须能追溯到因子读数、"
                 "不许用价格本身；触发后的动作是改锚（重算 fair_high），不是改买点。"),
    }


def cmd_set_reanchor(a):
    """写入/更新锚复评触发器。刻意不动 valuation.revised_at —— 设触发器不是改锚，
    改了会让「锚版本」失真，后续报告无法判断哪份用了最新锚。"""
    data = load()
    code, h = get_holding(data, a.code)
    v = h.setdefault("valuation", {})
    cur = [x for x in (v.get("reanchor_triggers") or []) if isinstance(x, dict)]
    if a.clear:
        cur = []
    if a.file:
        with open(a.file, "r", encoding="utf-8") as f:
            patch = json.load(f)
        items = patch if isinstance(patch, list) else (patch.get("triggers") or [])
        by_id = {x.get("id"): x for x in cur}
        for it in items:
            if not isinstance(it, dict):
                die("触发器必须是对象数组")
            if it.get("type") != "event":
                for c in (it.get("all_of") or [it]):
                    if c.get("op", ">=") not in REANCHOR_OPS:
                        die("op 只支持 >= 或 <=，收到 %r" % c.get("op"))
                tgt = it.get("reanchor", "all")
                if tgt not in REANCHOR_TARGETS:
                    die("reanchor 只能是 %s，收到 %r" % (list(REANCHOR_TARGETS), tgt))
                if it.get("direction", "review_only") not in REANCHOR_DIRS:
                    die("direction 只能是 %s，收到 %r"
                        % (list(REANCHOR_DIRS), it.get("direction")))
            by_id[it.get("id") or it.get("desc")] = it
        cur = list(by_id.values())
    today = datetime.now().strftime("%Y-%m-%d")
    for spec in (a.observe or []):
        if "=" not in spec:
            die("--observe 格式为 id=数值（多条件触发器写成 id:因子名=数值），收到 %r" % spec)
        key, _, raw = spec.partition("=")
        key = key.strip()
        tid, _, drv = key.partition(":")
        tid, drv = tid.strip(), (drv.strip() or None)
        hit = None
        for it in cur:
            if (it.get("id") or it.get("desc")) == tid:
                hit = it
                break
        if hit is None:
            die("找不到触发器 id=%s" % tid)
        try:
            num = float(raw)
        except ValueError:
            die("--observe 的数值非法：%r" % raw)
        conds = hit.get("all_of") or [hit]
        if drv is None:
            if len(conds) > 1:
                die("触发器 %s 有 %d 个条件，--observe 须写成 id:因子名=数值 逐条回填"
                    % (tid, len(conds)))
            conds[0]["observed"] = num
            conds[0]["observed_date"] = today
        else:
            matched = [c for c in conds if c.get("driver") == drv]
            if not matched:
                die("触发器 %s 没有名为「%s」的条件" % (tid, drv))
            for c in matched:
                c["observed"] = num
                c["observed_date"] = today   # 只认读数日期，与 drivers.latest_date 同口径
    v["reanchor_triggers"] = cur
    h["updated_at"] = now()
    save(data)
    out({"ok": True, "code": code, "count": len(cur),
         "triggers": cur, "check": check_reanchor(v, h)})


def cmd_set_drivers(a):
    data = load()
    code, h = get_holding(data, a.code)
    with open(a.file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, list):
        payload = {"items": payload}
    items = payload.get("items") or payload.get("drivers") or []
    if not items:
        die("drivers 文件里没有因子列表（顶层用 items 或 drivers 都可以）")
    d = normalize_drivers(h)
    if payload.get("industry"):
        d["industry"] = payload["industry"]
    if payload.get("note"):
        d["note"] = payload["note"]

    by_name = {x.get("name"): x for x in d["items"]}
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            die("每个因子必须有 name")
        lv = it.get("level", "secondary")
        if lv not in DRIVER_LEVELS:
            die(f"{name} 的 level 必须是 {DRIVER_LEVELS} 之一")
        if it.get("lead") and it["lead"] not in DRIVER_LEADS:
            die(f"{name} 的 lead 必须是 {DRIVER_LEADS} 之一")
        if it.get("freq") and it["freq"] not in ("daily", "weekly", "monthly", "quarterly", "event"):
            die(f"{name} 的 freq 不合法")
        if not it.get("source"):
            die(f"{name} 未写数据来源 source（取数时会不知道去哪里找）")
        cat = it.get("category", (by_name.get(name) or {}).get("category", "industry"))
        if cat not in DRIVER_CATEGORIES:
            die(f"{name} 的 category 必须是 {DRIVER_CATEGORIES} 之一")
        if cat == "policy":
            # 政策/政治因子必须可证伪、有传导链、有窗口，否则就是"叙事"不是"因子"
            if (it.get("freq") or "") != "event":
                die(f"{name} 是政策因子，freq 必须是 event（政策是事件，不是连续变量）")
            if (it.get("lead") or "leading") != "leading":
                die(f"{name} 是政策因子，lead 必须是 leading（政策先于业绩，不能用来确认）")
            if not it.get("channel"):
                die(f"{name} 是政策因子，必须写 channel：政策 → 哪个变量 → 哪条财务科目。"
                    f"写不出传导链，说明这只是情绪，不是因子")
            if it.get("direction") not in POLICY_DIRECTIONS:
                die(f"{name} 是政策因子，direction 必须是 {POLICY_DIRECTIONS} 之一")
            if not it.get("horizon_months"):
                die(f"{name} 是政策因子，必须给 horizon_months（影响窗口，单位月）")
            hm = int(it["horizon_months"])
            if not (1 <= hm <= POLICY_MAX_MONTHS):
                die(f"{name} 的 horizon_months 应在 1–{POLICY_MAX_MONTHS} 之间；"
                    f"超过 {POLICY_MAX_MONTHS} 个月的影响无法验证，不要写进因子")
            if not it.get("verifiable"):
                die(f"{name} 是政策因子，必须写 verifiable：后续用什么数据确认影响已发生/未发生")
            if it.get("level") == "core":
                die(f"{name} 是政策因子，level 不能是 core（政策不确定性只能压低买价，"
                    f"不能作为核心盈利驱动）")
        if cat == "rate":
            # 折现率因子（10Y 美债等）：不改盈利、只改倍数，因此必须可追溯（基准 + 敏感度）
            if (it.get("freq") or "") not in ("daily", "weekly"):
                die(f"{name} 是利率因子，freq 必须是 daily 或 weekly（利率是连续市场变量，不是事件）")
            if (it.get("lead") or "leading") != "leading":
                die(f"{name} 是利率因子，lead 必须是 leading")
            if it.get("level") == "core":
                die(f"{name} 是利率因子，level 不能是 core（它不改变盈利预测，只改变合理倍数；"
                    f"真正的核心盈利驱动请另设 industry 因子）")
            if not it.get("channel"):
                die(f"{name} 是利率因子，必须写 channel：利率 → 折现率/ERP → 哪个估值参数"
                    f"（如 base_low 27.00→25.80）。写不出传导链就不是因子")
            try:
                _br = float(it.get("beta_rate"))
            except (TypeError, ValueError):
                die(f"{name} 是利率因子，必须给 beta_rate（10Y 每上行 100bp，合理估值中枢的变动比例，"
                    f"如 -0.09；银行/保险等方向不确定的形态记 0）。缺了它无法做倍数修正")
            if not (-0.30 <= _br <= 0.0):
                die(f"{name} 的 beta_rate 应在 -0.30 ~ 0 之间（利率上行 → 倍数下修）；"
                    f"方向不确定的形态记 0，不要给正值")
        merged = dict(by_name.get(name) or {})
        merged.update(it)
        merged["name"] = name
        merged.setdefault("level", "secondary")
        merged["last_checked"] = datetime.now().strftime("%Y-%m-%d")
        by_name[name] = merged
    d["items"] = list(by_name.values())
    h["updated_at"] = now()
    save(data)
    out({"ok": True, "code": code, "drivers": summarize_drivers(d),
         "hint": "早盘任务须先查 core 因子是否更新，再定预案；latest_date 用于计算滞后"})


def resolve_profile(data, h):
    """标的画像优先，缺省回退全局画像。返回生效参数与来源。"""
    g = data.get("prefs") or {}
    p = h.get("profile") or {}
    style = p.get("entry_style") or g.get("entry_style") or "mixed"
    if style not in STYLE_ENUM:
        style = "mixed"
    ratio = p.get("entry_zone_ratio")
    src_ratio = "holding"
    if ratio is None:
        ratio = g.get("entry_zone_ratio")
        src_ratio = "global"
    if ratio is None:
        ratio = STYLE_RATIO[style]
        src_ratio = "style-default"
    try:
        ratio = max(0.0, min(1.0, float(ratio)))
    except (TypeError, ValueError):
        ratio = STYLE_RATIO[style]
    core, swing = normalize_split(h)
    try:
        tol = float(g.get("drawdown_tolerance", 0.25))
    except (TypeError, ValueError):
        tol = 0.25
    return {"regret_aversion": g.get("regret_aversion", "mid"),
            "drawdown_tolerance": tol,
            "entry_style": style, "entry_style_label": STYLE_ENUM[style],
            "entry_zone_ratio": ratio,
            "core_shares": core, "swing_shares": swing,
            "source": {"entry_style": "holding" if p.get("entry_style") else "global",
                       "entry_zone_ratio": src_ratio},
            "note": p.get("note", ""),
            "rules": STYLE_NOTE.get(style, [])}


def cmd_set_profile(a):
    """设置标的级画像：这只票用什么执行策略（与全局人格参数分开）。"""
    data = load()
    code, h = get_holding(data, a.code)
    p = h.setdefault("profile", {})
    if a.entry_style:
        if a.entry_style not in STYLE_ENUM:
            die(f"entry_style 必须是 {sorted(STYLE_ENUM)} 之一")
        p["entry_style"] = a.entry_style
        if a.entry_zone_ratio is None:
            p["entry_zone_ratio"] = STYLE_RATIO[a.entry_style]
    if a.entry_zone_ratio is not None:
        if not (0 <= a.entry_zone_ratio <= 1):
            die("entry_zone_ratio 应在 0–1 之间")
        p["entry_zone_ratio"] = a.entry_zone_ratio
    total = int(h.get("shares", 0))
    if a.core_shares is not None:
        if not (0 <= a.core_shares <= total):
            die(f"底仓股数必须在 0–{total} 之间")
        h["core_shares"] = int(a.core_shares)
        h.pop("swing_shares", None)
        h["split_configured"] = True
        p["split_set_on"] = datetime.now().strftime("%Y-%m-%d")
    if a.swing_shares is not None:
        if not (0 <= a.swing_shares <= total):
            die(f"波段仓必须在 0–{total} 股之间")
        h["swing_shares"] = int(a.swing_shares)
        h.pop("core_shares", None)
        h["split_configured"] = True
        p["split_set_on"] = datetime.now().strftime("%Y-%m-%d")
    if a.note:
        p["note"] = a.note
    if getattr(a, "swing_quality_override", None):
        p["swing_quality_override"] = (a.swing_quality_override == "on")
        p["swing_quality_override_set_on"] = datetime.now().strftime("%Y-%m-%d")
    p["set_on"] = datetime.now().strftime("%Y-%m-%d")
    h["updated_at"] = now()
    save(data)
    out({"ok": True, "code": code, "profile": h.get("profile"),
         "resolved": resolve_profile(data, h),
         "hint": "全局画像（踏空厌恶/承受浮亏）用 set-prefs；标的画像（风格/窗口/波段仓）用本命令"})


BAND_BY_TYPE = {"cyclical": 0.30, "slow_grower": 0.25, "stalwart": 0.15,
                "fast_grower": 0.20, "turnaround": 0.10, "asset_play": 0.10}


def suggest_swing_ratio(data, h):
    """按形态、质地、踏空厌恶给一个建议波段比例，并列出理由。用户可采纳也可覆盖。"""
    ct = normalize_type(h)
    prof = resolve_profile(data, h)
    prim = ct.get("primary") or "stalwart"
    base = BAND_BY_TYPE.get(prim, 0.15)
    reasons = ["形态 %s：基准 %.0f%%（波动越大越有波段空间）" % (TYPE_ENUM.get(prim, prim), base * 100)]

    q = h.get("quality_score")
    if q is None:
        qf, qtxt = 1.0, "质地未评分：不调整"
    else:
        q = float(q)
        if q >= 9:
            qf, qtxt = 0.8, "质地 %.0f 很高：×0.8（好公司少折腾，卖飞了很难买回）" % q
        elif q >= 8:
            qf, qtxt = 1.0, "质地 %.0f：×1.0" % q
        elif q >= 7:
            qf, qtxt = 0.8, "质地 %.0f 偏低：×0.8" % q
        else:
            qf, qtxt = 0.0, "质地 %.0f <7：不建议设波段仓（不确定性大，容易卖飞变踏空）" % q
    reasons.append(qtxt)

    ra = prof["regret_aversion"]
    rf = {"high": 0.8, "mid": 1.0, "low": 1.2}.get(ra, 1.0)
    reasons.append("踏空厌恶 %s：×%.1f（越怕踏空，波段仓应越小）" % (ra, rf))

    ratio = 0.0 if qf == 0 else max(0.0, min(0.35, base * qf * rf))
    reasons.append("建议上限 35%：这只是**软建议**，不是硬上限。分桶只需指定底仓股数，"
                   "波段仓 = 总持仓 − 底仓自动派生，不存在比例约束")
    return round(ratio, 2), reasons


def cmd_plan_swing(a):
    """按可用资金算出"最多能买多少股"，再按比例划分底仓/波段仓（结果取整到每手）。"""
    data = load()
    code, h = get_holding(data, a.code)
    price = float(a.price)
    if price <= 0:
        die("价格必须大于 0")
    lot = int(h.get("lot_size") or 1)
    budget = float(a.budget) if a.budget is not None else float(h.get("budget", 0) or 0)
    held = int(h.get("shares", 0))
    cost = float(h.get("cost", 0) or 0)
    basis = held * cost
    avail = max(0.0, budget - basis)
    max_shares = lots_floor(avail, price, lot)
    sug, sug_reasons = suggest_swing_ratio(data, h)
    if a.ratio is None:
        ratio = sug
        ratio_src = "采用建议值"
    else:
        ratio = float(a.ratio)
        ratio_src = "用户指定（建议值为 %.0f%%）" % (sug * 100)
    if not (0 <= ratio <= 1):
        die("ratio 应在 0–1 之间（0.3 表示 30%）")

    if held > 0:
        # 已有持仓：现有部分即底仓基础，波段仓由后续用现金买入自动形成
        core_shares = held
        swing_shares = 0
        planned_total = held + max_shares
        advice = ("已有 %d 股持仓：现有部分按底仓处理，后续用剩余 %.2f 元买入的部分自动计入波段仓，"
                  "无需按比例划分" % (held, avail))
    else:
        swing_shares = int(max_shares * ratio) // lot * lot
        core_shares = max_shares - swing_shares
        planned_total = max_shares
        advice = ("空仓：按现价 %.2f 与可用 %.2f 元，最多买 %d 股；划 %.0f%% 做波段 → "
                  "底仓 %d 股、波段仓 %d 股" % (price, avail, max_shares, ratio * 100,
                                            core_shares, swing_shares))

    out({"ok": True, "code": code, "price": price, "lot": lot,
         "budget": budget, "held_shares": held, "available_cash": round(avail, 2),
         "max_buy_shares": max_shares,
         "ratio": ratio,
         "planned_total_shares": planned_total,
         "core_shares": core_shares, "swing_shares": swing_shares,
         "swing_cash_est": round(swing_shares * price, 2),
         "advice": advice,
         "suggestion": {"ratio": sug, "ratio_pct": round(sug * 100, 1),
                        "shares_if_empty": int(max_shares * sug) // lot * lot if held == 0 else None,
                        "reasons": sug_reasons,
                        "ratio_source": ratio_src,
                        "note": "建议值仅供参考，采纳与否由你决定；覆盖时用 --ratio 显式指定"},
         "next_commands": [
             "买入成交后：upsert --shares %d --cost <券商成本>" % planned_total,
             "然后：set-profile --code %s --core-shares %d" % (code, core_shares),
         ],
         "caveat": "按当前价与当前可用资金估算的上限；成交价不同需重算，实际股数以券商成交回报为准"})


def cmd_set_type(a):
    data = load()
    code, h = get_holding(data, a.code)
    prim = a.primary
    if prim not in TYPE_ENUM:
        die(f"primary 必须是 {sorted(TYPE_ENUM)} 之一")
    old_prim = (h.get("company_type") or {}).get("primary")
    if old_prim in TYPE_ENUM and old_prim != prim and len(a.reason or "") < 6:
        die("改判形态必须给出结构性理由（--reason）：增速连续 8 个季度跨越门槛 / 主业剥离 / "
            "重整完成 / 注入落地。单季数据波动或股价涨跌不算理由")
    tags = []
    for t in (a.tags or "").split(","):
        t = t.strip()
        if not t:
            continue
        if t not in TAG_ENUM and t not in TYPE_ENUM:
            die(f"tags 含未知标签：{t}；属性标签可选 {sorted(TAG_ENUM)}，也可填形态 key")
        if t != prim and t not in tags:
            tags.append(t)
    model = a.model or MODEL_DEFAULT[prim]
    if model not in MODEL_ENUM:
        die(f"model 必须是 {sorted(MODEL_ENUM)} 之一")
    if len(a.basis or "") < 6:
        die("必须给出判定依据（至少两条量化证据：毛利率振幅、营收 CAGR、净利峰谷比、股息率、ROE 序列等）")
    if len(a.counter or "") < 4:
        die("必须给出一条反方证据（--counter）：支持相反分类的证据，写不出说明判定太随意")

    h["company_type"] = {
        "primary": prim,
        "tags": tags,
        "label": TYPE_ENUM[prim],
        "valuation_model": model,
        "basis": a.basis,
        "counter_evidence": a.counter,
        "user_view": a.user_view or "",
        "confirmed": bool(a.confirmed),
        "set_on": datetime.now().strftime("%Y-%m-%d"),
    }
    if a.reason:
        h["company_type"]["reason"] = a.reason
        h["company_type"].setdefault("history", []).append(
            {"date": datetime.now().strftime("%Y-%m-%d"), "from": old_prim,
             "to": prim, "reason": a.reason})
    h["updated_at"] = now()
    save(data)
    warn = []
    if not a.confirmed:
        warn.append("未标记为用户确认（confirmed=false），报告中须注明「性质未确认（自动判定）」")
    if a.user_view:
        warn.append("已记录用户的不同看法；按数据结论执行，并提高安全边际一档")
    if model != MODEL_DEFAULT[prim]:
        warn.append(f"估值范式 {model} 偏离该形态的默认值 {MODEL_DEFAULT[prim]}，请在报告中说明理由")
    out({"ok": True, "code": code, "company_type": h["company_type"],
         "model_label": MODEL_ENUM[model], "model_is_default": model == MODEL_DEFAULT[prim],
         "type_focus": TYPE_FOCUS[prim],
         "type_break_signal": TYPE_BREAK[prim],
         "warnings": warn})


def parse_window_months(text):
    """从 '12–18 个月' / '18–36 个月' / '2 年' 之类文本里取最大月数。"""
    if not text:
        return None
    t = str(text)
    m = re.findall(r'(\d+)\s*[–\-~至到]\s*(\d+)\s*个月', t)
    if m:
        return max(int(a) for _, a in m)
    months = [int(x) for x in re.findall(r'(\d+)\s*个月', t)]
    years = [int(x) * 12 for x in re.findall(r'(\d+)\s*年', t)]
    allm = months + years
    if allm:
        return max(allm)
    nums = [int(x) for x in re.findall(r'\d+', t)]
    return max(nums) if nums else None


def option_deadline(it, fallback_from):
    """返回 (deadline_date|None, 原始口径说明)。priority: 显式 deadline > window 解析。"""
    d = it.get("deadline")
    if d:
        try:
            y, m, dd = [int(x) for x in str(d)[:10].split("-")]
            return datetime(y, m, dd).date(), "显式 deadline"
        except ValueError:
            pass
    months = parse_window_months(it.get("window"))
    if not months:
        return None, "无法解析窗口"
    base = it.get("set_on") or fallback_from
    try:
        y, m, dd = [int(x) for x in str(base)[:10].split("-")]
        return (datetime(y, m, dd) + timedelta(days=months * 30)).date(), "window 解析 %d 个月" % months
    except ValueError:
        return None, "基准日缺失"


def compute_option(v, today=None):
    """
    上行期权定价：每股期权价值 = Σ(情形每股价值 × 生效概率 × 折扣)
    时效处理：
      · status=missed → 计入 0
      · status=hit    → 仍按登记 prob 计入，但标记 realized，提示复核基础价值后剔除
      · 逾期未兑现   → 逾期 ≤180 天 prob ×0.5；>180 天 prob ×0（视为证伪，保留条目待确认）
    折扣默认 0.85；期权合计超过基础价值中枢 30% 的部分只按 50% 计入。
    """
    today = today or datetime.now().date()
    ov = v.get("option_value") or {}
    items = ov.get("items") or []
    fallback_from = ov.get("set_on") or datetime.now().strftime("%Y-%m-%d")
    total, detail, due = 0.0, [], []
    for it in items:
        try:
            payoff = float(it.get("payoff_per_share") or 0)
            prob = float(it.get("prob") or 0)
            disc = float(it.get("discount") if it.get("discount") is not None else 0.85)
        except (TypeError, ValueError):
            die(f"期权条目数值非法：{it}")
        if not (0 <= prob <= 1):
            die(f"期权概率必须在 0–1 之间：{it.get('name')} prob={prob}")

        status = (it.get("status") or "open").lower()
        eff, flag, dl, dl_src = prob, "", None, ""
        if status == "missed":
            eff, flag = 0.0, "已证伪，不计入"
        elif status == "hit":
            flag = "已兑现：请复核基础价值并把该条从期权中剔除"
        else:
            dl, dl_src = option_deadline(it, fallback_from)
            if dl and today > dl:
                overdue = (today - dl).days
                if overdue > 180:
                    eff, flag = 0.0, "逾期 %d 天未兑现，按证伪处理" % overdue
                else:
                    eff, flag = prob * 0.5, "逾期 %d 天未兑现，概率折半" % overdue
                due.append({"name": it.get("name", ""), "deadline": dl.isoformat(),
                            "overdue_days": overdue, "prob_registered": prob,
                            "prob_effective": round(eff, 4), "note": flag})

        contrib = payoff * eff * disc
        total += contrib
        detail.append({"name": it.get("name", ""), "trigger": it.get("trigger", ""),
                       "window": it.get("window", ""), "deadline": dl.isoformat() if dl else None,
                       "deadline_basis": dl_src, "status": status,
                       "payoff_per_share": payoff, "prob": prob, "prob_effective": round(eff, 4),
                       "discount": disc, "contribution": round(contrib, 4), "flag": flag})

    base_mid = float(v.get("base_mid_price") or 0)
    capped, cap_applied, cap_base = total, False, None
    if base_mid > 0:
        cap_base = 0.30 * base_mid
        if total > cap_base:
            capped = cap_base + (total - cap_base) * 0.5
            cap_applied = True
    return {"items": detail,
            "total_per_share": round(total, 4),
            "capped_per_share": round(capped, 4),
            "cap_base": round(cap_base, 4) if cap_base else None,
            "cap_applied": cap_applied,
            "pct_of_base_mid": round(capped / base_mid * 100, 2) if base_mid else None,
            "count": len(items),
            "due": due,
            "due_count": len(due),
            "hits": [d["name"] for d in detail if d["status"] == "hit"],
            "missed": [d["name"] for d in detail if d["status"] == "missed"]}


def cmd_option(a):
    """登记/更新期权清单。默认按 name 合并（便于只改一条的状态），--replace 才全量替换。"""
    data = load()
    code, h = get_holding(data, a.code)
    with open(a.file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items") if isinstance(payload, dict) and "items" in payload else payload
    if not isinstance(items, list):
        die("期权文件应为 [{...}, ...] 或 {\"items\": [...]}")
    v = h.setdefault("valuation", {})
    if payload.get("base_mid_price") if isinstance(payload, dict) else False:
        v["base_mid_price"] = float(payload["base_mid_price"])

    if a.replace:
        merged_items = items
        mode = "全量替换"
    else:
        old = (v.get("option_value") or {}).get("items") or []
        by_name = {x.get("name"): dict(x) for x in old if x.get("name")}
        added, updated = [], []
        for it in items:
            name = (it.get("name") or "").strip()
            if not name:
                die("每个期权必须有 name（合并模式靠 name 匹配）")
            if name not in by_name and it.get("payoff_per_share") is None:
                die(f"合并模式下找不到名为「{name}」的期权。若是想改状态，请核对名称是否与登记时完全一致；"
                    f"确属新增，请一并提供 payoff_per_share / prob / window")
            base = dict(by_name.get(name) or {})
            (updated if name in by_name else added).append(name)
            base.update(it)
            base.setdefault("status", "open")
            base.setdefault("set_on", datetime.now().strftime("%Y-%m-%d"))
            by_name[name] = base
        merged_items = list(by_name.values())
        mode = "按 name 合并（更新 %d 条%s）" % (
            len(updated), ("，新增 %d 条：%s" % (len(added), "、".join(added))) if added else "")

    v["option_value"] = {"items": merged_items}
    computed = compute_option(v)
    v["option_value"].update({k: computed[k] for k in
                              ("total_per_share", "capped_per_share", "cap_base", "cap_applied",
                               "due_count", "due", "hits")})
    v["option_value"]["set_on"] = v["option_value"].get("set_on") or datetime.now().strftime("%Y-%m-%d")
    h["updated_at"] = now()
    save(data)
    warn = []
    pct = computed.get("pct_of_base_mid")
    if pct is not None:
        if pct < 1:
            warn.append(f"期权合计仅占基础中枢 {pct}%：先检查 payoff_per_share 是否漏乘合理倍数"
                        f"（应为 Δ每股盈利 × 合理PE，而非 Δ每股盈利）")
        elif pct > 30 and not computed.get("cap_applied"):
            warn.append(f"期权合计占基础中枢 {pct}% 却未触发 30% 上限，请核对 base_mid_price 是否填错")
    no_dl = [d["name"] for d in computed["items"]
             if not d.get("deadline") and (d.get("status") or "open") == "open"]
    if no_dl:
        warn.append("以下期权的 window 无法解析出到期日，时效机制对它们不生效：%s —— "
                    "请改成「12–18 个月」这类格式，或直接给 deadline" % "、".join(no_dl))
    if computed["due_count"]:
        warn.append("有 %d 条期权已逾期未兑现（概率已自动折减）：%s"
                    % (computed["due_count"], "、".join(x["name"] for x in computed["due"])))
    if computed["hits"]:
        warn.append("已兑现的期权：%s —— 请复核基础价值并把它们从期权中剔除"
                    % "、".join(computed["hits"]))
    out({"ok": True, "code": code, "mode": mode, "option_value": computed, "warnings": warn,
         "hint": "fair_high = 基础上沿 + capped×0.6；overvalued_above = (基础上沿 + capped)×1.15；"
                 "标记状态只需传一条 {name, status: hit|missed|open}，默认按 name 合并"})


def cmd_set_prefs(a):
    data = load()
    prefs = data.setdefault("prefs", {})
    if a.regret_aversion:
        prefs["regret_aversion"] = a.regret_aversion
    if a.drawdown_tolerance is not None:
        if not (0.05 <= a.drawdown_tolerance <= 0.8):
            die("drawdown_tolerance 应在 0.05–0.8 之间（如 0.25 表示可承受 25% 浮亏）")
        prefs["drawdown_tolerance"] = a.drawdown_tolerance
    if a.entry_style:
        if a.entry_style not in STYLE_ENUM:
            die(f"entry_style 必须是 {sorted(STYLE_ENUM)} 之一")
        prefs["entry_style"] = a.entry_style
        if a.entry_zone_ratio is None:          # 未显式给窗口时，跟随风格默认
            prefs["entry_zone_ratio"] = STYLE_RATIO[a.entry_style]
    if a.entry_zone_ratio is not None:
        if not (0 <= a.entry_zone_ratio <= 1):
            die("entry_zone_ratio 应在 0–1 之间（0=只在底仓线建仓，1=整个合理区都可以）")
        prefs["entry_zone_ratio"] = a.entry_zone_ratio
    if a.profile_note is not None:
        prefs["profile_note"] = a.profile_note
    for k, v in DEFAULT_PREFS.items():
        prefs.setdefault(k, v)
    save(data)
    ra = prefs["regret_aversion"]
    st = prefs.get("entry_style", "mixed")
    out({"ok": True, "prefs": prefs, "regime": REGIME[ra],
         "entry_style": {"key": st, "label": STYLE_ENUM.get(st), "rules": STYLE_NOTE.get(st, [])},
         "meaning": {"low": "只在深度低估建仓，接受长期空仓",
                     "mid": "低估区建底仓，不接受合理区建仓",
                     "high": "允许合理区内用「时间换价格」建底仓，避免长期踏空"}[ra]})


def cmd_set_thesis(a):
    data = load()
    code, h = get_holding(data, a.code)
    with open(a.file, "r", encoding="utf-8") as f:
        patch = json.load(f)
    allowed = {"thesis", "company_type", "valuation", "sell_triggers", "watch_metrics",
               "notes", "quality_score", "option_notes"}
    bad = set(patch) - allowed
    if bad:
        die(f"不支持的字段：{sorted(bad)}，允许：{sorted(allowed)}")
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(h.get(k), dict):
            h[k].update(v)
        else:
            h[k] = v
    warn = []
    if "quality_score" in patch:
        try:
            qs = float(patch["quality_score"])
        except (TypeError, ValueError):
            die("quality_score 必须是 0–10 的数字")
        if not (0 <= qs <= 10):
            die("quality_score 必须在 0–10 之间（5 项各 0/1/2 分，满分 10）")
    if "valuation" in patch:
        h["valuation"]["set_on"] = h["valuation"].get("set_on") or datetime.now().strftime("%Y-%m-%d")
        h["valuation"]["revised_at"] = now()      # 锚版本（含时分）：同日多份报告靠它判新旧
        v = h["valuation"]
        if "option_value" in v or (v.get("option_value") or {}).get("items"):
            computed = compute_option(v)
            ov = v.setdefault("option_value", {})
            ov.update({k: computed[k] for k in
                       ("total_per_share", "capped_per_share", "cap_base", "cap_applied")})
        order = ["deep_value_price", "core_entry_price", "fair_high_price", "overvalued_above_price"]
        seq = [(k, v.get(k)) for k in order]
        if all(x[1] for x in seq):
            nums = [float(x[1]) for x in seq]
            if not all(nums[i] < nums[i + 1] for i in range(3)):
                die("估值锚顺序必须满足 deep_value < core_entry < fair_high < overvalued_above，"
                    f"当前为 {dict(seq)}，未保存")
            # 一致性检查：deep_value 与 core_entry×(1−安全边际) 的偏离
            q = h.get("quality_score")
            m = margin_for(q) if q is not None else None
            if m is not None:
                suggested = round(float(v["core_entry_price"]) * (1 - m), 2)
                v["deep_value_suggested"] = suggested
                v["margin_used"] = m
                if abs(float(v["deep_value_price"]) - suggested) / suggested > 0.15:
                    warn.append(f"deep_value_price={v['deep_value_price']} 与 core_entry×(1−{m:.0%})={suggested} "
                                f"偏离 >15%，请确认安全边际是否另有依据")
            # 合理上沿应包含期权（买入侧打折），否则会系统性低估
            ov = v.get("option_value") or {}
            if ov.get("capped_per_share") and v.get("base_high_price"):
                expect = float(v["base_high_price"]) + float(ov["capped_per_share"]) * 0.6
                if abs(float(v["fair_high_price"]) - expect) / expect > 0.08:
                    warn.append(f"fair_high_price={v['fair_high_price']} 与「基础上沿 {v['base_high_price']} + "
                                f"期权×0.6 = {expect:.2f}」不一致，可能漏计上行期权")
    h["updated_at"] = now()
    save(data)
    out({"ok": True, "code": code, "holding": h, "warnings": warn})


def cmd_remove(a):
    data = load()
    code = normalize_code(a.code)
    if code not in data["holdings"]:
        die(f"{code} 不在持仓中")
    data["holdings"].pop(code)
    save(data)
    out({"ok": True, "removed": code})


def cmd_markets(a):
    data = load()
    mk = {}
    for code, h in data["holdings"].items():
        mk.setdefault(h.get("market", market_of(code)), []).append(code)
    out({"ok": True, "markets": mk})


def lots_floor(amount, price, lot):
    if price <= 0 or lot <= 0:
        return 0
    return int(amount // (price * lot)) * lot


def zone_of(v, p):
    """五档估值区。缺锚返回「未设置估值锚」。"""
    if not v:
        return "未设置估值锚"
    deep = v.get("deep_value_price")
    entry = v.get("core_entry_price")
    fh = v.get("fair_high_price")
    over = v.get("overvalued_above_price")
    if deep and p <= float(deep):
        return "深度低估区"
    if entry and p <= float(entry):
        return "低估区"
    if fh and p <= float(fh):
        return "合理区"
    if over and p <= float(over):
        return "偏贵区"
    if over:
        return "透支区"
    return "未设置估值锚"


ZONE_ORDER = ["深度低估区", "低估区", "合理区", "偏贵区", "透支区"]


def matrix_advice(zone, budget, invested, avail):
    """
    把 fundamental-standard「价格位置 × 预算使用度」决策矩阵脚本化。
    矩阵必须脚本化：否则每次都要人脑补「还能加多少」。
    把「低估区 加至 2/3」错套到「合理区」，会算出现实中根本不存在的加仓额度。
    脚本化后报告直接引用 room_amount，可以消除这类口算偏差。
    """
    b = float(budget or 0)
    inv = float(invested or 0)
    inv_pct = (inv / b * 100) if b else 0.0
    third = 33.34
    if zone == "深度低估区":
        cap_pct = 1.0
        action = ("建仓至 1/2–2/3" if inv_pct <= 0
                  else "可加至上限（同时检查有无 A 类信号）")
    elif zone == "低估区":
        cap_pct = 2 / 3
        action = ("建底仓 1/3" if inv_pct <= 0
                  else ("加至 2/3" if inv_pct < 66.67 else "持有，不加"))
    elif zone == "合理区":
        if inv_pct <= 0:
            cap_pct, action = 0.5, "踏空厌恶=high 且价格落在窗口内 → 按 C 通道建底仓；否则等待"
        elif inv_pct <= third:
            cap_pct, action = 1 / 3, "底仓未满 1/3 → 可补至 1/3"
        else:
            cap_pct, action = inv_pct / 100, "持有，不加（底仓已建满，合理区不再加）"
    elif zone == "偏贵区":
        cap_pct = inv_pct / 100 if inv_pct > 0 else 0.0
        action = "不买，记录关注价" if inv_pct <= 0 else "持有，不加"
    elif zone == "透支区":
        cap_pct = inv_pct / 100
        action = ("不买" if inv_pct <= 0
                  else "按减仓纪律处理（减 1/3；已投≥2/3 减至 1/2 预算）")
    else:
        return {"zone": zone, "inv_pct": round(inv_pct, 2), "cap_pct": None,
                "cap_amount": None, "room_amount": None,
                "action": "未设置估值锚：禁止给出买入建议"}
    cap_amt = round(b * cap_pct, 2)
    room = round(max(0.0, cap_amt - inv), 2)
    return {"scope": "base",
            "zone": zone, "inv_pct": round(inv_pct, 2), "cap_pct": round(cap_pct, 4),
            "cap_amount": cap_amt, "room_amount": room,
            "avail_cash": round(float(avail or 0), 2), "action": action,
            "note": "room_amount = 当前价位下**底仓**允许再加的钱；avail_cash = 账户余额。"
                    "两者不同，报告回答『底仓还能加多少』只能用 room_amount。"
                    "**此额度不适用于波段仓**——波段的钱记在 calc.swing 里，两套账不共享。"}


def swing_advice(h, price, avail, lot, fair_high, quality_score):
    """
    波段仓的额度与规则（两套账：底仓管估值锚，波段管买卖价差）。

    为什么必须单独一个函数：
    底仓回答的是「这个价位值不值得新增**长期**持仓」——它必须用估值锚（deep/entry/zone_cap）
    和预算矩阵来管。波段回答的是「这一批筹码过几天要不要出来」——它根本不产生长期敞口，
    用建仓价位去管它，等于要求波段仓也必须是长期仓，那它就不再是波段。
    所以这里**刻意不读** deep_value_price / core_entry_price / zone_cap / matrix.room_amount。

    两条等式：
      ① 仓位只划一次：只指定**底仓股数**，波段仓 = 总持仓 − 底仓，自动派生。
         波段仓没有比例上限——比例既不是钱的口径，也不是风险的口径，
         它只是把波段塞回底仓刻度的尺子。
      ② 钱只有两块：底仓占用的钱 + 波段交易的钱（= 该标的可用资金）。
         波段买入只受后者约束，不参与 1/3、2/3 任何预算刻度。

    波段只认自己剩下的三条：
      ① 质地够（错的标的做 T 只会把错误摊更久；用户可在 profile 显式覆盖这一条）；
      ② 钱 ≤ 该标的可用资金；
      ③ 必须有**两端价格**：先卖后写给买回价，先买后卖给卖出价，价差 ≥5%、卖端 ≤ fair_high。
    """
    core, swing = normalize_split(h)
    q = quality_score
    lsz = int(lot or 100)
    p = float(price or 0)
    av = max(0.0, float(avail or 0))
    prof = h.get("profile") or {}
    override = bool(prof.get("swing_quality_override"))
    by_cash = int(av // (p * lsz)) * lsz if p > 0 else 0
    sellable = swing                      # 先卖后买：只能卖波段仓那部分，底仓永不出让
    can_open = by_cash                    # 先买后卖：规模由钱决定，无股数比例上限
    # 先买后卖还有一道价格闸门：买价 ×1.05 必须 ≤ fair_high，否则「要的卖价」被上限挡住，
    # 这笔交易在规则上根本不可能成立。反推出允许的最高买价 = fair_high ÷ 1.05。
    # 这一条最容易漏：额度够、通道开、5% 价差也算得出来，但卖端上限把它掐死了。
    buy_first_max_price = round(float(fair_high) / 1.05, 4) if fair_high else None
    buy_first_ok_now = bool(buy_first_max_price and p <= buy_first_max_price + 1e-9)
    q_ok = override or (q is not None and q >= 8)
    eligible = bool(fair_high) and q_ok and core > 0
    if not fair_high:
        reason = "未设 fair_high：波段没有卖端上限，禁止"
    elif core <= 0:
        reason = "无底仓：波段以底仓为锚，先建底仓"
    elif not q_ok:
        reason = "质地 %s <8：错的标的做 T 只会把错误摊更久（用户可显式覆盖）" % q
    else:
        if sellable > 0:
            tail = "波段仓 %d 股可先卖后买" % sellable
        elif buy_first_ok_now:
            tail = "波段仓 0 股（持仓全划底仓）→ 只能先买后卖，现价可做"
        else:
            tail = ("波段仓 0 股且先买后卖也做不了：现价 %.2f 高于允许的最高买价 %.2f"
                    "（fair_high %.2f ÷ 1.05），要先跌到 %.2f 才有 5%% 价差空间"
                    % (p, buy_first_max_price or 0, float(fair_high or 0), buy_first_max_price or 0))
        reason = "可用（质地 %s%s）：%s" % (
            q, " 由用户显式覆盖，profile.swing_quality_override=true" if override else "", tail)
    return {
        "scope": "swing",
        "eligible": eligible,
        "reason": reason,
        "entry_gate": None,
        "entry_gate_note": "波段买入**不受** deep/entry/zone_cap 限制；也不受 matrix.room_amount 限制。"
                           "它只在卖出端受 fair_high 约束（卖价 ≤ fair_high），买入端由 5% 价差反推。",
        "core_shares": core,
        "swing_shares": swing,
        "sellable_shares": sellable,
        "sellable_amount": round(sellable * p, 2),
        "buy_power": round(av, 2),
        "max_shares_by_cash": by_cash,
        "can_open_shares": can_open,
        "can_open_amount": round(can_open * p, 2),
        "buy_first_max_price": buy_first_max_price,
        "buy_first_ok_now": buy_first_ok_now,
        "price_used": p,
        "quality_override": override,
        "forms": {
            "sell_first": {
                "cmd": "--side sell --sell-reason swing --buyback-price X",
                "requires": "波段仓 >0（= 持仓 − 底仓，自动派生）；买回价 ≤ 卖价×0.95 且 ≤ fair_high",
            },
            "buy_first": {
                "cmd": "--side buy --swing-open --target-sell-price X",
                "requires": "卖出价 ≥ 买价×1.05 且 ≤ fair_high；成交后波段仓自动增加，无需再划仓",
            },
        },
        "note": "波段额度与底仓额度是两套账。底仓『还能加多少』看 matrix.room_amount；"
                "波段『能用多少钱』看这里 buy_power（= 可用资金），"
                "『能卖多少股』看 sellable_shares（= 持仓 − 底仓）。**不许拿 room_amount 回答波段问题。**",
    }



def build_entry_plan(v, p, avail, lot, quality, prefs_regime, ra, zone_ratio=0.33, budget=None):
    """
    建仓阶梯：解决「只算下行、不留上行」导致的系统性踏空。
    返回三档触发价、额度，以及在当前价下的可执行股数。
    """
    rg_name, rg = prefs_regime, REGIME[prefs_regime]
    deep = float(v.get("deep_value_price") or 0)
    entry = float(v.get("core_entry_price") or 0)
    fh = float(v.get("fair_high_price") or 0)
    q = float(quality) if quality is not None else None

    _cap = max(avail, 0.0)

    def tier(price, label):
        """三档只规定触发价，不切分金额：单笔额度 = 该标的可用资金全额。"""
        price = round(price, 4) if price else None
        sh = lots_floor(_cap, price, lot) if price else 0
        return {"label": label, "trigger_price": price,
                "max_amount": round(_cap, 2),
                "suggested_amount": round(_cap / 3, 2),
                "shares_if_triggered": sh,
                "reachable_now": bool(price and p <= price)}

    plan = {
        "regret_aversion": rg_name,
        "tier1_core": tier(entry, "一档：合理区下沿（core_entry，不含期权）"),
        "tier2_add": tier(deep, "二档：深度价值线（deep_value）"),
        "tier3_deep": tier(deep * 0.85 if deep else 0, "三档：深跌 15%（deep_value × 0.85）"),
        "amount_rule": "max_amount = 该标的可用资金全额（单笔不设比例上限）；"
                       "实际能下多少股还要与 matrix.room_amount（累计额度）取小；"
                       "suggested_amount = 可用 ÷ 3，只是节奏建议，不是上限。",
    }

    # 时间换价格：价格长期不回落到核心区时的替代路径，避免"永远等不到"
    # 前置条件①（fundamental-standard 第 1 条）：空仓或底仓未建满（已投 ≤ 1/3 预算）
    _inv = (float(budget) - float(avail)) if (budget not in (None, 0)) else 0.0
    _inv_pct = (_inv / float(budget) * 100) if (budget not in (None, 0)) else 0.0
    base_built = bool(budget not in (None, 0) and _inv > float(budget) / 3 + 1e-6)
    te_allowed = bool(rg["time_entry"] and q is not None and q >= 8 and avail > 0
                      and (v.get("option_value") or {}).get("capped_per_share")
                      and not base_built)
    # 「时间换价格」不在整个合理区启用，只在合理区下部这一段：既缓解踏空，也不在合理区上半部追
    zone_cap = round(entry + zone_ratio * (fh - entry), 4) if (entry and fh) else fh
    te = {"allowed": te_allowed, "reason": "", "zone_cap": zone_cap,
          "zone_ratio": zone_ratio, "active_now": bool(zone_cap and p <= zone_cap),
          "base_built": base_built, "invested_pct_at_review": round(_inv_pct, 2)}
    if te_allowed:
        per_round = avail / 6
        te.update({
            "reason": "踏空厌恶=high 且 质地≥8 且有已登记的上行期权：长期空仓的机会成本高于等待折价的收益",
            "zone_condition": (f"仅在 底仓线 {entry:.2f} ~ {zone_cap:.2f}（合理区下 {zone_ratio:.0%} 段）内执行；"
                               f"高于 {zone_cap:.2f} 一律不建仓，等回落"),
            "amount_per_round": round(per_round, 2),
            "shares_per_round": lots_floor(per_round, p, lot),
            "rounds": rg["rounds"],
            "max_cumulative": round(per_round * rg["rounds"], 2),
            "cadence": "价格每再下移 5% 加一轮（纯价格驱动，不看时间），累计不超过可用资金的 1/2",
        })
    else:
        why = []
        if not rg["time_entry"]:
            why.append("踏空厌恶非 high（当前 %s）" % rg_name)
        if q is None or q < 8:
            why.append("质地未达 8 分（当前 %s）" % (q if q is not None else "未评分"))
        if not (v.get("option_value") or {}).get("capped_per_share"):
            why.append("未登记上行期权")
        if avail <= 0:
            why.append("可用资金为 0")
        if base_built:
            why.append("底仓已建满（已投 %.2f%% &gt; 1/3）：C 通道只对空仓/底仓未建满开放，"
                       "合理区加仓应按矩阵「持有」处理" % _inv_pct)
        te["reason"] = "不启用：" + "；".join(why)
    plan["time_entry"] = te
    # 单笔资金上限：通道不设比例上限
    plan["single_order_cap"] = {
        "rule": "A/B 单笔仅受该标的可用资金约束；C 单笔 ≤ 可用 1/2（与其累计上限自洽）",
        "abl_pct": 1.0, "abl_amount": round(avail, 2),
        "abl_shares_at_entry": lots_floor(avail, entry, lot) if entry else 0,
        "c_pct": 0.5, "c_amount": round(avail * 0.5, 2),
        "c_shares_at_zone": lots_floor(avail * 0.5, zone_cap, lot) if zone_cap else 0,
        "note": "三档的 1/3 是节奏建议，不是硬上限。实际可下单金额 = "
                "min(该档 max_amount, matrix.room_amount)，股数按每手向下取整。",
    }
    plan["note"] = ("单点挂低价等成交不是纪律，是赌价格会回来。高价优质标的必须同时具备"
                    "「向下的深度价值线」和「向上的建仓路径」，否则仓位会长期为 0。")
    return plan


def cmd_calc(a):
    data = load()
    code, h = get_holding(data, a.code)
    ra, _rg = regime_of(data)
    p = float(a.price)
    if p <= 0:
        die("价格必须大于 0：无状态原则要求传入的是当前真实价格，不要用占位值")
    _fresh = []
    if a.price_time:
        try:
            _pt = datetime.strptime(str(a.price_time)[:10], "%Y-%m-%d").date()
            _gap = (datetime.now().date() - _pt).days
            if _gap > 1:
                _fresh.append("价格时点是 %s（距今天 %d 天）：无状态原则要求用当前价，"
                              "用它算出的区间与仓位只适用于那一天" % (a.price_time, _gap))
        except ValueError:
            _fresh.append("price_time「%s」无法解析为日期，请用 YYYY-MM-DD" % a.price_time)
    shares = h.get("shares", 0)
    cost = h.get("cost", 0.0)
    budget = h.get("budget", 0.0) or 0.0
    lot = h.get("lot_size") or 1
    basis = shares * cost
    mv = shares * p
    avail = budget - basis
    ratio = (basis / budget * 100) if budget else 0.0
    if shares == 0:
        state = "空仓"
    elif ratio <= 33:
        state = "底仓"
    elif ratio <= 66:
        state = "半仓"
    else:
        state = "重仓"
    if cost and p and abs(p / cost - 1) > 0.8:
        _fresh.append("现价 %.4f 与成本 %.4f 相差 %.0f%%，请确认价格没输错"
                      % (p, cost, abs(p / cost - 1) * 100))
    res = {
        "code": code, "name": h.get("name"), "price": p, "price_time": a.price_time,
        "shares": shares, "cost": cost, "lot_size": lot,
        "cost_basis": round(basis, 2), "market_value": round(mv, 2),
        "unrealized_pnl": round(mv - basis, 2),
        "unrealized_pnl_pct": round((p / cost - 1) * 100, 2) if cost else None,
        "budget": budget,
        "invested_ratio_pct": round(basis / budget * 100, 2) if budget else None,
        "available_cash": round(avail, 2),
        "max_buy_shares": lots_floor(max(avail, 0), p, lot),
        "tranche_1_3_shares": lots_floor(max(avail, 0) / 3, p, lot),
        "tranche_1_3_amount_est": round(lots_floor(max(avail, 0) / 3, p, lot) * p, 2),
        "regret_aversion": ra,
        "quality_score": h.get("quality_score"),
        "position_state": state,
        "position_state_label": {"空仓": "未投入", "底仓": "已投 ≤1/3",
                                 "半仓": "已投 1/3–2/3", "重仓": "已投 ≥2/3"}.get(state, state),
    }
    v = h.get("valuation") or {}
    for key in ("deep_value_price", "core_entry_price", "fair_high_price", "overvalued_above_price"):
        if v.get(key):
            res[f"vs_{key}_pct"] = round((p / float(v[key]) - 1) * 100, 2)

    ov = v.get("option_value") or {}
    if ov:
        comp = compute_option(v)          # 实时算：逾期的会自动折减
        res["option_value"] = {
            "capped_per_share": comp["capped_per_share"],
            "total_per_share": comp["total_per_share"],
            "cap_applied": comp["cap_applied"],
            "count": comp["count"],
            "pct_of_base_mid": comp["pct_of_base_mid"],
            "due_count": comp["due_count"],
            "due": comp["due"],
            "hits": comp["hits"],
            "stored_capped_per_share": ov.get("capped_per_share"),
        }
        if comp["due_count"] or comp["hits"]:
            parts = []
            if comp["due_count"]:
                parts.append("%d 条逾期未兑现（概率已折减）：%s"
                             % (comp["due_count"], "、".join(x["name"] for x in comp["due"])))
            if comp["hits"]:
                parts.append("已兑现待剔除：%s" % "、".join(comp["hits"]))
            res["option_warning"] = "；".join(parts) + "。请复核 fair_high 与 overvalued_above。"
        old_capped = float(ov.get("capped_per_share") or 0)
        if abs(comp["capped_per_share"] - old_capped) > 0.005:
            res["option_stale"] = ("存档期权价值 %.4f 与新算 %.4f 不一致（多半因到期折减）："
                                   "请重跑 option 并用新的 fair_high / overvalued_above 覆盖 set-thesis"
                                   % (old_capped, comp["capped_per_share"]))
    ct = normalize_type(h)
    res["company_type"] = ct
    prim = ct.get("primary")
    if prim:
        res["type_focus"] = TYPE_FOCUS[prim]
        res["type_break_signal"] = TYPE_BREAK[prim]
        res["valuation_model"] = {"key": ct.get("valuation_model"),
                                  "label": MODEL_ENUM.get(ct.get("valuation_model")),
                                  "is_default": ct.get("valuation_model") == MODEL_DEFAULT[prim]}
        if ct.get("counter_evidence"):
            res["counter_evidence"] = ct["counter_evidence"]
        if ct.get("user_view"):
            res["user_view"] = ct["user_view"]
    else:
        res["type_warning"] = "公司性质未判定：先跑 set-type，否则分析重点与估值方法都可能用错"
    if not ct.get("confirmed"):
        res["type_unconfirmed"] = True
    dv = normalize_drivers(h)
    if dv.get("items"):
        summ = summarize_drivers(dv)
        res["drivers"] = summ
        if summ["stale"]:
            res["driver_warning"] = (f"{len(summ['stale'])} 个因子超过更新周期未刷新："
                                     + "、".join(f"{x['name']}({x['days']}天)" for x in summ["stale"])
                                     + "；早盘预案必须先补数据，不能只用价格定方案")
        elif summ["never_updated"]:
            res["driver_warning"] = ("以下因子从未登记读数：" + "、".join(summ["never_updated"])
                                     + "；首次取数后请回填 latest_value/latest_date")
    else:
        res["driver_warning"] = "未登记驱动因子：若该股由行业数据驱动（猪价、运价、批价等），必须先 set-drivers"
    res["report_links"] = {"%s" % slot: file_uri(REPORTS / ("%s-%s.html" % (code, slot)))
                           for slot, _label in REPORT_SLOTS
                           if (REPORTS / ("%s-%s.html" % (code, slot))).exists()}
    tol = (data.get("prefs") or {}).get("drawdown_tolerance")
    pnl = res.get("unrealized_pnl_pct")
    if tol and pnl is not None and pnl < -float(tol) * 100:
        res["emotion_guard"] = ("当前浮亏 %.2f%%，已超出你设定的承受区间 %.0f%%。"
                                "此时做决定最容易受情绪驱动：只按既定通道执行，不临时加仓或减仓；"
                                "若要减仓，理由必须是基本面恶化或估值透支。" % (pnl, float(tol) * 100))
    _upd = h.get("updated_at") or ""
    try:
        _age = (datetime.now() - datetime.strptime(str(_upd)[:10], "%Y-%m-%d")).days
    except ValueError:
        _age = None
    if _age is not None and _age >= 30 and int(h.get("shares", 0)) > 0:
        res["holding_stale"] = ("持仓快照已 %d 天未核对（最后 %s）：分红送股、拆股会在不成交的情况下改变股数，"
                                "请对照券商账户核对一次（有变化就 upsert 覆盖）" % (_age, str(_upd)[:10]))
    _core_sh, _swing_sh = normalize_split(h)
    if h.get("shares"):
        res["position_split"] = {
            "shares": int(h["shares"]), "core_shares": _core_sh, "swing_shares": _swing_sh,
            "swing_pct": round(_swing_sh / int(h["shares"]) * 100, 1),
            "max_swing_per_trade": min(_swing_sh, int(h["shares"]) // 3),
            "swing_cash": round(max(avail, 0), 2),
            "core_cost_basis": round(_core_sh * cost, 2),
            "split_configured": bool(h.get("split_configured")),
            "note": (("底仓 %d 股不参与波段；波段仓 %d 股仅可先卖后买（先卖后买，卖出须给买回价）"
                      % (_core_sh, _swing_sh)) if _swing_sh
                     else (("底仓 %d 股已锁定，波段仓当前 0 股；可用波段资金 %.2f 元待部署"
                            % (_core_sh, max(avail, 0))) if h.get("split_configured")
                           else "未划分底仓/波段仓：全部持股视为底仓，卖出必须基于基本面恶化或估值透支")),
        }
    _rev = v.get("revised_at") or (str(v.get("set_on") or "") + " 00:00:00" if v.get("set_on") else "")
    _rev_age = None
    if _rev:
        try:
            _rev_age = round((datetime.now() - datetime.strptime(str(_rev)[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400, 1)
        except ValueError:
            _rev_age = None
    res["anchor_revision"] = {"revised_at": _rev or None, "age_days": _rev_age,
                              "is_today": (_rev_age is not None and _rev_age < 1),
                              "is_stale": (_rev_age is not None and _rev_age > 120)}
    if _rev_age is not None and _rev_age > 120:
        _fresh.append("估值锚已 %.0f 天未复核（最后 %s）：财报口径可能已过期，建议跑一次深度复核"
                      % (_rev_age, str(_rev)[:10]))
    _prof = resolve_profile(data, h)
    res["entry_style"] = {"key": _prof["entry_style"], "label": _prof["entry_style_label"],
                          "rules": _prof["rules"], "source": _prof["source"]["entry_style"]}
    res["trading_profile"] = _prof
    res["budget_note"] = ("该标的可投入总额 %.2f，已投 %.2f（%.0f%%），还可投 %.2f。"
                          "budget 是『这笔钱能全投进去』的额度，投满 100%% 是正常终局，"
                          "不代表账户满仓；它只用于控制单个标的的上限。"
                          % (budget, basis, (basis / budget * 100) if budget else 0, max(avail, 0)))
    if avail < 0:
        _fresh.append("可用资金为负（%.2f）：持仓成本已超过 budget，本轮任何买入都会被额度校验拦下；"
                      "请核对 budget 是否设错" % avail)
    if _fresh:
        res["price_warning"] = "；".join(_fresh)
    res["valuation_zone_by_price"] = zone_of(v, p)
    # 折现率因子必须可追溯：有 10Y 因子但没记基准 → 禁止改锚，只能写定性说明
    _rate_items = [x for x in (h.get("drivers") or {}).get("items") or []
                   if isinstance(x, dict) and x.get("category") == "rate"]
    if _rate_items:
        _rb = (v or {}).get("rate_baseline")
        _rv = _rate_items[0].get("latest_value")
        if _rb in (None, "", {}):
            res["rate_warning"] = ("已登记利率因子（10Y 美债）但 valuation.rate_baseline 为空："
                                   "本轮不得做倍数修正，报告只能写定性说明并标注「锚未做利率校准」；"
                                   "请在 set-thesis 的 valuation 里补 rate_baseline={yield, date}")
        elif _rv in (None, ""):
            res["rate_warning"] = ("利率因子已登记但 latest_value 为空：先回填当前 10Y 读数"
                                   "（含日期与纽约尾盘/收盘口径），再谈倍数修正")
    if v.get("_schema") == "legacy-4anchor":
        res["valuation_schema"] = "legacy-4anchor"
        res["warning"] = v.get("_legacy_note")
    # 锚复评触发：因子先动、锚才动，买点才有依据。放在 entry_plan 之前算，
    # 好让报告把「锚该重评了」写在「买点在哪」的前面。
    _rk = check_reanchor(v, h)
    res["reanchor"] = _rk
    if _rk["triggered"] or _rk["event_due"]:
        res["reanchor_warning"] = _rk["verdict"]
    if v.get("deep_value_price"):
        res["entry_plan"] = build_entry_plan(v, p, max(avail, 0), lot,
                                             h.get("quality_score"), ra, ra,
                                             _prof["entry_zone_ratio"], budget)
    # 决策矩阵脚本化：报告回答「还能加多少」只能引用 room_amount
    res["matrix"] = matrix_advice(res.get("valuation_zone_by_price") or zone_of(v, p),
                                  budget, basis, max(avail, 0))
    # 波段额度独立成块：与底仓矩阵是两套账，互不套用
    res["swing"] = swing_advice(h, p, max(avail, 0), lot,
                                (v or {}).get("fair_high_price"), h.get("quality_score"))
    if not v:
        res["warning"] = "未设置估值锚：禁止给出任何买入建议，先跑 option + set-thesis。"
    _all = [res.get(k) for k in ("warning", "type_warning", "driver_warning",
                                 "option_warning", "option_stale", "holding_stale",
                                 "price_warning", "emotion_guard", "rate_warning",
                                 "reanchor_warning")]
    res["warnings"] = [x for x in _all if x]
    out({"ok": True, **res})


# ---------------------------------------------------------------- 量价定性
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _pick(b, *keys):
    for k in keys:
        if k in b and b[k] is not None:
            v = b[k]
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def load_bars(path):
    """
    兼容三种输入：
      1) 自定义：{"market":"A","elapsed_minutes":210,"bars":[{date,open,high,low,close,volume}]}
      2) 行情工具 data_kline 原样返回：{"data":{"nodes":[{date,open,high,low,last,volume}]}}
      3) {"nodes":[...]}
    统一输出按日期升序的 bars。
    """
    if not Path(path).exists():
        die(f"找不到 K 线文件：{path}（先把行情返回按三种格式之一存成 JSON）")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except json.JSONDecodeError as e:
        die(f"K 线文件不是合法 JSON：{e}")
    mkt_hint, elapsed_hint, raw = None, None, None
    if isinstance(d, dict) and isinstance(d.get("bars"), list):
        raw = d["bars"]
        mkt_hint = d.get("market")
        elapsed_hint = d.get("elapsed_minutes")
    else:
        node = d
        for _ in range(6):
            if not isinstance(node, dict):
                break
            if isinstance(node.get("nodes"), list):
                raw = node["nodes"]
                break
            if isinstance(node.get("bars"), list):
                raw = node["bars"]
                break
            node = node.get("data")
    if raw is None:
        die("无法解析 K 线文件：需为 {\"bars\":[...]} 或 data_kline 返回的 {\"nodes\":[...]}")

    bars = []
    for b in raw:
        close = _pick(b, "close", "last", "收盘价", "收盘")
        o, h, l = _pick(b, "open", "开盘价", "开盘"), _pick(b, "high", "最高价", "最高"), \
                  _pick(b, "low", "最低价", "最低")
        v = _pick(b, "volume", "成交量", "vol")
        date = b.get("date") or b.get("day") or b.get("日期")
        if None in (close, o, h, l, v) or not date:
            continue
        bars.append({"date": str(date)[:10], "open": o, "high": h, "low": l, "close": close, "volume": v})
    bars.sort(key=lambda x: x["date"])
    return bars, mkt_hint, elapsed_hint


def cmd_volprice(a):
    """
    输入见 load_bars 的三种格式。至少 21 根 K 线，推荐 61 根以上（用于判断是否处于高位）。
    当日已交易分钟：A股 14:30=210、收盘=240；港股 15:30=300、收盘=330。
    可用 --elapsed 覆盖，或 --market 指定市场。
    """
    bars, mkt_hint, elapsed_hint = load_bars(a.file)
    if len(bars) < 21:
        die("K线不足 21 根，无法做量价定性")
    mkt = a.market or mkt_hint or "A"
    total = SESSION_MINUTES.get(mkt, 240)
    elapsed = a.elapsed or elapsed_hint or total
    today, hist = bars[-1], bars[:-1]
    vols = [b["volume"] for b in hist]
    closes = [b["close"] for b in hist]
    proj_vol = today["volume"] * total / max(elapsed, 1)
    v5 = sma(vols, 5)
    vol_ratio = proj_vol / v5 if v5 else None
    prev = hist[-1]
    chg = (today["close"] / prev["close"] - 1) * 100
    ma20 = sma(closes + [today["close"]], 20)
    ma60 = sma(closes + [today["close"]], 60)
    low20 = min(b["low"] for b in hist[-20:])
    high60 = max(b["high"] for b in (hist[-60:] if len(hist) >= 60 else hist))
    low60 = min(b["low"] for b in (hist[-60:] if len(hist) >= 60 else hist))
    pos60 = (today["close"] - low60) / (high60 - low60) if high60 > low60 else 0.5
    rng = today["high"] - today["low"]
    upper_shadow = (today["high"] - max(today["open"], today["close"])) / rng if rng else 0
    lower_shadow = (min(today["open"], today["close"]) - today["low"]) / rng if rng else 0
    # 近 5 日累计跌幅与是否连创新低
    last5 = hist[-5:]
    down5 = (today["close"] / last5[0]["close"] - 1) * 100
    new_low = today["low"] < min(b["low"] for b in hist[-5:])
    prev_down = prev["close"] < hist[-2]["close"]
    broke_support = (today["close"] < low20) or (ma60 and prev["close"] >= ma60 > today["close"]) or \
                    (ma20 and prev["close"] >= ma20 > today["close"])
    big = vol_ratio is not None and vol_ratio >= 1.5
    small = vol_ratio is not None and vol_ratio <= 0.8

    # 量能是五态的第一分界：缩量态与放量态互斥，用 ±3 的门槛分强制隔离
    vol_gate_small = 3 if small else -3
    vol_gate_big = 3 if big else -3
    scores = {
        "放量破位": vol_gate_big + (2 if broke_support else 0) + (1 if chg <= -3 else 0),
        "放量反包": vol_gate_big + (2 if (prev_down and today["close"] >= prev["open"] and chg > 0) else 0)
                   + (1 if chg >= 3 else 0),
        "缩量止跌": vol_gate_small + (2 if (not new_low and down5 < 0) else 0)
                   + (1 if (abs(chg) <= 1 or lower_shadow >= 0.4) else 0),
        "缩量阴跌": vol_gate_small + (2 if (new_low and chg < 0) else 0) + (1 if down5 <= -3 else 0),
        "高位放量滞涨": vol_gate_big + (2 if pos60 >= 0.8 else 0)
                     + (1 if (abs(chg) <= 1.5 or upper_shadow >= 0.4) else 0),
    }
    label = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    margin = ranked[0] - ranked[1]
    out({
        "suggested_label": label,
        "score_margin": margin,
        "scores": scores,
        "note": "脚本给出的是规则化初判；最终必须五选一并结合K线形态、支撑位与当日消息说明依据，若与初判不同需写明理由。score_margin<=1 表示初判不稳健，须人工定性。",
        "metrics": {
            "today_change_pct": round(chg, 2),
            "projected_full_day_volume": round(proj_vol),
            "avg_volume_5d": round(v5) if v5 else None,
            "volume_ratio_vs_5d": round(vol_ratio, 2) if vol_ratio else None,
            "ma20": round(ma20, 4) if ma20 else None,
            "ma60": round(ma60, 4) if ma60 else None,
            "low_20d_prior": low20,
            "position_in_60d_range": round(pos60, 2),
            "five_day_change_pct": round(down5, 2),
            "new_5d_low": new_low,
            "broke_support": bool(broke_support),
            "upper_shadow_ratio": round(upper_shadow, 2),
            "lower_shadow_ratio": round(lower_shadow, 2),
        },
    })


# ---------------------------------------------------------------- 硬约束校验
def cmd_check_order(a):
    data = load()
    code, h = get_holding(data, a.code)
    side = a.side
    shares = int(a.shares)
    limit = float(a.limit)
    prev_close = float(a.prev_close)
    lot = h.get("lot_size") or 1
    budget = h.get("budget", 0.0) or 0.0
    basis = h.get("shares", 0) * h.get("cost", 0.0)
    avail = budget - basis
    checks = []
    cap_info = {}

    def chk(name, ok, detail):
        checks.append({"constraint": name, "pass": bool(ok), "detail": detail})

    if side == "hold":
        chk("不动", True, "无订单，无需校验金额/股数/限价")
        out({"ok": True, "code": code, "side": side, "all_pass": True, "checks": checks})
        return

    amount = shares * limit
    chk("股数为每手整数倍", shares > 0 and shares % lot == 0, f"{shares} 股，每手 {lot}")
    lim = price_limit_pct(code, h.get("name", ""))
    if lim is not None:
        up, dn = round(prev_close * (1 + lim), 2), round(prev_close * (1 - lim), 2)
        chk("限价在涨跌停范围内", dn <= limit <= up, f"限价 {limit}，允许 [{dn}, {up}]（±{lim:.0%}）")
    else:
        chk("限价偏离昨收不超过 ±10%（港美股无涨跌停，防误填）", abs(limit / prev_close - 1) <= 0.10,
            f"限价 {limit}，昨收 {prev_close}")
    if side == "buy":
        v = h.get("valuation") or {}
        ra, _rg = regime_of(data)
        fh = v.get("fair_high_price")
        entry = v.get("core_entry_price")
        deep = v.get("deep_value_price")
        ov = v.get("option_value") or {}
        q = h.get("quality_score")

        # ⓪ 锚复评闸门：事先写死的因子阈值已越界 / 事件已到期 → 旧锚失效，
        # 这笔单必须先重跑锚再下，否则就是把「锚已经不对了」的买点当有效买点。
        _rk = check_reanchor(v, h)
        if _rk["triggered"] or _rk["event_due"]:
            _hits = [str(x.get("desc") or x.get("id"))
                     for x in (_rk["triggered"] + _rk["event_due"])]
            chk("锚复评已触发 → 必须先重跑锚（旧锚算出的买点无效）", a.anchor_reviewed,
                "已复核并重跑锚" if a.anchor_reviewed
                else "触发项：" + "；".join(_hits) +
                     "。先 option + set-thesis 重算四锚，再带 --anchor-reviewed 重下这笔单")

        # ① 期权必须显式评估过（可以为空，但不能"没想过"）
        chk("上行期权已评估（含显式确认该标的无可定价期权）", a.option_reviewed,
            "由分析结论传入 --option-reviewed" if not a.option_reviewed
            else f"已登记 {len(ov.get('items') or [])} 条，每股期权价值 {ov.get('capped_per_share')}")

        # ② 上限：不超过合理上沿（合理上沿已含期权打折计入）
        chk("限价不高于合理上沿（含期权打折计入）", bool(fh) and limit <= float(fh) + 1e-9,
            f"限价 {limit}，合理上沿 {fh}" if fh else "未设定 fair_high_price 估值锚，禁止买入")

        # ③ 通道判定 / 波段买回判定（decision-rules 7.3：买回不要求落回建仓窗口）
        _prof = resolve_profile(data, h)
        _zr = _prof["entry_zone_ratio"]
        zone_cap = (float(entry) + _zr * (float(fh) - float(entry))) if (entry and fh) else fh
        _core_sh, _swing_sh = normalize_split(h)
        if a.swing_open:
            # 波段建仓（先买后卖）。decision-rules 7.4：波段仓的买入**不受建仓通道与矩阵 room 限制**，
            # 因为它不是长期敞口，用「必须在低估区买」去管它，等于把波段变成第二份底仓。
            # 改由「必须给出卖出价 + 5% 价差 + 规模上限 35% + 总投入不超 budget」约束。
            _tsp = a.target_sell_price
            chk("波段建仓必须同时给出计划卖出价（不给卖出价，那就只是加仓）", bool(_tsp),
                ("计划卖出 %s" % _tsp) if _tsp else "缺失 --target-sell-price")
            if _tsp:
                chk("波段卖出价高于买入价至少 5%（否则覆盖不了往返成本与踏空风险）",
                    float(_tsp) >= limit * 1.05 - 1e-9,
                    "卖出 %s vs 买入 %s×1.05=%.4f" % (_tsp, limit, limit * 1.05))
                chk("波段卖出价不高于合理上沿（不指望卖进偏贵区）",
                    bool(fh) and float(_tsp) <= float(fh) + 1e-9,
                    ("卖出 %s，合理上沿 %s" % (_tsp, fh)) if fh else "未设 fair_high：禁止波段")
            # 仓位只划底仓，波段仓 = 持仓 − 底仓（自动派生），不设比例上限：
            # 比例既不是钱的口径也不是风险的口径，只是把波段塞回底仓刻度的尺子。
            # 先买后卖的规模只由「钱」约束，即下面这条「买入金额不超过该标的可用资金」。
            chk("买入金额不超过该标的可用资金", amount <= avail + 1e-6,
                "拟用 %s，可用 %s" % (format(amount, ",.2f"), format(avail, ",.2f")))
            # 波段就是高抛低吸，不存在 2/3 界，也不参与任何预算刻度判断。
            # 「买后总投入 ≤ budget」与「金额 ≤ 可用资金」在 avail = budget − basis 下恒等，
            # 属于冗余；而它输出的 amount_pct_of_budget_after 会把波段硬塞回底仓的预算百分比，
            # 正是两套规则混淆的表现，故不输出。
            cap_info = {"swing_open": True, "target_sell_price": _tsp,
                        "swing_before": _swing_sh, "swing_after": _swing_sh + shares,
                        "core_shares_unchanged": _core_sh,
                        "swing_note": "波段不设预算刻度、也不设股数比例上限：只看可用资金与 fair_high。"
                                      "买入后波段仓自动 = 持仓 − 底仓，无需重新划仓。",
                        "concentration_note": "买入后持仓 %d 股、底仓 %d 股不变 → 波段仓 %d 股"
                                              % (int(h.get("shares", 0)) + shares, _core_sh,
                                                 _swing_sh + shares)}
        elif a.swing_buyback:
            _sp = a.swing_sell_price
            _ss = a.swing_sold_shares
            _floor = round(float(_sp) * 0.95, 4) if _sp else None
            chk("已提供上一轮波段卖出价（用于校验价差）", bool(_sp),
                (f"卖出价 {_sp}，买回上限 {_floor}") if _sp
                else "缺少 --swing-sell-price：无法校验「买回 ≤ 卖价×0.95」")
            chk("波段买回价低于卖出价至少 5%", bool(_sp) and limit <= _floor + 1e-9,
                (f"买回 {limit} vs 卖价 {_sp}×0.95={_floor}") if _sp else "未提供卖出价")
            chk("波段买回不要求落回建仓窗口（fair_high 即上限，见 7.3 第 4 条）",
                bool(fh) and limit <= float(fh) + 1e-9,
                f"买回 {limit} ≤ 合理上沿 {fh}；窗口上限 {round(zone_cap, 2) if zone_cap is not None else '—'} 仅用于新建仓，不约束买回")
            if _ss:
                _back = float(_sp) * int(_ss) if _sp else 0.0
                chk("波段买回股数不超过上一轮卖出股数", shares <= int(_ss), f"拟买回 {shares}，上轮卖出 {_ss}")
                chk("波段买回金额不超过上轮卖出回收金额", amount <= _back + 1e-6,
                    f"拟用 {amount:,.2f}，上轮回收 {_back:,.2f}")
            chk("买入金额不超过该标的可用资金", amount <= avail + 1e-6, f"拟用 {amount:,.2f}，可用 {avail:,.2f}")
        else:
            # 单笔操作不设通道比例上限：A/B 用可用全额、C 用 1/2（与累计上限对齐）
            # A/B 单笔仅受该标的可用资金约束（1.0）；C 保留 1/2，以与其「累计 ≤ 可用 1/2」自洽
            if deep and limit <= float(deep):
                channel, ratio_cap = "深度低估通道", 1.0
            elif entry and limit <= float(entry):
                channel, ratio_cap = "低估区建底仓", 1.0
            elif zone_cap and limit <= float(zone_cap):
                channel, ratio_cap = "时间换价格底仓", 0.5
            elif _prof["entry_style"] == "right" and entry and limit > float(entry):
                channel, ratio_cap = "无可用通道", 0
            else:
                channel, ratio_cap = "无可用通道", 0
            if channel == "无可用通道":
                if not (entry and fh):
                    detail = "未设置估值锚：无法判定建仓通道，先跑 option + set-thesis 再下单"
                else:
                    detail = (f"限价 {limit} 处于合理区上半部（时间换价格窗口上限 {zone_cap:.2f}）"
                              f"或已超合理上沿 {fh}：此价位不建仓，等回落")
            else:
                detail = (f"限价 {limit} → {channel}（深度低估≤{deep}｜底仓≤{entry}｜"
                          f"时间换价格≤{round(zone_cap, 2) if zone_cap is not None else '—'}"
                          f"，即合理区下 {_zr:.0%} 段）")
            chk("价格落在可执行的建仓通道内", channel != "无可用通道", detail)

            # ④ 合理区内买入必须走「时间换价格」，且有门槛
            if channel == "时间换价格底仓":
                _inv_pct2 = (basis / budget * 100) if budget else 0.0
                _base_built = bool(budget and basis > budget / 3 + 1e-6)
                gate = (a.time_entry and ra == "high" and q is not None and float(q) >= 8
                        and bool(ov.get("capped_per_share")) and not _base_built)
                chk("合理区内买入走「时间换价格」且满足门槛（空仓/底仓未建满、踏空厌恶=high、质地≥8、期权已定价）",
                    gate,
                    f"time_entry={a.time_entry}；已投 {_inv_pct2:.2f}%（底仓已建满={_base_built}，门槛 ≤33.33%）；"
                    f"踏空厌恶={ra}；质地={q}；期权={ov.get('capped_per_share')}")

            # ⑤ 金额与分批
            chk("买入金额不超过该标的可用资金", amount <= avail + 1e-6, f"拟用 {amount:,.2f}，可用 {avail:,.2f}")
            cap = avail * ratio_cap
            min_lot_amt = limit * lot
            cap_label = ("单笔不超过该标的可用资金（通道上限已放开至全额）" if ratio_cap >= 0.999
                         else f"单笔不超过可用资金 {ratio_cap:.0%}（{channel}累计上限，非通道比例）")
            if ratio_cap and min_lot_amt > cap:
                chk(cap_label, amount <= min_lot_amt + 1e-6,
                    f"一手金额 {min_lot_amt:,.2f} 已超单笔上限 {cap:,.2f}，降级为「最多买一手」")
            else:
                chk(cap_label, bool(ratio_cap) and amount <= cap + 1e-6,
                    f"拟用 {amount:,.2f}，上限 {cap:,.2f}（可用 {avail:,.2f}）")
            if avail > 0:
                cap_info = {"cap_ratio": ratio_cap, "cap_amount": round(cap, 2),
                            "amount_pct_of_avail": round(amount / avail * 100, 2),
                            "concentration_note": (
                                f"本笔用掉该标的可用资金的 {amount / avail:.0%}：已是集中下单，"
                                f"分批节奏（A/B/C 三档）将被一次性消耗，后续无子弹补仓"
                                if amount / avail > 0.5 else "")}

        chk("基本面未恶化（无 A1–A7 信号）", a.fundamental_ok, "由分析结论传入 --fundamental-ok")
    if side == "sell":
        v = h.get("valuation") or {}
        over = v.get("overvalued_above_price")
        core_sh, swing_sh = normalize_split(h)
        chk("卖出股数不超过持股", shares <= h.get("shares", 0), f"拟卖 {shares}，持有 {h.get('shares', 0)}")
        if a.sell_reason == "swing":
            chk("波段卖出股数不超过波段仓", shares <= swing_sh,
                f"拟卖 {shares}，波段仓 {swing_sh}（底仓 {core_sh} 不动）" if swing_sh
                else "未划分波段仓：先 set-profile --core-shares 划出波段仓，或改用 fundamental/overvalued 理由")
            if a.swing_exit:
                # 先买后卖的兑现腿：出口在买入时（--target-sell-price）就定了，此处只验证价差成立
                chk("先买后卖兑现必须给出当时买入价", bool(a.swing_entry_price),
                    f"买入价 {a.swing_entry_price}" if a.swing_entry_price
                    else "缺失 --swing-entry-price：无法验证「卖出价 ≥ 买入价×1.05」")
                if a.swing_entry_price:
                    _f = float(a.swing_entry_price) * 1.05
                    chk("卖出价高于买入价至少 5%（先买后卖的出口条件）", limit >= _f - 1e-9,
                        f"卖出 {limit} vs 买入 {a.swing_entry_price}×1.05={_f:.4f}")
            else:
                chk("波段卖出必须给出计划买回价（先卖后买），或改用 --swing-exit（先买后卖兑现）",
                    bool(a.buyback_price),
                    f"买回价 {a.buyback_price}" if a.buyback_price
                    else "缺失 --buyback-price：没有买回计划的波段就是减仓；"
                         "若这是早先 --swing-open 买入的仓位兑现，请改用 --swing-exit --swing-entry-price")
                if a.buyback_price and limit:
                    chk("买回价低于卖出价至少 5%", float(a.buyback_price) <= limit * 0.95 + 1e-9,
                        f"买回 {a.buyback_price} vs 卖出 {limit}×0.95={limit * 0.95:.2f}")
        else:
            chk("卖出理由为基本面恶化或估值透支（非短期下跌）",
                a.sell_reason in ("fundamental", "overvalued"), f"sell_reason={a.sell_reason}")
            if swing_sh:
                chk("非波段卖出会动用底仓，须确认是有意减底仓",
                    shares > swing_sh or a.sell_reason in ("fundamental", "overvalued"),
                    f"拟卖 {shares}，波段仓 {swing_sh}，底仓 {core_sh}")
        if a.sell_reason == "overvalued":
            if not over:
                chk("估值透支理由成立：卖出价已到透支线（透支线已含期权全额）", False,
                    "未设置估值锚：无法验证透支理由，先跑 option + set-thesis；"
                    "在此之前只能用 fundamental（基本面恶化）作为卖出理由")
            else:
                chk("估值透支理由成立：卖出价已到透支线（透支线已含期权全额）",
                    limit >= float(over) - 1e-9, f"卖出限价 {limit}，透支线 {over}")
    chk("行情数据为当日最新且已核对时间戳", a.data_fresh, "由分析结论传入 --data-fresh")
    all_pass = all(c["pass"] for c in checks)
    out({"ok": True, "code": code, "side": side, "shares": shares, "limit": limit,
         "amount": round(amount, 2),
         "regret_aversion": regime_of(data)[0],
         "all_pass": all_pass, "checks": checks, **cap_info})


# ---------------------------------------------------------------- 分析日志
REPORT_SLOTS = [("open", "早盘"), ("close", "收盘"), ("deep", "深度")]


def _policy_net(plist):
    """政策净方向：多空相抵即为 neutral，不能因为有任一利好就判 bullish。"""
    nb = sum(1 for x in plist if x.get("direction") == "bearish")
    nu = sum(1 for x in plist if x.get("direction") == "bullish")
    if nb > nu:
        return "bearish"
    if nu > nb:
        return "bullish"
    return "neutral"


def tag_label(x):
    return TAG_ENUM.get(x) or TYPE_ENUM.get(x) or x


def file_uri(path):
    """本地路径 → file:/// URI，供聊天里的 Markdown 链接使用。"""
    p = str(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + p


def cmd_new_report(a):
    """
    固定槽位报告：每标的只保留 deep / open / close 三个文件，同名覆盖，永不按日期新建。
    历史结论由 analysis_log.jsonl 承担，报告本身不做版本堆积。
    """
    data = load()
    code, h = get_holding(data, a.code)
    REPORTS.mkdir(parents=True, exist_ok=True)
    suffix = {"deep": "deep", "morning": "open", "afternoon": "close"}[a.session]
    path = REPORTS / f"{code}-{suffix}.html"
    replaced = path.exists()
    prev_size = path.stat().st_size if replaced else 0

    archived = []
    if not a.keep_legacy:  # 把旧的按日期命名的同槽位文件挪进 _archive，保持根目录只有 3 个槽位
        arch = REPORTS / "_archive"
        for f in REPORTS.glob(f"{code}-*-{suffix}.*"):
            try:
                arch.mkdir(exist_ok=True)
                f.replace(arch / f.name)
                archived.append(f.name)
            except OSError:
                pass

    out({"ok": True, "path": str(path), "slot": suffix, "code": code, "name": h.get("name"),
         "uri": file_uri(path),
         "markdown_link": "[%s · %s报告](%s)" % (h.get("name") or code,
                                                  {"open": "早盘", "close": "收盘", "deep": "深度"}[suffix],
                                                  file_uri(path)),
         "will_overwrite": replaced, "prev_size": prev_size, "archived_legacy": archived,
         "rule": "每标的固定 open / close / deep 三个槽位，同一槽位永远覆盖写，不按日期新建",
         "usage": "写完报告后，在公司分析块的最后一行原样附上 markdown_link，不要自己拼路径"})


INDEX_SHELL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓报告索引</title>
<style>
  :root{--ink:#0f172a;--muted:#64748b;--line:#e5e8ee;--bg:#f4f6f9;--blue:#1a56db}
  *{box-sizing:border-box}
  body{margin:0;padding:28px 20px 48px;background:var(--bg);color:var(--ink);
       font:14px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:880px;margin:0 auto}
  header{background:linear-gradient(135deg,#111c33,#243a63);color:#fff;border-radius:18px;
         padding:22px 26px;box-shadow:0 16px 36px -22px rgba(15,23,42,.55);margin-bottom:16px}
  header h1{margin:0 0 6px;font-size:20px;letter-spacing:.3px}
  header p{margin:0;font-size:12.5px;color:#a9bcda}
  .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 20px 16px;
        margin-top:14px;box-shadow:0 1px 2px rgba(15,23,42,.04),0 12px 26px -22px rgba(15,23,42,.28)}
  .ch{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px}
  .ch h2{margin:0;font-size:16px}
  .code{font:11.5px/1 ui-monospace,Consolas,monospace;color:var(--muted);letter-spacing:1px}
  .pos{margin-left:auto;font-size:12px;color:var(--muted)}
  .badges{margin:10px 0 8px}
  .badge{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 9px;
         border-radius:999px;white-space:nowrap}
  .b-buy{color:#1a56db;background:#eef4ff}
  .b-fair{color:#475569;background:#f1f5f9}
  .b-rich{color:#b45309;background:#fdf3e3}
  .concl{font-size:12.5px;color:#334155;background:#f8fafc;border-left:3px solid #cbd5e1;
         padding:8px 12px;border-radius:0 8px 8px 0;margin:6px 0 12px}
  .concl i{font-style:normal;color:#94a3b8;margin-left:8px;font-size:11.5px}
  .links{display:flex;flex-wrap:wrap;gap:10px}
  .btn{display:inline-flex;flex-direction:column;gap:2px;padding:8px 14px;border-radius:10px;
       border:1px solid #d9e2f3;background:#f7faff;color:var(--blue);text-decoration:none;
       font-size:13px;font-weight:600}
  .btn:hover{background:#eef4ff;border-color:#b9cdf5}
  .btn span{font-size:10.5px;font-weight:400;color:#94a3b8}
  .empty{font-size:12.5px;color:#94a3b8}
  .filters{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 4px}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:5px 13px;border-radius:999px;
        border:1px solid #dfe5ef;background:#fff;color:#475569;font-size:12.5px;font-weight:600;
        cursor:pointer;transition:.15s;font-family:inherit}
  .chip b{font-weight:700;color:#94a3b8;font-size:11.5px}
  .chip:hover{border-color:#b9cdf5;background:#f7faff}
  .chip.on{background:#111c33;border-color:#111c33;color:#fff}
  .chip.on b{color:#9db0cf}
  .chip.tag{border-style:dashed}
  .chip.tag.on{background:#b45309;border-color:#b45309;border-style:solid}
  .chip.tag.on b{color:#fde6c8}
  #vi-empty{display:none;text-align:center;padding:28px 0;color:#94a3b8;font-size:13px}
  footer{margin-top:22px;font-size:11.5px;color:#94a3b8;text-align:center;line-height:1.8}
</style></head><body><div class="wrap">
<header>
  <h1>持仓报告索引</h1>
  <p>每标的三份：早盘预案 / 收盘决策 / 深度分析。定时任务覆盖更新，这里始终指向最新一份。</p>
</header>
<div class="filters" id="vi-filters"><!--CHIPS--></div>
<div id="vi-empty">该分类下暂无标的</div>
<!--CARDS-->
<footer>生成时间 __TIME__　·　报告目录由 VI_DATA_DIR（或默认数据目录）决定，运行 path 子命令查看</footer>
</div>
<script>
(function () {
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card[data-type]'));
  var empty = document.getElementById('vi-empty');
  function apply(f) {
    var n = 0;
    cards.forEach(function (c) {
      var tags = ',' + (c.getAttribute('data-tags') || '') + ',';
      var ok = (f === 'all') || c.getAttribute('data-type') === f || tags.indexOf(',' + f + ',') > -1;
      c.style.display = ok ? '' : 'none';
      if (ok) { n++; }
    });
    chips.forEach(function (ch) { ch.classList.toggle('on', ch.getAttribute('data-f') === f); });
    if (empty) { empty.style.display = n ? 'none' : 'block'; }
    try {
      if (f === 'all') { history.replaceState(null, '', location.pathname); }
      else { history.replaceState(null, '', '#' + f); }
    } catch (e) {}
  }
  chips.forEach(function (ch) {
    ch.addEventListener('click', function () { apply(ch.getAttribute('data-f')); });
  });
  var want = (location.hash || '').replace('#', '') || 'all';
  var hit = chips.filter(function (ch) { return ch.getAttribute('data-f') === want; })[0];
  apply(hit ? want : 'all');
})();
</script>
</body></html>"""


def _render_index(rows):
    order = list(TYPE_ENUM.keys())
    counts, tag_counts = {}, {}
    for r in rows:
        counts[r["type_key"]] = counts.get(r["type_key"], 0) + 1
        for t in r["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    chips = ['<button class="chip on" data-f="all">全部 <b>%d</b></button>' % len(rows)]
    for k in order + ["none"]:
        if counts.get(k):
            chips.append('<button class="chip" data-f="%s">%s <b>%d</b></button>'
                         % (k, TYPE_ENUM.get(k, "未判定"), counts[k]))
    for t, c in sorted(tag_counts.items(), key=lambda x: -x[1]):
        chips.append('<button class="chip tag" data-f="%s">%s <b>%d</b></button>'
                     % (t, tag_label(t), c))
    chip_html = "\n".join(chips)

    cards = []
    for r in rows:
        badges = '<span class="badge b-buy">%s</span>' % r["type"]
        for t in r["tags"]:
            badges += ' <span class="badge b-fair">%s</span>' % tag_label(t)
        if r["quality"]:
            badges += ' <span class="badge b-fair">质地 %s/10</span>' % r["quality"]
        badges += (' <span class="badge b-buy">%s · 窗口 %.0f%%</span>'
                   % (r["style"], r["zone_ratio"] * 100))
        if r.get("anchor_rev"):
            _cls = "b-over" if r.get("anchor_stale") else "b-fair"
            badges += (' <span class="badge %s">锚 %s</span>' % (_cls, r["anchor_rev"][5:]))
        if r.get("policy_n"):
            _pc = {"bearish": "b-over", "bullish": "b-buy", "neutral": "b-fair"}[r["policy_net"]]
            badges += ' <span class="badge %s">政策 %d</span>' % (_pc, r["policy_n"])
        if r.get("option_due"):
            badges += ' <span class="badge b-over">期权到期 %d</span>' % r["option_due"]
        if r.get("driver_pending"):
            badges += (' <span class="badge b-rich" title="%s">%d 项因子未取数</span>'
                       % (r.get("driver_pending_title", ""), r["driver_pending"]))
        links = ""
        for sl in r["slots"]:
            links += ('<a class="btn" href="%s">%s<span>%s · %dKB</span></a>'
                      % (sl["href"], sl["label"], sl["mtime"], sl["kb"]))
        if not links:
            links = '<span class="empty">暂无报告（定时任务尚未运行）</span>'
        if r["last_conclusion"]:
            concl = ('<div class="concl"><b>%s</b>　%s<i>%s</i></div>'
                     % (r["last_action"], r["last_conclusion"], r["last_time"]))
        else:
            concl = '<div class="concl"><i>暂无分析记录</i></div>'
        if r["shares"]:
            pos = ("底仓 %s · 波段 %s · 可用 %s"
                   % (format(r["core_shares"], ","), format(r["swing_shares"], ","),
                      format(int(r["swing_cash"]), ",") if r["swing_cash"] is not None else "—"))
        else:
            pos = "空仓"
        cards.append("\n".join([
            '  <section class="card" data-type="%s" data-tags="%s">'
            % (r["type_key"], ",".join(r["tags"])),
            '    <div class="ch"><h2>%s <span class="code">%s</span></h2>'
            % (r["name"], r["code"].upper()),
            '      <div class="pos">持仓 %s　·　驱动因子 %d 个</div></div>' % (pos, r["drivers"]),
            '    <div class="badges">%s</div>' % badges,
            '    %s' % concl,
            '    <div class="links">%s</div>' % links,
            '  </section>',
        ]))
    return (INDEX_SHELL.replace("<!--CHIPS-->", chip_html)
                       .replace("<!--CARDS-->", "\n".join(cards))
                       .replace("__TIME__", datetime.now().strftime("%Y-%m-%d %H:%M")))


def cmd_lint_report(a):
    """检查生成的报表 HTML 是否有结构性错误（这类错误不会报错，只会让样式静默失效）。"""
    code = normalize_code(a.code)
    suffix = {"deep": "deep", "morning": "open", "afternoon": "close"}[a.session]
    path = REPORTS / ("%s-%s.html" % (code, suffix))
    if not path.exists():
        die(f"报告不存在：{path}（先 new-report 拿路径并写入内容）")
    src = path.read_text(encoding="utf-8")
    issues = []

    # 1) <style> 与 :root 之间只能是空白 —— 最常见、最隐蔽的错误
    if "<style>" in src:
        i = src.index("<style>")
        j = src.find(":root{", i)
        if j == -1:
            issues.append({"level": "error", "msg": "找不到 :root 变量定义：整份报表的配色会全部失效"})
        else:
            junk = src[i + 7:j]
            if junk.strip():
                issues.append({"level": "error",
                               "msg": "style 标签与 :root 之间有 %d 字符杂文本，会把 :root 规则整条吞掉，"
                                      "导致所有 CSS 变量失效（表现：页头白字白底）。"
                                      "多半是复制模板时切在了注释中间" % len(junk.strip()),
                               "sample": junk.strip()[:60]})
    # 2) style 标签是否成对
    if src.count("<style>") != src.count("</style>"):
        issues.append({"level": "error", "msg": "style 标签未成对：%d 开 / %d 闭"
                       % (src.count("<style>"), src.count("</style>"))})
    # 3) 注释是否闭合
    if src.count("<!--") != src.count("-->"):
        issues.append({"level": "warn", "msg": "HTML 注释未闭合：%d 开 / %d 闭"
                       % (src.count("<!--"), src.count("-->"))})
    # 4) var(--x) 是否都有定义
    defined = set(re.findall(r"(--[a-zA-Z0-9-]+)\s*:", src))
    used = set(re.findall(r"var\((--[a-zA-Z0-9-]+)\)", src))
    missing = sorted(used - defined)
    if missing:
        issues.append({"level": "error", "msg": "以下变量被使用但未定义，相关样式会失效：%s"
                       % "、".join(missing)})
    # 5) 关键区块是否都在
    for h2 in ("驱动因子", "公司性质", "结论"):
        if h2 not in src:
            issues.append({"level": "warn", "msg": "缺少「%s」区块（SKILL 要求必填）" % h2})

    # 6) 口径检查：这类错误不会让页面坏掉，只会让结论错，浏览器永远发现不了
    for pat, msg in (
        (r"单笔[^<]{0,8}1/3", "单笔上限仍写 1/3。现行规则无通道比例上限（A/B=可用全额、C=1/2）"),
        (r"单笔[^<]{0,8}1/4", "单笔上限仍写 1/4。同上，C 通道为可用 1/2"),
        (r"每\s*10\s*个交易日", "C 通道节奏写成「每 10 个交易日一轮」。应为「价格每再下移 5% 加一轮」（纯价格驱动）"),
        (r"可加\s*4,?825", "「可加 4,825」是把低估区额度错套到合理区。合理区底仓已建满时可加为 0，见 calc.matrix.room_amount"),
        # 底仓与波段是两套账，混用进场条件是最严重的口径错误
        (r"波段[^<]{0,30}(room_amount|可加额度|矩阵额度)",
         "把底仓额度（matrix.room_amount）用到波段上。波段额度看 calc.swing.buy_power，两套账不共享"),
        (r"波段[^<]{0,40}(等|跌到|回落)[^<]{0,12}(core_entry|建仓窗口|建仓价|低估区)",
         "要求波段买入落在建仓价位。波段买入不受 deep/entry/zone_cap 约束，只由卖出价÷1.05 反推"),
        (r"波段[^<]{0,20}只(能|可|允许)先卖后买",
         "写成「波段只能先卖后买」。先买后卖同样合法（--swing-open / --swing-exit），判据是有没有写下来的出口"),
        # 波段是独立资金池，不存在 2/3 界，也不参与任何预算刻度
        (r"波段[^<]{0,50}(越|破|超过|突破)[^<]{0,12}(2/3|三分之二)",
         "把底仓的 2/3 界套到波段上。波段就是高抛低吸，不设预算刻度；要盯的是「这笔有没有按计划卖掉」"),
        (r"波段[^<]{0,40}预算使用度",
         "用「预算使用度」描述波段。该指标只属于底仓，波段只看可用资金与 fair_high÷1.05"),
        (r"波段[^<]{0,40}(买后|占预算)[^<]{0,16}\d{2}\.\d{2}\s*%",
         "给波段报「买后占预算百分比」。波段不输出任何预算百分比"),
        # 分桶只问底仓，波段仓 = 总持仓 − 底仓，无比例上限
        # （该条单独判，因为报告里可能引用历史上的比例说法，不能一律误报）
        (r"(cap_shares|swing_cap_shares|cap_pct_of_core)",
         "字段名不在当前 schema 中（cap_shares / swing_cap_shares / cap_pct_of_core）。"
         "改用 sellable_shares（可卖）/ can_open_shares（可买）/ buy_first_max_price（价格闸门）"),
    ):
        m = re.search(pat, src)
        if m:
            issues.append({"level": "error", "msg": msg, "sample": m.group(0)[:40]})

    # 7) 波段规模不得写成「≤ 底仓 35%」。单独判，避免把合法引述误报为错误
    for _m in re.finditer(r"波段[^<]{0,16}(?:规模|仓位?)[^<]{0,16}(?:≤|<|不超过|上限)[^<]{0,14}35", src):
        _ctx = src[_m.start():_m.start() + 90]
        if ("取消" in _ctx) or ("已删" in _ctx) or ("作废" in _ctx):
            continue
        issues.append({"level": "error",
                       "msg": "波段规模仍写「≤ 底仓 35%」。该比例上限不存在："
                              "波段仓 = 总持仓 − 底仓（自动派生），规模由持仓结构与可用资金决定",
                       "sample": _m.group(0)[:40]})
        break

    errs = [x for x in issues if x["level"] == "error"]

    out({"ok": not errs, "code": code, "session": a.session, "path": str(path),
         "errors": len(errs), "warnings": len(issues) - len(errs),
         "issues": issues,
         "verdict": "通过" if not errs else "必须先修掉 error 再交付"})


def cmd_selfcheck(a):
    """
    交付前一致性自检。它拦的都是同一类错误（C 通道漏校验底仓、
    「可用资金」被当成「可加额度」、报告照抄过期口径）——都属于「文档写了、代码没做、
    报告照抄」三件事脱节，人眼看不出来，只能靠断言拦住。
    """
    data = load()
    holdings = data.get("holdings") or {}
    if not holdings:
        out({"ok": True, "note": "尚未配置持仓，无需自检", "checks": []})
        return
    last_px = {}
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("code") and rec.get("price"):
                last_px[rec["code"]] = float(rec["price"])
    checks = []

    def ck(code, name, ok, detail=""):
        checks.append({"code": code, "check": name, "pass": bool(ok), "detail": detail})

    _ra, _rg = regime_of(data)
    for code, h in holdings.items():
        v = h.get("valuation") or {}
        budget = float(h.get("budget") or 0)
        basis = float(h.get("shares") or 0) * float(h.get("cost") or 0)
        avail = budget - basis
        try:
            dv, ce, fh, ov = [float(v.get(k)) for k in ("deep_value_price", "core_entry_price",
                                                        "fair_high_price", "overvalued_above_price")]
            ck(code, "四锚严格递增", dv < ce < fh < ov, "%.2f < %.2f < %.2f < %.2f" % (dv, ce, fh, ov))
        except (TypeError, ValueError):
            ck(code, "四锚严格递增", False, "四锚缺失或非数值：先跑 option + set-thesis")
        # 锚复评触发器：写错因子名等于没设防，必须拦在交付前
        _trig = [x for x in (v.get("reanchor_triggers") or []) if isinstance(x, dict)]
        if _trig:
            _known = {it.get("name") for it in (h.get("drivers") or {}).get("items") or []}
            _bad = []
            for t in _trig:
                tid = t.get("id") or t.get("desc") or "?"
                if t.get("type") == "event":
                    if not t.get("due_date"):
                        _bad.append("%s 事件型缺 due_date" % tid)
                    continue
                for c in (t.get("all_of") or [t]):
                    if c.get("driver") not in _known:
                        _bad.append("%s 引用未登记因子「%s」" % (tid, c.get("driver")))
                    if c.get("op", ">=") not in REANCHOR_OPS:
                        _bad.append("%s op=%r 非法" % (tid, c.get("op")))
                if t.get("reanchor", "all") not in REANCHOR_TARGETS:
                    _bad.append("%s reanchor=%r 非法" % (tid, t.get("reanchor")))
            ck(code, "锚复评触发器结构合法（因子名已登记 / op 与目标锚合法）",
               not _bad, "；".join(_bad) if _bad else "%d 条已登记" % len(_trig))
            _armed = sum(1 for t in _trig
                         if t.get("type") == "event"
                         or all((c.get("observed") is not None)
                                for c in (t.get("all_of") or [t])))
            ck(code, "锚复评触发器已回填读数（否则只是文档、不是闸门）",
               _armed == len(_trig),
               "%d/%d 条已武装；未回填的用 set-reanchor --observe id=数值" % (_armed, len(_trig)))
        try:
            bl, bm, bh = [float(v.get(k)) for k in ("base_low_price", "base_mid_price", "base_high_price")]
            ck(code, "基础价值三档递增", bl <= bm <= bh, "%.2f / %.2f / %.2f" % (bl, bm, bh))
        except (TypeError, ValueError):
            ck(code, "基础价值三档递增", False, "基础价值缺失")
        _pct = ((v.get("option_value") or {}).get("pct_of_base_mid"))
        ck(code, "期权占比未超 30% 上限", _pct is None or float(_pct) <= 30.0001, str(_pct))
        if v.get("deep_value_price") and budget > 0:
            base_built = basis > budget / 3 + 1e-6
            px = last_px.get(code)
            if px:
                ep = build_entry_plan(v, px, max(avail, 0), h.get("lot_size") or 1,
                                      h.get("quality_score"), _ra, _ra,
                                      resolve_profile(data, h)["entry_zone_ratio"], budget)
                te = ep["time_entry"]
                ck(code, "底仓已建满时 C 通道必须关闭",
                   (not base_built) or (not te.get("allowed")),
                   "allowed=%s base_built=%s 已投=%.2f%%" % (te.get("allowed"), base_built,
                                                            basis / budget * 100 if budget else 0))
                m = matrix_advice(zone_of(v, px), budget, basis, max(avail, 0))
                ck(code, "matrix 额度与动作不矛盾",
                   not (m.get("room_amount") == 0 and "加至" in (m.get("action") or "")),
                   "%s / room=%.2f" % (m.get("action"), m.get("room_amount") or 0))
                # 波段与底仓是两套账，不得混用进场条件
                lsz = int(h.get("lot_size") or 1)
                sw = swing_advice(dict(h), px, max(avail, 0), lsz,
                                  v.get("fair_high_price"), h.get("quality_score"))
                _core, _sw = normalize_split(dict(h))
                _exp_sell = _sw
                _exp_open = int(max(avail, 0) // (px * lsz)) * lsz if px > 0 else 0
                ck(code, "矩阵额度只作用于底仓（scope=base）",
                   m.get("scope") == "base",
                   "scope=%s" % m.get("scope"))
                ck(code, "波段无建仓价位门槛（不受 deep/entry/zone_cap 约束）",
                   sw.get("entry_gate") is None and sw.get("scope") == "swing",
                   "entry_gate=%s" % sw.get("entry_gate"))
                ck(code, "波段可用资金 = 该标的可用资金（不受矩阵 room 约束）",
                   abs(float(sw.get("buy_power") or 0) - max(avail, 0)) < 0.01,
                   "buy_power=%.2f avail=%.2f room=%.2f"
                   % (float(sw.get("buy_power") or 0), max(avail, 0), float(m.get("room_amount") or 0)))
                ck(code, "波段仓 = 总持仓 − 底仓（自动派生，用户只划底仓）",
                   (sw.get("core_shares") == _core) and (sw.get("swing_shares") == _exp_sell)
                   and (_core + _sw == int(h.get("shares", 0) or 0)),
                   "底仓=%s 波段=%s 持仓=%s" % (sw.get("core_shares"), sw.get("swing_shares"),
                                              h.get("shares")))
                ck(code, "先卖后买的可卖股数 = 波段仓股数（底仓永不出让）",
                   sw.get("sellable_shares") == _exp_sell,
                   "sellable=%s 期望=%s" % (sw.get("sellable_shares"), _exp_sell))
                ck(code, "先买后卖的可买股数只由可用资金决定（无 35% 比例上限）",
                   sw.get("can_open_shares") == _exp_open,
                   "can_open=%s 期望=%s（可用 %.2f / 一手 %.2f）"
                   % (sw.get("can_open_shares"), _exp_open, max(avail, 0), px * lsz))
                _fh = v.get("fair_high_price")
                _exp_bf = round(float(_fh) / 1.05, 4) if _fh else None
                ck(code, "先买后卖的价格闸门 = fair_high ÷ 1.05",
                   sw.get("buy_first_max_price") == _exp_bf
                   and sw.get("buy_first_ok_now") == (bool(_exp_bf) and px <= _exp_bf + 1e-9),
                   "最高买价=%s 现价=%s 可做=%s" % (sw.get("buy_first_max_price"), px,
                                                 sw.get("buy_first_ok_now")))
                ck(code, "波段额度不含任何预算刻度或股数比例上限",
                   not any(k in sw for k in ("amount_pct_of_budget_after", "budget_pct",
                                             "room_amount", "cap_amount",
                                             "cap_shares", "cap_pct_of_core")),
                   "字段：%s" % ",".join(sorted(sw.keys())))
        p = REPORTS / ("%s-deep.html" % code)
        if p.exists():
            s = p.read_text(encoding="utf-8")
            n = s.count('class="kicker"')
            ck(code, "深度报告区块齐全（13）", n >= 13, "kicker=%d" % n)
            bad = re.search(r"单笔[^<]{0,8}1/[34]", s)
            ck(code, "深度报告无过期单笔口径", bad is None, bad.group(0) if bad else "ok")
        else:
            ck(code, "深度报告存在", False, str(p))
    fails = [c for c in checks if not c["pass"]]
    out({"ok": not fails, "total": len(checks), "failed": len(fails), "checks": checks,
         "verdict": "全部通过" if not fails else "有 %d 项不一致，先修再交付" % len(fails)})


def cmd_index(a):
    """生成 reports/index.html：关掉任务结果后仍可再打开的固定入口。"""
    data = load()
    REPORTS.mkdir(parents=True, exist_ok=True)
    last = {}
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            last[rec.get("code")] = rec
    rows = []
    for code, h in data["holdings"].items():
        ct = h.get("company_type") or {}
        dv = summarize_drivers(normalize_drivers(h))
        slots = []
        for slot, label in REPORT_SLOTS:
            p = REPORTS / ("%s-%s.html" % (code, slot))
            if p.exists():
                slots.append({"slot": slot, "label": label, "href": p.name, "uri": file_uri(p),
                              "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M"),
                              "kb": max(1, p.stat().st_size // 1024)})
        _v = h.get("valuation") or {}
        _rev_raw = _v.get("revised_at") or _v.get("set_on") or ""
        _rev_age = None
        try:
            _rev_age = (datetime.now() - datetime.strptime(
                str(_rev_raw)[:19] if len(str(_rev_raw)) > 10 else str(_rev_raw)[:10] + " 00:00:00",
                "%Y-%m-%d %H:%M:%S")).days
        except (ValueError, TypeError):
            _rev_age = None
        prof = resolve_profile(data, h)
        core_sh, swing_sh = normalize_split(h)
        basis = int(h.get("shares", 0)) * float(h.get("cost", 0) or 0)
        bg = float(h.get("budget", 0) or 0)
        lr = last.get(code) or {}
        pend = []
        for x in dv.get("stale") or []:
            pend.append("%s（%d 天未更新）" % (x["name"], x["days"]))
        for x in dv.get("never_updated") or []:
            pend.append("%s（从未取数）" % x)
        rows.append({"code": code, "name": h.get("name") or code,
                     "driver_pending": len(pend),
                     "driver_pending_title": "；".join(pend),
                     "type": ct.get("label") or "未判定",
                     "style": prof["entry_style_label"],
                     "style_src": prof["source"]["entry_style"],
                     "zone_ratio": prof["entry_zone_ratio"],
                     "core_shares": core_sh, "swing_shares": swing_sh,
                     "swing_cash": round(max(bg - basis, 0), 2) if bg else None,
                     "shares": int(h.get("shares", 0)),
                     "type_key": ct.get("primary") or "none",
                     "tags": ct.get("tags") or [], "quality": h.get("quality_score"),
                     "shares": h.get("shares", 0), "cost": h.get("cost", 0),
                     "drivers": dv.get("count", 0),
                     "anchor_rev": str(_rev_raw)[:10] or None,
                     "anchor_stale": bool(_rev_age is not None and _rev_age > 120),
                     "option_due": int(((h.get("valuation") or {}).get("option_value") or {})
                                       .get("due_count") or 0),
                     "policy_n": len(dv.get("policy_active") or []),
                     "policy_net": _policy_net(dv.get("policy_active") or []),
                     "driver_stale": len(dv.get("stale") or []) + len(dv.get("never_updated") or []),
                     "last_time": lr.get("time", ""), "last_conclusion": lr.get("conclusion", ""),
                     "last_action": lr.get("action", ""), "slots": slots})
    path = REPORTS / "index.html"
    path.write_text(_render_index(rows), encoding="utf-8")
    out({"ok": True, "path": str(path), "uri": file_uri(path),
         "markdown_link": "[报告索引](%s)" % file_uri(path),
         "holdings": len(rows)})


ACTION_TYPES = ("buy", "sell", "hold")


def _infer_action_type(text):
    """从自由文本推断动作类型；否定说法优先判为 hold。仅在没有显式 --action-type 时使用。"""
    t = str(text or "")
    if any(w in t for w in ("不动", "持有", "不买", "不卖", "观望", "暂停", "跳过")):
        return "hold"
    if any(w in t for w in ("卖出", "减仓", "清仓", "止损")):
        return "sell"
    if any(w in t for w in ("买入", "加仓", "建仓", "补仓")):
        return "buy"
    if "卖" in t:
        return "sell"
    if "买" in t:
        return "buy"
    return "hold"


def cmd_log(a):
    """
    写入当日分析记录（只埋点，不做统计）。
    同一天 + 同一标的 + 同一任务类型只保留最新一条：重复触发会覆盖，不堆积。
    同时自动抓取当时的区间与四锚快照，供日后复盘比对（现在不做分析）。
    """
    data = load()
    code, h = get_holding(data, a.code)
    v = h.get("valuation") or {}
    anchors = {k: v.get(k) for k in ("deep_value_price", "core_entry_price",
                                     "fair_high_price", "overvalued_above_price")}
    p = float(a.price) if a.price is not None else None
    rec = {
        "time": now(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "code": code,
        "session": a.session,
        "price": p,
        "zone": zone_of(v, p) if p else None,
        "anchors": anchors,
        "action_type": a.action_type or _infer_action_type(a.action),
        "action": a.action,
        "target_price": a.target_price,
        "expected": a.expected,
        "volprice": a.volprice,
        "conclusion": a.conclusion,
        "report": a.report,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows, dropped = [], 0
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            same = (r.get("code") == code and r.get("session") == a.session
                    and (r.get("date") or str(r.get("time") or "")[:10]) == rec["date"])
            if same:
                dropped += 1
                continue
            rows.append(r)
    rows.append(rec)
    tmp = LOG.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    os.replace(tmp, LOG)
    out({"ok": True, "logged": rec, "replaced_same_day": dropped,
         "total_records": len(rows),
         "note": "同日同任务只保留最新一条；区间与四锚已随记录留档，供日后复盘比对（当前不做分析）"})


def cmd_last_log(a):
    if not LOG.exists():
        out({"ok": True, "count": 0, "records": []})
        return
    code = normalize_code(a.code) if a.code else None
    rows = []
    with open(LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if code is None or r.get("code") == code:
                rows.append(r)
    tail = rows[-a.n:]
    out({"ok": True, "count": len(tail), "records": tail,
         "note": "仅用于审计回看，不参与本次决策（无状态原则）"})


def main():
    p = argparse.ArgumentParser(description="价值投资持仓与决策辅助工具")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("path").set_defaults(fn=cmd_path)

    s = sp.add_parser("show"); s.add_argument("--code"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sp.add_parser("upsert")
    s.add_argument("--code", required=True); s.add_argument("--name"); s.add_argument("--market", choices=["A", "HK", "US"])
    s.add_argument("--budget", type=float, help="可用于该标的的总资金（含已投入部分）")
    s.add_argument("--shares", type=int, help="当前持股数")
    s.add_argument("--cost", type=float, help="最新持仓成本价（每股）")
    s.add_argument("--swing-shares", type=int, help="划给波段操作的股数（其余为底仓，默认 0）")
    s.add_argument("--core-shares", type=int, help="底仓股数（固定不动的那部分，波段仓自动派生）")
    s.add_argument("--lot-size", type=int, help="每手股数（港股必填）")
    s.add_argument("--currency")
    s.set_defaults(fn=cmd_upsert)

    s = sp.add_parser("set-thesis"); s.add_argument("--code", required=True); s.add_argument("--file", required=True)
    s.set_defaults(fn=cmd_set_thesis)

    s = sp.add_parser("set-prefs")
    s.add_argument("--regret-aversion", choices=["low", "mid", "high"],
                   help="踏空厌恶：high 允许合理区内「时间换价格」建底仓")
    s.add_argument("--entry-style", choices=sorted(STYLE_ENUM),
                   help="建仓风格：left 左侧摊薄 / swing 纪律波段 / right 右侧确认 / mixed 混合")
    s.add_argument("--drawdown-tolerance", type=float,
                   help="单标的可承受最大浮亏，如 0.25 表示 25%%")
    s.add_argument("--entry-zone-ratio", type=float,
                   help="时间换价格窗口占合理区的比例（0.33=下三分之一，0=只在底仓线，1=整个合理区）")
    s.add_argument("--profile-note", help="画像问卷的原始答案摘要")
    s.set_defaults(fn=cmd_set_prefs)

    s = sp.add_parser("option"); s.add_argument("--code", required=True); s.add_argument("--file", required=True)
    s.add_argument("--replace", action="store_true", help="全量替换（默认按 name 合并）")
    s.set_defaults(fn=cmd_option)

    sp.add_parser("index").set_defaults(fn=cmd_index)

    sp.add_parser("selfcheck").set_defaults(fn=cmd_selfcheck)

    s = sp.add_parser("lint-report")
    s.add_argument("--code", required=True)
    s.add_argument("--session", required=True, choices=["deep", "morning", "afternoon"])
    s.set_defaults(fn=cmd_lint_report)

    s = sp.add_parser("plan-swing")
    s.add_argument("--code", required=True)
    s.add_argument("--price", type=float, required=True)
    s.add_argument("--ratio", type=float, help="划给波段仓的比例，0–1（如 0.3 表示 30%%）")
    s.add_argument("--budget", type=float, help="覆盖该标的预算（默认读持仓档）")
    s.set_defaults(fn=cmd_plan_swing)

    s = sp.add_parser("set-profile")
    s.add_argument("--code", required=True)
    s.add_argument("--entry-style", choices=sorted(STYLE_ENUM), help="该标的的建仓风格")
    s.add_argument("--entry-zone-ratio", type=float, help="该标的的建仓窗口比例 0–1")
    s.add_argument("--swing-shares", type=int, help="该标的的波段仓股数（浮动部分）")
    s.add_argument("--core-shares", type=int, help="该标的的底仓股数（固定不动，推荐用这个）")
    s.add_argument("--swing-quality-override", choices=["on", "off"], dest="swing_quality_override",
                   help="显式覆盖「波段要求质地≥8」这一条门槛（on/off）；覆盖必须写进 note 与报告")
    s.add_argument("--note", help="该标的的执行策略备注")
    s.set_defaults(fn=cmd_set_profile)

    s = sp.add_parser("set-drivers")
    s.add_argument("--code", required=True); s.add_argument("--file", required=True)
    s.set_defaults(fn=cmd_set_drivers)

    s = sp.add_parser("set-reanchor")
    s.add_argument("--code", required=True)
    s.add_argument("--file", help="触发器数组（按 id 合并；不给则只做 --observe / --clear）")
    s.add_argument("--observe", action="append", metavar="ID=VALUE",
                   help="回填某条触发器的当前读数（同时记 observed_date=今日），可重复")
    s.add_argument("--clear", action="store_true", help="清空全部触发器")
    s.set_defaults(fn=cmd_set_reanchor)

    s = sp.add_parser("set-type")
    s.add_argument("--code", required=True)
    s.add_argument("--primary", required=True, choices=sorted(TYPE_ENUM))
    s.add_argument("--model", choices=sorted(MODEL_ENUM), help="估值范式，默认由形态推导")
    s.add_argument("--tags", help="补充标签，逗号分隔，最多 2 个")
    s.add_argument("--basis", required=True, help="判定依据：至少两条量化证据")
    s.add_argument("--counter", required=True, help="反方证据：一条支持相反分类的证据")
    s.add_argument("--user-view", help="用户与数据判定不一致时的用户看法（不改写数据结论）")
    s.add_argument("--reason", help="改判形态时的结构性理由（形态发生变化时必填）")
    s.add_argument("--confirmed", action="store_true", help="用户已确认该性质")
    s.set_defaults(fn=cmd_set_type)

    s = sp.add_parser("remove"); s.add_argument("--code", required=True); s.set_defaults(fn=cmd_remove)
    sp.add_parser("markets").set_defaults(fn=cmd_markets)

    s = sp.add_parser("calc"); s.add_argument("--code", required=True); s.add_argument("--price", required=True, type=float)
    s.add_argument("--price-time", help="行情时间戳"); s.set_defaults(fn=cmd_calc)

    s = sp.add_parser("volprice"); s.add_argument("--file", required=True)
    s.add_argument("--market", choices=["A", "HK", "US"], help="市场（决定全日交易分钟数）")
    s.add_argument("--elapsed", type=int, help="当日已交易分钟数（A股14:30=210，港股15:30=300）")
    s.set_defaults(fn=cmd_volprice)

    s = sp.add_parser("new-report")
    s.add_argument("--code", required=True)
    s.add_argument("--session", required=True, choices=["deep", "morning", "afternoon"])
    s.add_argument("--keep-legacy", action="store_true", help="不把旧的按日期命名的同槽位文件挪进 _archive")
    s.set_defaults(fn=cmd_new_report)

    s = sp.add_parser("check-order")
    s.add_argument("--code", required=True); s.add_argument("--side", required=True, choices=["buy", "sell", "hold"])
    s.add_argument("--shares", type=int, default=0); s.add_argument("--limit", type=float, default=0)
    s.add_argument("--prev-close", type=float, default=1)
    s.add_argument("--sell-reason", choices=["fundamental", "overvalued", "swing", "other"], default="other")
    s.add_argument("--buyback-price", type=float, help="波段卖出时的计划买回价（swing 必填）")
    s.add_argument("--swing-buyback", action="store_true",
                   help="本次为波段买回：豁免建仓通道校验，改校验「≤卖价×0.95、≤fair_high、≤上轮卖出股数」")
    s.add_argument("--swing-sell-price", type=float, help="上一轮波段卖出价（波段买回必填）")
    s.add_argument("--swing-sold-shares", type=int, help="上一轮波段卖出股数（可选，用于校验买回不超过卖出）")
    s.add_argument("--swing-open", action="store_true",
                   help="本次为波段建仓（先买后卖）：不发建仓通道校验，改校验「必须给出计划卖出价、"
                        "卖出价≥买价×1.05、≤fair_high、波段仓不超底仓 35%%、总投入≤budget」")
    s.add_argument("--target-sell-price", type=float, help="波段建仓的计划卖出价（--swing-open 必填）")
    s.add_argument("--swing-exit", action="store_true",
                   help="本次为波段兑现（先买后卖的第二腿）：豁免买回价，改校验「卖出价 ≥ 买入价×1.05」")
    s.add_argument("--swing-entry-price", type=float,
                   help="先买后卖那笔波段的买入价（--swing-exit 必填）")
    s.add_argument("--option-reviewed", action="store_true", help="已评估上行期权（无可定价期权也须显式传入）")
    s.add_argument("--anchor-reviewed", action="store_true",
                   help="锚复评触发器已越界/到期时，已重跑四锚并确认本次买点用的是新锚")
    s.add_argument("--time-entry", action="store_true", help="本次为合理区内「时间换价格」底仓")
    s.add_argument("--fundamental-ok", action="store_true")
    s.add_argument("--valuation-ok", action="store_true", help="无作用：估值区由脚本按锚自动判定")
    s.add_argument("--data-fresh", action="store_true")
    s.set_defaults(fn=cmd_check_order)

    s = sp.add_parser("log")
    s.add_argument("--code", required=True); s.add_argument("--session", required=True,
                                                            choices=["deep", "morning", "afternoon", "adhoc"])
    s.add_argument("--price", type=float); s.add_argument("--conclusion", required=True)
    s.add_argument("--action", default="不动"); s.add_argument("--volprice"); s.add_argument("--report")
    s.add_argument("--action-type", choices=list(ACTION_TYPES),
                   help="动作类型（buy/sell/hold）；不填则从 --action 文本推断")
    s.add_argument("--target-price", type=float, help="建议的目标价/限价（无则留空）")
    s.add_argument("--expected", choices=["up", "down", "flat"],
                   help="对后续方向的预期（可选，供日后复盘比对）")
    s.set_defaults(fn=cmd_log)

    s = sp.add_parser("last-log"); s.add_argument("--code"); s.add_argument("--n", type=int, default=3)
    s.set_defaults(fn=cmd_last_log)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
