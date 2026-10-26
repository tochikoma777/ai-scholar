"""L5a：静态 HTML 单文件导出器（零依赖、自包含、双击可开）。

★ 内容边界（负责人 2026-10-25 定）：**只读 public 数据**（每日报告 / 记分板 /
  DB 论文元数据），**绝不读取 vault** —— 个人笔记不进 public 产物。

★ 红线 11：只展示分项分（热度 / 跨源 / 主题），**不合成单一总分**。

★ 输出只由输入决定：不写生成时间、不读当前时间 ⇒ 重跑逐字节一致（`--check` 可断言）。
  数据日期来自最新报告，而不是 `today()`。

★ 自包含：CSS 内联、无 `<script>` / `<link>` / CDN / 外链字体；唯一外链是论文原始链接。
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ai_scholar.export.base import ExportContext, ExportDoc, Exporter
from ai_scholar.store.models import Paper
from ai_scholar.store.wanted import want_command
from ai_scholar.utils.errors import ConfigError

# ★ 首版体积上限（负责人定 ≤ 5 MB）：只内联摘要级文本，不内联论文正文/abstract
MAX_BYTES = 5 * 1024 * 1024

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1c1c1c; --muted: #6b6b6b; --line: #e3e3e0;
  --card: #ffffff; --accent: #2f6f4e; --warn: #8a5a00; --bad: #a33;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #16181a; --fg: #e8e8e6; --muted: #a2a2a0; --line: #2f3336;
           --card: #1e2124; --accent: #7fc7a4; --warn: #e0b062; --bad: #e08b8b; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC",
        "PingFang SC", "Microsoft YaHei", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 24px 20px 56px; }
header { max-width: 980px; margin: 0 auto; padding: 32px 20px 0; }
h1 { margin: 0 0 4px; font-size: 28px; letter-spacing: .02em; }
h2 { margin: 36px 0 12px; font-size: 20px; border-bottom: 1px solid var(--line);
     padding-bottom: 6px; }
h3 { margin: 0 0 6px; font-size: 16px; }
p, li { margin: 6px 0; }
.sub { color: var(--muted); font-size: 14px; margin: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 12px; margin-top: 14px; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 12px 14px; }
.stat .n { font-size: 24px; font-weight: 650; }
.stat .k { color: var(--muted); font-size: 13px; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
.paper { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: 14px 16px; margin-top: 10px; }
.paper a { color: inherit; text-decoration: none; border-bottom: 1px solid var(--line); }
.paper a:hover { color: var(--accent); }
.meta { color: var(--muted); font-size: 13px; }
.scores { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; font-size: 13px; }
.scores span { color: var(--muted); }
.scores b { color: var(--fg); font-variant-numeric: tabular-nums; }
.want-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 10px; }
.want { font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 999px;
        border: 1px solid var(--line); background: var(--card); color: var(--accent);
        cursor: pointer; }
.want:hover { border-color: var(--accent); }
.want-row code { font-size: 12px; color: var(--muted); }
.controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 10px; margin: 14px 0 8px; align-items: end; }
.controls label { display: flex; flex-direction: column; gap: 4px;
                  font-size: 12px; color: var(--muted); }
.controls input, .controls select, .controls button {
  font: inherit; font-size: 14px; padding: 6px 8px; color: var(--fg);
  background: var(--card); border: 1px solid var(--line); border-radius: 7px; }
.controls button { cursor: pointer; color: var(--muted); }
.controls button:hover { color: var(--accent); border-color: var(--accent); }
.count { color: var(--muted); font-size: 13px; margin: 4px 0 0; }
[hidden] { display: none !important; }
.tag { display: inline-block; font-size: 12px; color: var(--accent);
       border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px; margin-left: 6px; }
.hit { color: var(--accent); } .miss { color: var(--bad); } .insufficient { color: var(--warn); }
.empty { color: var(--muted); font-style: italic; }
footer { max-width: 980px; margin: 0 auto; padding: 0 20px 40px; color: var(--muted);
         font-size: 13px; border-top: 1px solid var(--line); padding-top: 14px; }
@media print { .paper { break-inside: avoid; } .controls, .count, noscript { display: none; } }
""".strip()

