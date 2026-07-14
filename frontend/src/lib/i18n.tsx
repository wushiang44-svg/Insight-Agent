import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type Language = "en" | "zh";

const STORAGE_KEY = "insight-agent-language";

type Dict = Record<string, string>;

// Keys are semantic (what the string means), not the English text itself — the
// English text lives in `en` like any other language, so nothing is privileged
// as "the real string" that others are translated from.
const en: Dict = {
  "app.title": "VOC Insight Agent",
  "nav.newRun": "+ New Run",

  "runs.title": "Research Runs",
  "runs.loading": "Loading...",
  "runs.empty": "No research runs yet. Click the button above to start your first one.",
  "runs.col.productCategory": "Product Category",
  "runs.col.dataSource": "Data Source",
  "runs.col.status": "Status",
  "runs.col.iterations": "Iterations",
  "runs.col.evidence": "Evidence",
  "runs.col.createdAt": "Created At",

  "create.title": "New Research Run",
  "create.intro":
    "Enter the product category you want to research. The agent will keep searching, filtering, and analyzing relevant posts and comments, then generate a merchant-readable product improvement report once it has collected enough information.",
  "create.productCategory": "Product category *",
  "create.productCategory.placeholder": "e.g. wireless earbuds",
  "create.dataSource": "Data source",
  "create.source.reddit_api": "Reddit API (live search)",
  "create.source.reddit_scraper": "Reddit scraper (no API key, unofficial)",
  "create.source.amazon": "Amazon Reviews (browser automation, requires one-time login)",
  "create.source.youtube": "YouTube Comments (browser automation, no login needed)",
  "create.source.json_upload": "JSON upload (offline / demo data)",
  "create.redditWarning":
    'No Reddit API credentials detected — the Reddit Data API currently requires an approval process that may take a while. Consider "Reddit scraper" for live data without credentials, or "JSON upload" to try the full flow with prepared sample data.',
  "create.sourceWarning":
    "{source} isn't set up on this backend yet ({hint}). Pick another data source, or ask whoever runs this backend to finish setup.",
  "create.hint.amazon": "no agent-browser login found for Amazon — see the AmazonCollector docstring for the one-time login command",
  "create.hint.notInstalled": "agent-browser isn't installed",
  "create.note.reddit_scraper":
    'Pulls Reddit\'s public .json endpoints directly — no API key needed, but it\'s unofficial, slower (rate-limited client-side to be polite), and can be blocked or changed by Reddit at any time. Use "Reddit API" instead once your credentials are approved.',
  "create.note.amazon":
    "Drives a real, logged-in Chrome session per run — slower than an API (product search plus a 5-star-rating sweep per product), and needs a one-time manual login into a persistent agent-browser profile before it will return anything.",
  "create.note.youtube":
    "Drives a real Chrome session per run to search videos and scroll their comment sections — no login needed, but slower than an API and dependent on YouTube's comment lazy-loading behaving.",
  "create.upload.label": "Upload a JSON file (array of Reddit posts/comments)",
  "create.upload.selected": "Selected {name}, {count} item(s).",
  "create.upload.viewExample": "View JSON format example",
  "create.keywords": "Keywords (optional, comma-separated)",
  "create.keywords.placeholder": "battery, comfort",
  "create.subreddits": "Target subreddits (optional, comma-separated, without r/)",
  "create.subreddits.placeholder": "headphones, gadgets",
  "create.maxIterations": "Max iterations",
  "create.targetEvidence": "Target evidence count",
  "create.submitting": "Creating...",
  "create.submit": "Start Agent",
  "create.error.noCategory": "Please enter a product category",
  "create.error.noUpload": "JSON upload mode requires selecting a JSON file first",

  "detail.round": "Round",
  "detail.evidenceCollected": "Evidence collected",
  "detail.currentFocus": "Current research focus: ",
  "detail.notStarted": "Not started yet",
  "detail.dataSource": "Data source: ",
  "detail.error": "Error: ",
  "detail.stopReason": "Stop reason: ",
  "detail.stopAgent": "Stop Agent",
  "detail.viewReport": "View merchant report →",
  "detail.timeline": "Agent Research Timeline",
  "detail.loading": "Loading...",
  "detail.acrossAll": "across all of {source}",
  "detail.in": "in {group}",

  "report.title": "{category} — AI Product Insight Report",
  "report.titleFallback": "Merchant Optimization Report",
  "report.backToRun": "← Back to run",
  "report.health": "Overall Product Health",
  "report.sentiment": "Customer Sentiment",
  "report.evidenceCollected": "Evidence Collected",
  "report.itemsAnalyzed": "{noun} analyzed",
  "report.covered": "{group} Covered",
  "report.researchRounds": "Research Rounds",
  "report.iterations": "iterations",
  "report.topPriorities": "Top Priorities",
  "report.topPrioritiesHint": "If you fix one thing next quarter…",
  "report.noPriorities": "Not enough pain points or feature requests were collected to rank priorities yet.",
  "report.priority": "Priority {n}",
  "report.showMore": "Show {n} more",
  "report.showFewer": "Show fewer",
  "report.coverage": "{group} Coverage",
  "report.noGroupData": "No {group} data collected yet.",
  "report.aspect.one": "1 aspect",
  "report.aspect.many": "{n} aspects",
  "report.roadmap": "Recommended Product Roadmap",
  "report.noRoadmap": "Not enough evidence yet to propose a roadmap.",
  "report.fix": "Fix",
  "report.build": "Build",
  "report.expectedImpact": "Expected impact",
  "report.fullSummary": "Full analyst summary",
  "report.recommendedActions": "Recommended actions",
  "report.techAppendix": "Technical appendix",
  "report.quoteOriginal": "quotes are original review text and are not translated",
  "report.narrativeFallbackNote": "No Chinese version of this narrative was generated — showing the English original.",

  "why.percentOfComplaints": "{percent}% of complaints ({count} mention{s})",
  "why.percentOfFeatureRequests": "{percent}% of feature requests ({count} mention{s})",
  "why.mentionedAcross": "Mentioned across {count} {group}",
  "why.negativeSentiment": "{percent}% negative sentiment",
  "why.highConfidence": "High analyst confidence",

  "category.pain_point.label": "Pain point",
  "category.pain_point.section": "Top Pain Points",
  "category.pain_point.empty": "No significant pain points found.",
  "category.feature_request.label": "Feature request",
  "category.feature_request.section": "Feature Requests",
  "category.feature_request.empty": "No clear feature requests found.",
  "category.praise.label": "Praise",
  "category.praise.section": "Praised Aspects",
  "category.praise.empty": "No significant praise found.",
  "category.comparison.label": "Competitor mention",
  "category.comparison.section": "Competitor Mentions",
  "category.comparison.empty": "No competitor comparisons found.",

  "severity.critical": "Critical",
  "severity.high": "High",
  "severity.medium": "Medium",
  "severity.low": "Low",

  "health.good": "Good",
  "health.attention": "Needs attention",
  "health.atRisk": "At risk",

  "source.reddit_api.label": "Reddit API",
  "source.reddit_scraper.label": "Reddit Scraper (unofficial)",
  "source.json_upload.label": "JSON Upload",
  "source.amazon.label": "Amazon Reviews",
  "source.youtube.label": "YouTube Comments",
  "source.group.subreddit": "Subreddits",
  "source.group.subreddit.singular": "subreddit",
  "source.group.product": "Products",
  "source.group.product.singular": "product",
  "source.group.video": "Videos",
  "source.group.video.singular": "video",
  "source.group.source": "Sources",
  "source.group.source.singular": "source",
  "source.item.comments": "comments",
  "source.item.comment": "comment",
  "source.item.reviews": "reviews",
  "source.item.review": "review",
  "source.item.items": "items",
  "source.item.item": "item",

  "status.planning": "Planning",
  "status.searching": "Searching",
  "status.summarizing": "Summarizing",
  "status.completed": "Completed",
  "status.failed": "Failed",
  "status.stopped": "Stopped",

  "trace.step.thought": "Research Plan",
  "trace.step.action_search": "Data Collection",
  "trace.step.observation": "Key Findings",
  "trace.step.sufficiency_check": "Research Decision",
  "trace.step.summary": "Final Report",
  "trace.round": "Round {n}",
  "trace.complete": "Complete",
  "trace.inProgress": "In progress…",
  "trace.goal": "Goal",
  "trace.search": "Search",
  "trace.results": "Results",
  "trace.filtering": "Filtering",
  "trace.decision": "Decision",
  "trace.summary": "Summary",
  "trace.stop": "Stop",
  "trace.continue": "Continue",
  "trace.viewDetails": "View technical details",
  "trace.notPlanned": "Not planned yet",
  "trace.waitingForSearch": "Waiting for search…",
  "trace.filteringNotRun": "Filtering not run yet",
  "trace.empty": "The agent hasn't produced any steps yet.",
  "trace.resultsFound": "{n} result(s) found",
  "trace.searchFailed": "Search failed ({error})",
  "trace.itemsRetained": "{kept} useful item(s) retained (total {total})",
};