# ★ L5b：筛选 / 排序 / 搜索全部在本地 DOM 上完成 —— 不发请求、不引第三方库。
#   数据已由服务端渲染成卡片（无 JS 也能读）；这里只做渐进增强。
_JS = """
(function () {
  var list = document.getElementById("paper-list");
  if (!list) return;
  var cards = Array.prototype.slice.call(list.querySelectorAll(".paper"));
  var q = document.getElementById("q");
  var group = document.getElementById("group");
  var tier = document.getElementById("tier");
  var date = document.getElementById("date");
  var sort = document.getElementById("sort");
  var count = document.getElementById("count");
  var empty = document.getElementById("empty");
  var reset = document.getElementById("reset");

  function matches(card) {
    var d = card.dataset;
    var needle = q.value.trim().toLowerCase();
    if (needle && d.search.indexOf(needle) === -1) return false;
    if (group.value) {
      var groups = [];
      try { groups = JSON.parse(d.groups || "[]"); } catch (e) { groups = []; }
      if (groups.indexOf(group.value) === -1) return false;
    }
    if (tier.value) {
      if (tier.value === "none") { if (d.tier) return false; }
      else if (d.tier !== tier.value) return false;
    }
    if (date.value && d.date !== date.value) return false;
    return true;
  }

  function apply() {
    var shown = 0;
    cards.forEach(function (card) {
      var ok = matches(card);
      card.hidden = !ok;
      if (ok) shown += 1;
    });
    cards.sort(function (a, b) {
      if (sort.value === "date") {
        return (b.dataset.date || "").localeCompare(a.dataset.date || "");
      }
      var av = parseFloat(a.dataset[sort.value]);
      var bv = parseFloat(b.dataset[sort.value]);
      if (isNaN(av)) av = -Infinity;
      if (isNaN(bv)) bv = -Infinity;
      return bv - av;
    });
    cards.forEach(function (card) { list.appendChild(card); });
    count.textContent = "显示 " + shown + " / " + cards.length + " 篇";
    empty.hidden = shown > 0;
  }

  q.addEventListener("input", apply);
  [group, tier, date, sort].forEach(function (el) {
    el.addEventListener("change", apply);
  });
  reset.addEventListener("click", function () {
    q.value = "";
    group.value = "";
    tier.value = "";
    date.value = "";
    sort.value = "date";
    apply();
    q.focus();
  });
  apply();
})();

// ★ L7-6「点读入口」：复制「标记想读」命令到剪贴板（纯本地，零请求）。
//   无 JS 时按钮旁的 <code> 里就是同一条命令，手动复制即可。
(function () {
  document.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest(".want") : null;
    if (!btn) return;
    var cmd = btn.getAttribute("data-cmd") || "";
    var done = function (ok) {
      var old = btn.getAttribute("data-label") || btn.textContent;
      btn.setAttribute("data-label", old);
      btn.textContent = ok ? "已复制 ✓" : "请手动复制";
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(cmd).then(function () { done(true); },
                                             function () { done(false); });
    } else {
      done(false);
    }
  });
})();
""".strip()


@dataclass(frozen=True)
class SitePaper:
    """页面展示用论文（★ 只有元数据与摘要级文本，不含正文/abstract）。"""

    paper_id: str
    title: str
    url: str | None = None
    first_seen: str = ""
    tier: int | None = None
    groups: tuple[str, ...] = ()
    one_line: str | None = None
    why: str | None = None
    heat: float | None = None
    cross_source: float = 0.0
    topic: float = 0.0


@dataclass(frozen=True)
class SitePrediction:
    claim: str
    status: str
    due_at: str
    observed: int | None = None
    threshold: int | None = None


@dataclass(frozen=True)
class SiteCalibrationRow:
    tier: int
    n: int
    mean_citations: float
    median_citations: float


@dataclass(frozen=True)
class SiteTrendRow:
    date: str
    new_today: int
    hits: int
    verified: int


@dataclass
class SiteData:
    """一整页所需的数据（由 `pipeline/site.py` 组装，导出器只负责渲染）。"""

    date: str
    total_papers: int = 0
    new_today: int = 0
    hits_today: int = 0
    # ★ L6-2：当日实际 LLM 成本（off 档为 0.0，不是 None）
    cost_cny: float = 0.0
    open: int = 0
    hit: int = 0
    miss: int = 0
    insufficient: int = 0
    hit_rate: float | None = None
    opinions: int = 0
    predictions_new: list[SitePrediction] = field(default_factory=list)
    predictions_due: list[SitePrediction] = field(default_factory=list)
    predictions_verified: list[SitePrediction] = field(default_factory=list)
    calibration_rows: list[SiteCalibrationRow] = field(default_factory=list)
    spearman: float | None = None
    calibration_enough: bool = False
    calibration_reason: str = ""
    papers: list[SitePaper] = field(default_factory=list)
    trend: list[SiteTrendRow] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)


def _e(value: object) -> str:
    """所有外部文本（标题 / 组名 / 降级原因）必须转义后再进 HTML。"""
    return html.escape("" if value is None else str(value), quote=True)


def _fmt(value: float | None) -> str:
    """缺失显示 `—`，不把"没数据"写成 0（与 CLI 表格同一原则）。"""
    return "—" if value is None else f"{value:.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _safe_url(url: str | None) -> str | None:
    """★ 只允许 http(s)：DB / 采集源里的字符串不可信，`javascript:` 会变成 XSS。"""
    if not url:
        return None
    parsed = urlparse(url)
    return url if parsed.scheme in ("http", "https") and parsed.netloc else None


def _status_text(status: str) -> tuple[str, str]:
    return {
        "hit": ("✅ 命中", "hit"),
        "miss": ("❌ 未命中", "miss"),
        "insufficient": ("⚠️ 数据不足", "insufficient"),
        "open": ("⏳ 未到期", "insufficient"),
    }.get(status, (status, "insufficient"))