const zh: Dict = {
  "app.title": "VOC 客户之声洞察助手",
  "nav.newRun": "+ 新建任务",

  "runs.title": "研究任务",
  "runs.loading": "加载中...",
  "runs.empty": "还没有任何研究任务,点击上方按钮开始第一个。",
  "runs.col.productCategory": "产品类别",
  "runs.col.dataSource": "数据来源",
  "runs.col.status": "状态",
  "runs.col.iterations": "迭代轮次",
  "runs.col.evidence": "证据数",
  "runs.col.createdAt": "创建时间",

  "create.title": "新建研究任务",
  "create.intro":
    "输入你想研究的产品类别。智能体会持续搜索、筛选并分析相关的帖子和评论,收集到足够信息后自动生成一份商家可读的产品优化报告。",
  "create.productCategory": "产品类别 *",
  "create.productCategory.placeholder": "例如:无线耳机",
  "create.dataSource": "数据来源",
  "create.source.reddit_api": "Reddit API(实时搜索)",
  "create.source.reddit_scraper": "Reddit 爬虫(无需 API key,非官方)",
  "create.source.amazon": "Amazon 评论(浏览器自动化,需要一次性登录)",
  "create.source.youtube": "YouTube 评论(浏览器自动化,无需登录)",
  "create.source.json_upload": "JSON 上传(离线 / 演示数据)",
  "create.redditWarning":
    "未检测到 Reddit API 凭证——Reddit Data API 目前需要经过审批流程,可能要等一段时间。可以先选择「Reddit 爬虫」无需凭证也能拿到实时数据,或者选择「JSON 上传」用准备好的样本数据体验完整流程。",
  "create.sourceWarning": "{source} 在这个后端上还没配置好({hint})。请换一个数据来源,或者联系维护这个后端的人完成配置。",
  "create.hint.amazon": "没有找到 Amazon 的 agent-browser 登录状态——一次性登录命令请参考 AmazonCollector 的文档注释",
  "create.hint.notInstalled": "没有安装 agent-browser",
  "create.note.reddit_scraper":
    "直接抓取 Reddit 公开的 .json 接口——不需要 API key,但这是非官方方式,速度更慢(为了礼貌在客户端做了限流),而且随时可能被 Reddit 屏蔽或改变。凭证审批下来之后建议改用「Reddit API」。",
  "create.note.amazon":
    "每次任务都会启动一个真实的、已登录的 Chrome 会话——比 API 慢(每个商品要搜索加上 5 个星级筛选各抓一次),而且需要提前在一个持久化的 agent-browser profile 里手动登录一次,否则拿不到任何数据。",
  "create.note.youtube":
    "每次任务都会启动一个真实 Chrome 会话去搜索视频并滚动加载评论区——不需要登录,但比 API 慢,而且依赖 YouTube 评论懒加载机制正常工作。",
  "create.upload.label": "上传一个 JSON 文件(Reddit 帖子/评论数组)",
  "create.upload.selected": "已选择 {name},共 {count} 条。",
  "create.upload.viewExample": "查看 JSON 格式示例",
  "create.keywords": "关键词(可选,逗号分隔)",
  "create.keywords.placeholder": "电池,舒适度",
  "create.subreddits": "目标 subreddit(可选,逗号分隔,不带 r/)",
  "create.subreddits.placeholder": "headphones, gadgets",
  "create.maxIterations": "最大迭代轮次",
  "create.targetEvidence": "目标证据数量",
  "create.submitting": "创建中...",
  "create.submit": "启动智能体",
  "create.error.noCategory": "请输入产品类别",
  "create.error.noUpload": "JSON 上传模式需要先选择一个 JSON 文件",

  "detail.round": "轮次",
  "detail.evidenceCollected": "已收集证据",
  "detail.currentFocus": "当前研究焦点: ",
  "detail.notStarted": "尚未开始",
  "detail.dataSource": "数据来源: ",
  "detail.error": "错误: ",
  "detail.stopReason": "停止原因: ",
  "detail.stopAgent": "停止智能体",
  "detail.viewReport": "查看商家报告 →",
  "detail.timeline": "智能体研究时间线",
  "detail.loading": "加载中...",
  "detail.acrossAll": "覆盖全部{source}",
  "detail.in": "在 {group} 中",

  "report.title": "{category} — AI 产品洞察报告",
  "report.titleFallback": "商家优化报告",
  "report.backToRun": "← 返回任务",
  "report.health": "产品整体健康度",
  "report.sentiment": "客户情感倾向",
  "report.evidenceCollected": "已收集证据",
  "report.itemsAnalyzed": "已分析{noun}",
  "report.covered": "覆盖{group}",
  "report.researchRounds": "研究轮次",
  "report.iterations": "轮迭代",
  "report.topPriorities": "核心优先事项",
  "report.topPrioritiesHint": "如果下个季度只能修一件事……",
  "report.noPriorities": "目前收集到的痛点或功能请求还不够,无法排出优先级。",
  "report.priority": "优先级 {n}",
  "report.showMore": "展开剩余 {n} 项",
  "report.showFewer": "收起",
  "report.coverage": "{group}覆盖情况",
  "report.noGroupData": "还没有{group}相关数据。",
  "report.aspect.one": "1 个方面",
  "report.aspect.many": "{n} 个方面",
  "report.roadmap": "产品优化路线图建议",
  "report.noRoadmap": "目前证据还不够,无法提出路线图建议。",
  "report.fix": "修复",
  "report.build": "开发",
  "report.expectedImpact": "预期影响",
  "report.fullSummary": "完整分析总结",
  "report.recommendedActions": "推荐行动项",
  "report.techAppendix": "技术附录",
  "report.quoteOriginal": "引用为客户评论原文,不做翻译",
  "report.narrativeFallbackNote": "这部分内容还没有生成中文版,以下显示的是英文原文。",

  "why.percentOfComplaints": "占差评的 {percent}%({count} 次提及)",
  "why.percentOfFeatureRequests": "占功能请求的 {percent}%({count} 次提及)",
  "why.mentionedAcross": "覆盖 {count} 个{group}",
  "why.negativeSentiment": "{percent}% 为负面情感",
  "why.highConfidence": "分析置信度高",

  "category.pain_point.label": "痛点",
  "category.pain_point.section": "主要痛点",
  "category.pain_point.empty": "未发现明显的痛点。",
  "category.feature_request.label": "功能请求",
  "category.feature_request.section": "功能请求",
  "category.feature_request.empty": "未发现明确的功能请求。",
  "category.praise.label": "好评",
  "category.praise.section": "好评方面",
  "category.praise.empty": "未发现明显的好评。",
  "category.comparison.label": "竞品提及",
  "category.comparison.section": "竞品提及",
  "category.comparison.empty": "未发现竞品对比。",

  "severity.critical": "紧急",
  "severity.high": "高",
  "severity.medium": "中",
  "severity.low": "低",

  "health.good": "良好",
  "health.attention": "需要关注",
  "health.atRisk": "存在风险",

  "source.reddit_api.label": "Reddit API",
  "source.reddit_scraper.label": "Reddit 爬虫(非官方)",
  "source.json_upload.label": "JSON 上传",
  "source.amazon.label": "Amazon 评论",
  "source.youtube.label": "YouTube 评论",
  "source.group.subreddit": "Subreddit",
  "source.group.subreddit.singular": "subreddit",
  "source.group.product": "商品",
  "source.group.product.singular": "商品",
  "source.group.video": "视频",
  "source.group.video.singular": "视频",
  "source.group.source": "来源",
  "source.group.source.singular": "来源",
  "source.item.comments": "条评论",
  "source.item.comment": "条评论",
  "source.item.reviews": "条评论",
  "source.item.review": "条评论",
  "source.item.items": "条数据",
  "source.item.item": "条数据",

  "status.planning": "规划中",
  "status.searching": "搜索中",
  "status.summarizing": "生成报告中",
  "status.completed": "已完成",
  "status.failed": "失败",
  "status.stopped": "已停止",

  "trace.step.thought": "研究计划",
  "trace.step.action_search": "数据采集",
  "trace.step.observation": "关键发现",
  "trace.step.sufficiency_check": "研究决策",
  "trace.step.summary": "最终报告",
  "trace.round": "第 {n} 轮",
  "trace.complete": "已完成",
  "trace.inProgress": "进行中…",
  "trace.goal": "目标",
  "trace.search": "搜索",
  "trace.results": "结果",
  "trace.filtering": "筛选",
  "trace.decision": "决策",
  "trace.summary": "总结",
  "trace.stop": "停止",
  "trace.continue": "继续",
  "trace.viewDetails": "查看技术细节",
  "trace.notPlanned": "尚未规划",
  "trace.waitingForSearch": "等待搜索中…",
  "trace.filteringNotRun": "筛选尚未执行",
  "trace.empty": "智能体还没有产生任何步骤。",
  "trace.resultsFound": "找到 {n} 条结果",
  "trace.searchFailed": "搜索失败({error})",
  "trace.itemsRetained": "保留 {kept} 条有效数据(累计 {total} 条)",
};

const DICTS: Record<Language, Dict> = { en, zh };

export function translate(language: Language, key: string, vars?: Record<string, string | number>): string {
  const template = DICTS[language][key] ?? DICTS.en[key] ?? key;
  if (!vars) return template;
  return Object.entries(vars).reduce((text, [name, value]) => text.replaceAll(`{${name}}`, String(value)), template);
}

interface LanguageContextValue {
  language: Language;
  setLanguage: (language: Language) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

function loadInitialLanguage(): Language {
  if (typeof window === "undefined") return "en";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "zh" || stored === "en" ? stored : "en";
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(loadInitialLanguage);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, language);
  }, [language]);

  const value = useMemo<LanguageContextValue>(
    () => ({
      language,
      setLanguage: setLanguageState,
      t: (key, vars) => translate(language, key, vars),
    }),
    [language],
  );

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function useLanguage(): LanguageContextValue {
  const context = useContext(LanguageContext);
  if (!context) throw new Error("useLanguage must be used within a LanguageProvider");
  return context;
}