class HtmlExporter(Exporter):
    """把 public 数据渲染成**一个**自包含 HTML 文件。"""

    name = "html"

    # ── Exporter 接口 ──────────────────────────────────────────────────
    def render(self, paper: Paper, context: ExportContext) -> str:
        """单篇导出 = 只含这一篇的最小完整页面（满足 `Exporter.render` 的"完整文件"契约）。

        ★ `context` 刻意不用：HTML 不依赖 vault / 笔记 / 双链。
        """
        item = SitePaper(
            paper_id=paper.id,
            title=paper.title,
            url=paper.url,
            first_seen=(paper.first_seen_at or "")[:10],
            tier=paper.tier,
            one_line=paper.one_line,
            why=paper.why,
        )
        return self.render_site(
            SiteData(
                date=(paper.first_seen_at or "")[:10] or "—",
                total_papers=1,
                papers=[item],
            )
        )

    def render_site(self, data: SiteData) -> str:
        """渲染完整单文件页面（确定性：同一 `SiteData` ⇒ 逐字节一致）。"""
        body = "\n".join(
            [
                self._header(data),
                "<main>",
                self._overview(data),
                self._predictions(data),
                self._scoreboard(data),
                self._calibration(data),
                self._papers(data),
                self._trend(data),
                self._degraded(data),
                "</main>",
                self._footer(),
            ]
        )
        document = (
            "<!doctype html>\n"
            '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>AI Scholar · {_e(data.date)}</title>\n"
            f"<style>\n{_CSS}\n</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
        )
        size = len(document.encode("utf-8"))
        if size > MAX_BYTES:
            raise ConfigError(
                f"HTML 体积 {size / 1024 / 1024:.2f} MB 超过上限 "
                f"{MAX_BYTES / 1024 / 1024:.0f} MB；请减小 `--limit` / `--days`"
                "（★ 不要为压体积删板块）"
            )
        return document

    def site_path(self, out_dir: Path) -> Path:
        return Path(out_dir) / "index.html"

    # ── 板块 ────────────────────────────────────────────────────────────
    @staticmethod
    def _header(data: SiteData) -> str:
        return (
            "<header>\n<h1>AI Scholar</h1>\n"
            f'<p class="sub">数据日期 {_e(data.date)} · 库内 {data.total_papers} 篇 · '
            "只含 public 数据（不含个人笔记与论文正文）</p>\n</header>"
        )

    @staticmethod
    def _overview(data: SiteData) -> str:
        decided = data.hit + data.miss
        stats = [
            ("今日新增", str(data.new_today)),
            ("★ 今日命中", str(data.hits_today)),
            ("已判定推演", str(decided)),
            ("命中率", _pct(data.hit_rate)),
            ("今日 LLM 成本", f"¥{data.cost_cny:.4f}"),
        ]
        cards = "\n".join(
            f'<div class="stat"><div class="n">{_e(value)}</div>'
            f'<div class="k">{_e(key)}</div></div>'
            for key, value in stats
        )
        return (
            '<section id="today">\n<h2>今日概览</h2>\n<div class="grid">\n'
            f"{cards}\n</div>\n</section>"
        )

    @staticmethod
    def _prediction_list(items: list[SitePrediction], *, show_observed: bool) -> str:
        rows: list[str] = []
        for item in items:
            label, css = _status_text(item.status)
            observed = ""
            if show_observed and item.observed is not None:
                observed = f"（观测 {item.observed} / 阈值 {item.threshold}）"
            rows.append(
                f'<li><span class="{css}">{_e(label)}</span> {_e(item.claim)}'
                f'{_e(observed)} <span class="meta">到期 {_e(item.due_at)}</span></li>'
            )
        return "\n".join(rows) if rows else '<li class="empty">（无）</li>'

    def _predictions(self, data: SiteData) -> str:
        blocks = [
            ("今日新增", self._prediction_list(data.predictions_new, show_observed=False)),
            ("今日已验证", self._prediction_list(data.predictions_verified, show_observed=True)),
            ("已到期未验证", self._prediction_list(data.predictions_due, show_observed=False)),
        ]
        parts = ["<h2>今日推演</h2>"]
        for title, content in blocks:
            parts.append(f"<h3>{_e(title)}</h3>\n<ul>\n{content}\n</ul>")
        parts.append(
            f'<p class="meta">观点 {data.opinions} 条（★ 不可证伪，永不进命中率）；'
            "数据不足不进分母。</p>"
        )
        return '<section id="predictions">\n' + "\n".join(parts) + "\n</section>"

    @staticmethod
    def _scoreboard(data: SiteData) -> str:
        rows = [
            ("未到期 / 到期未验证", str(data.open)),
            ("命中 / 未命中", f"{data.hit} / {data.miss}"),
            ("命中率（分母 = 命中 + 未命中）", _pct(data.hit_rate)),
            ("数据不足（不计入分母）", str(data.insufficient)),
            ("观点（不计入统计）", str(data.opinions)),
        ]
        body = "\n".join(f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>" for key, value in rows)
        return (
            '<section id="scoreboard">\n<h2>记分板</h2>\n'
            f"<table>\n<tbody>\n{body}\n</tbody>\n</table>\n"
            '<p class="meta">命中率只由代码验证后填入；`insufficient` 与观点都不进分母。</p>\n'
            "</section>"
        )

    @staticmethod
    def _calibration(data: SiteData) -> str:
        if data.calibration_rows:
            rows = "\n".join(
                f"<tr><td>Tier {row.tier}</td><td>{row.n}</td>"
                f"<td>{row.mean_citations:.2f}</td><td>{row.median_citations:.2f}</td></tr>"
                for row in data.calibration_rows
            )
            table = (
                "<table>\n<thead><tr><th>Tier</th><th>n</th><th>引用均值</th>"
                f"<th>引用中位</th></tr></thead>\n<tbody>\n{rows}\n</tbody>\n</table>"
            )
        else:
            table = '<p class="empty">（暂无成熟论文可校准）</p>'
        spearman = "—" if data.spearman is None else f"{data.spearman:+.3f}"
        conclusion = (
            "样本充足，Spearman 可作参考。"
            if data.calibration_enough
            else f'<span class="insufficient">不下结论</span>：{_e(data.calibration_reason)}'
        )
        return (
            '<section id="calibration">\n<h2>定级校准</h2>\n'
            f"{table}\n"
            f'<p class="meta">Spearman(Tier, 引用)：{_e(spearman)} · {conclusion}</p>\n'
            "</section>"
        )

    def _paper_card(self, item: SitePaper) -> str:
        title = _e(item.title)
        safe_url = _safe_url(item.url)
        if safe_url:
            title = f'<a href="{_e(safe_url)}" rel="noopener noreferrer">{title}</a>'
        meta_bits = [f"first_seen {_e(item.first_seen) or '—'}"]
        if item.tier:
            meta_bits.append(f"Tier {item.tier}")
        tags = "".join(f'<span class="tag">{_e(g)}</span>' for g in item.groups)
        # ★ 筛选/排序只读这些属性；搜索文本在服务端小写化，JS 不再拼 HTML
        search_text = " ".join(
            [item.title, item.one_line or "", item.why or "", *item.groups]
        ).lower()
        groups_json = _e(json.dumps(list(item.groups), ensure_ascii=False))
        heat = "" if item.heat is None else f"{item.heat:.4f}"
        attrs = (
            f'class="paper" data-id="{_e(item.paper_id)}"'
            f' data-date="{_e(item.first_seen)}"'
            f' data-tier="{"" if item.tier is None else item.tier}"'
            f' data-groups="{groups_json}"'
            f' data-search="{_e(search_text)}"'
            f' data-heat="{heat}"'
            f' data-cross="{item.cross_source:.4f}"'
            f' data-topic="{item.topic:.4f}"'
        )
        lines = [
            f"<article {attrs}>",
            f"<h3>{title}{tags}</h3>",
            f'<p class="meta">{" · ".join(meta_bits)}</p>',
        ]
        if item.one_line:
            lines.append(f"<p>{_e(item.one_line)}</p>")
        if item.why:
            lines.append(f'<p class="meta">{_e(item.why)}</p>')
        lines += [
            '<div class="scores">'
            f"<span>热度 <b>{_e(_fmt(item.heat))}</b></span>"
            f"<span>跨源 <b>{item.cross_source:.2f}</b></span>"
            f"<span>主题 <b>{item.topic:.2f}</b></span>"
            "</div>",
        ]
        # ★ L7-6：想读入口 —— 命令写在 data-cmd 与 <code> 里（无 JS 也能手动复制）
        cmd = want_command(item.paper_id)
        lines += [
            '<div class="want-row">'
            f'<button type="button" class="want" data-cmd="{_e(cmd)}">复制想读命令</button>'
            f"<code>{_e(cmd)}</code>"
            "</div>",
            "</article>",
        ]
        return "\n".join(lines)

    @staticmethod
    def _options(values: list[str], *, blank: str = "全部") -> str:
        items = [f'<option value="">{_e(blank)}</option>']
        items += [f'<option value="{_e(v)}">{_e(v)}</option>' for v in values]
        return "".join(items)

    def _papers(self, data: SiteData) -> str:
        head = (
            '<section id="papers">\n<h2>论文（分项分）</h2>\n'
            '<p class="meta">热度 / 跨源 / 主题各自独立，本页只在本批内归一化，不可跨批比较。</p>\n'
        )
        if not data.papers:
            return head + '<p class="empty">（库内暂无论文）</p>\n</section>'

        groups = sorted({g for item in data.papers for g in item.groups})
        dates = sorted({item.first_seen for item in data.papers if item.first_seen}, reverse=True)
        tiers = sorted({str(item.tier) for item in data.papers if item.tier is not None})
        has_untiered = any(item.tier is None for item in data.papers)
        tier_options = self._options(tiers)
        if has_untiered:
            tier_options += '<option value="none">未定级</option>'

        manifest = {
            "version": 1,
            "total": len(data.papers),
            "groups": groups,
            "dates": dates,
            "tiers": [int(t) for t in tiers],
        }
        payload = json.dumps(manifest, ensure_ascii=False).replace("</", "<\\/")
        controls = (
            '<div class="controls" role="search">\n'
            f'<label>搜索<input id="q" type="search" placeholder="标题 / 一句话 / 标签"'
            ' autocomplete="off"></label>\n'
            f'<label>方向<select id="group">{self._options(groups)}</select></label>\n'
            f'<label>Tier<select id="tier">{tier_options}</select></label>\n'
            f'<label>入库日<select id="date">{self._options(dates)}</select></label>\n'
            '<label>排序<select id="sort">'
            '<option value="date">最新</option>'
            '<option value="heat">热度</option>'
            '<option value="cross">跨源</option>'
            '<option value="topic">主题</option>'
            "</select></label>\n"
            '<button id="reset" type="button">重置</button>\n'
            "</div>\n"
            '<p class="count" id="count" aria-live="polite">'
            f"显示 {len(data.papers)} / {len(data.papers)} 篇</p>\n"
            '<noscript><p class="meta">JavaScript 未启用：筛选不可用，'
            "全部论文仍可阅读。</p></noscript>\n"
        )
        cards = "\n".join(self._paper_card(item) for item in data.papers)
        script = (
            '<script type="application/json" id="site-index">' + payload + "</script>\n"
            "<script>\n" + _JS + "\n</script>\n"
        )
        return (
            head
            + controls
            + f'<div id="paper-list">\n{cards}\n</div>\n'
            + '<p class="empty" id="empty" hidden>没有符合条件的论文。</p>\n'
            + script
            + "</section>"
        )

    @staticmethod
    def _trend(data: SiteData) -> str:
        if not data.trend:
            return (
                '<section id="trend">\n<h2>近期概览</h2>\n'
                '<p class="empty">（暂无报告）</p>\n</section>'
            )
        rows = "\n".join(
            f"<tr><td>{_e(row.date)}</td><td>{row.new_today}</td>"
            f"<td>{row.hits}</td><td>{row.verified}</td></tr>"
            for row in data.trend
        )
        return (
            '<section id="trend">\n<h2>近期概览</h2>\n'
            "<table>\n<thead><tr><th>日期</th><th>新增</th><th>★ 命中</th>"
            f"<th>已验证推演</th></tr></thead>\n<tbody>\n{rows}\n</tbody>\n</table>\n"
            "</section>"
        )

    @staticmethod
    def _degraded(data: SiteData) -> str:
        if not data.degraded:
            return ""
        items = "\n".join(
            f"<li>{_e(key)}：{_e(reason)}</li>" for key, reason in sorted(data.degraded.items())
        )
        return (
            '<section id="degraded">\n<h2>降级状态</h2>\n'
            f'<ul class="insufficient">\n{items}\n</ul>\n'
            '<p class="meta">静默失败最危险：缺失的输入在此显式列出，不以空数据冒充正常。</p>\n'
            "</section>"
        )

    @staticmethod
    def _footer() -> str:
        return (
            "<footer>\n"
            "<p>数据来源：每日报告（data/reports）、记分板（data/predictions）、"
            "论文元数据（DB）。本页不读取、不包含 vault 个人笔记与论文正文。</p>\n"
            "<p>分项分各自独立展示，不合并为单一分数；样本不足时只报数字、不下结论。</p>\n"
            "<p>筛选 / 排序 / 搜索全部由本地原生 JS 完成，不发送任何网络请求。</p>\n"
            "</footer>"
        )

    # ── 落盘 ────────────────────────────────────────────────────────────
    def write_site(self, out_dir: Path, content: str) -> Path:
        """复用 `Exporter.write` 的原子写（临时文件 + rename）。"""
        return self.write(ExportDoc(path=self.site_path(out_dir), content=content))
