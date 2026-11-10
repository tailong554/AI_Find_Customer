import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, NameIntentLead } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Search, ExternalLink, Copy, Loader2, UserRoundSearch } from "lucide-react";

const statusLabels: Record<string, string> = {
  new: "新线索", reviewed: "已审核", ready_to_contact: "待联系", contacted: "已联系", email_consented: "已授权邮箱", email_sent: "已发邮件", replied: "已回复", converted: "已成交", dismissed: "已忽略",
};
const statusOptions = ["new", "reviewed", "ready_to_contact", "contacted", "email_consented", "email_sent", "replied", "converted", "dismissed"];

function LeadCard({ lead, onStatus }: { lead: NameIntentLead; onStatus: (id: string, status: string) => void }) {
  const [email, setEmail] = useState("");
  const [consent, setConsent] = useState(false);
  const [consentSaved, setConsentSaved] = useState(false);
  const [followupStatus, setFollowupStatus] = useState("");
  const [emailError, setEmailError] = useState("");
  const saveConsent = async () => {
    try {
      setEmailError("");
      await api.recordNameLeadConsent(lead.id, { email, consent_confirmed: consent, consent_source: lead.url, consent_evidence: "User voluntarily provided this email and explicitly checked the consent box in the lead management workflow." });
      setConsentSaved(true);
      setFollowupStatus("consented");
    } catch (error) { setEmailError((error as Error).message); }
  };
  const startFollowup = async () => {
    try { const result = await api.startNameLeadFollowup(lead.id); setFollowupStatus(String(result.followup.status || "active")); setEmailError(""); }
    catch (error) { setEmailError((error as Error).message); }
  };
  return (
    <Card>
      <CardContent className="p-5 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={lead.intent_score >= 70 ? "default" : "secondary"}>{lead.intent_score} 分</Badge>
          <Badge variant="outline">{lead.source_platform}</Badge>
          <Badge variant="outline">{lead.purchase_signal === "explicit" ? "明确付费信号" : lead.purchase_signal === "possible" ? "可能有服务需求" : "未表达付费意愿"}</Badge>
          <span className="text-xs text-muted-foreground ml-auto">{statusLabels[lead.status] || lead.status}</span>
        </div>
        <a href={lead.url} target="_blank" rel="noreferrer" className="font-semibold hover:underline flex items-center gap-2">
          {lead.title || lead.url}<ExternalLink className="h-4 w-4 shrink-0" />
        </a>
        <p className="text-sm text-muted-foreground">{lead.snippet || "搜索结果没有提供摘要。"}</p>
        {lead.evidence_quote && <p className="text-sm border-l-2 pl-3">意向证据：{lead.evidence_quote}</p>}
        {lead.need_summary && <p className="text-sm"><strong>需求摘要：</strong>{lead.need_summary}</p>}
        {lead.buying_signal_summary && <p className="text-sm"><strong>购买信号：</strong>{lead.buying_signal_summary}</p>}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs">
          {[
            ["需求明确", lead.need_clarity_score, 3],
            ["购买阶段", lead.purchase_stage_score, 3],
            ["紧迫程度", lead.urgency_score, 3],
            ["服务匹配", lead.service_fit_score, 3],
            ["公开可互动", lead.contactability_score, 2],
          ].map(([label, score, max]) => <div key={label} className="rounded-md bg-muted p-2"><div className="text-muted-foreground">{label}</div><strong>{score ?? 0}/{max}</strong></div>)}
        </div>
        <p className="text-xs text-muted-foreground">{lead.score_explanation || lead.reason}</p>
        <Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(`${window.location.origin}/name-request?source=${encodeURIComponent(lead.source_platform.toLowerCase())}&ref=${encodeURIComponent(lead.id)}`)}><Copy className="h-3 w-3 mr-2" />复制该线索专属起名表单链接</Button>
        {lead.reply_draft && (
          <div className="rounded-lg bg-muted p-3 space-y-2">
            <div className="text-xs font-semibold">英文回复草稿（人工审核后发布）</div>
            <p className="text-sm">{lead.reply_draft}</p>
            <Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(lead.reply_draft)}>
              <Copy className="h-3 w-3 mr-2" />复制回复
            </Button>
          </div>
        )}
        <div className="rounded-lg border p-3 space-y-2">
          <div className="font-semibold text-sm">邮件联系（仅限用户主动同意）</div>
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="用户主动提供的邮箱地址" />
          <label className="flex items-start gap-2 text-xs text-muted-foreground">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
            已核实用户明确同意接收中文起名服务邮件，并已记录同意证据。
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" disabled={!email || !consent} onClick={saveConsent}>保存邮箱与同意记录</Button>
            <Button size="sm" disabled={!consentSaved} onClick={startFollowup}>发送首封邮件</Button>
            {followupStatus && <Badge variant="outline">{followupStatus}</Badge>}
          </div>
          {emailError && <p className="text-xs text-destructive">{emailError}</p>}
          <p className="text-xs text-muted-foreground">保存同意不会自动发送。发送首封邮件前会检查 SMTP 测试状态；后续按设置执行，收到回复或退订后停止。</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {statusOptions.map((status) => (
            <Button key={status} size="sm" variant={lead.status === status ? "default" : "outline"} onClick={() => onStatus(lead.id, status)}>
              {statusLabels[status]}
            </Button>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

export function NameLeadsPage() {
  const [country, setCountry] = useState("us");
  const [minimumScore, setMinimumScore] = useState(0);
  const [filter, setFilter] = useState("");
  const [platformFilter, setPlatformFilter] = useState("");
  const dashboard = useQuery({ queryKey: ["name-lead-dashboard"], queryFn: api.getNameLeadDashboard, refetchInterval: 15000 });
  const revenue = useQuery({ queryKey: ["name-lead-revenue"], queryFn: api.getNameLeadRevenue, refetchInterval: 30000 });
  const [platforms, setPlatforms] = useState({ reddit: true, forums: true, tiktok: true, instagram: true, facebook: true });
  const queryClient = useQueryClient();
  const leadsQuery = useQuery({ queryKey: ["name-leads", filter, minimumScore, platformFilter], queryFn: () => api.listNameLeads(filter, minimumScore, platformFilter) });
  const search = useMutation({
    mutationFn: () => api.searchNameLeads({
      country_code: country.trim().toLowerCase(),
      max_results_per_query: 6,
      max_candidates: 40,
      include_reddit: platforms.reddit,
      include_other_forums: platforms.forums,
      include_tiktok: platforms.tiktok,
      include_instagram: platforms.instagram,
      include_facebook: platforms.facebook,
    }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["name-leads"] }); queryClient.invalidateQueries({ queryKey: ["name-lead-dashboard"] }); },
  });
  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => api.updateNameLeadStatus(id, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["name-leads"] }),
  });
  const leads = leadsQuery.data?.leads ?? [];
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight flex items-center gap-3"><UserRoundSearch className="h-8 w-8" />中文起名潜客</h1>
        <p className="text-muted-foreground mt-2">发现公开表达过“想给自己取中文名字”的帖子，按文本意向排序。</p>
      </div>
      <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
        {[
          ["今日新增", dashboard.data?.today_new_leads ?? 0],
          ["AI分析内容", dashboard.data?.today_analyzed ?? 0],
          ["明确需求", dashboard.data?.today_direct_requests ?? 0],
          ["值得联系", dashboard.data?.today_worth_contacting ?? 0],
        ].map(([label, value]) => <Card key={label}><CardContent className="p-4"><p className="text-xs text-muted-foreground">{label}</p><p className="text-2xl font-bold">{value}</p></CardContent></Card>)}
      </div>
      <Card>
        <CardHeader><CardTitle>平台订单与收入</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div><p className="text-xs text-muted-foreground">已归因订单</p><p className="text-2xl font-bold">{revenue.data?.total_orders ?? 0}</p></div>
            <div><p className="text-xs text-muted-foreground">已验证邮箱授权</p><p className="text-2xl font-bold">{revenue.data?.total_verified_consents ?? 0}</p></div>
          </div>
          <div className="space-y-2 text-sm">
            {(revenue.data?.platforms ?? []).map((item) => <div key={item.source} className="flex flex-col gap-1 border-b pb-2"><div className="flex justify-between"><span>{item.source}</span><strong>{item.orders} 单 · 转化 {item.conversion_rate}%</strong></div><div className="text-xs text-muted-foreground">提交 {item.submitted} · 已验证 {item.verified} · 收入 {Object.entries(item.revenue_by_currency).map(([currency, amount]) => `${currency} ${Number(amount).toFixed(2)}`).join(" / ") || "暂无"}</div></div>)}
          </div>
          <p className="text-xs text-muted-foreground">订单数据需要支付平台向 /api/v1/public/orders 回传成功付款事件。未配置 webhook 时不会自动出现订单。</p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>获客漏斗与平台分布</CardTitle></CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2 text-sm">
            {Object.entries(dashboard.data?.funnel ?? {}).map(([key, value]) => <div key={key} className="flex justify-between border-b pb-1"><span>{({analyzed:"分析内容",related:"相关需求",purchase_intent:"有购买信号",worth_contacting:"值得联系",contacted:"已联系"} as Record<string,string>)[key] || key}</span><strong>{String(value)}</strong></div>)}
          </div>
          <div className="space-y-2 text-sm">
            {Object.entries(dashboard.data?.platform_counts ?? {}).map(([key, value]) => <div key={key} className="flex justify-between border-b pb-1"><span>{key}</span><strong>{String(value)}</strong></div>)}
          </div>
        </CardContent>
      </Card>
      {dashboard.data?.social_capabilities && <Card><CardHeader><CardTitle>社交平台数据接入状态</CardTitle></CardHeader><CardContent className="grid gap-3 md:grid-cols-3">{Object.entries(dashboard.data.social_capabilities).map(([platform, value]: [string, any]) => <div key={platform} className="rounded-lg border p-3"><strong>{platform}</strong><p className="text-xs text-muted-foreground mt-1">{value.mode}</p><p className="text-xs mt-2">{value.message || value.capability}</p><p className="text-xs mt-1">{value.configured ? "已配置" : "待授权/配置"}</p></div>)}</CardContent></Card>}
      {dashboard.data?.active_run && <Card><CardContent className="p-4 text-sm"><strong>搜索任务运行中</strong><p className="text-muted-foreground mt-1">已完成 {dashboard.data.active_run.queries_completed}/{dashboard.data.active_run.queries_total} 个搜索词，已分析 {dashboard.data.active_run.raw_result_count} 条结果。</p></CardContent></Card>}
      <Card>
        <CardHeader><CardTitle>搜索公开需求</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-4 text-sm">
            {([
              ["reddit", "Reddit"],
              ["forums", "Quora / 其他论坛"],
              ["tiktok", "TikTok"],
              ["instagram", "Instagram"],
              ["facebook", "Facebook"],
            ] as const).map(([key, label]) => (
              <label key={key} className="flex items-center gap-2">
                <input type="checkbox" checked={platforms[key]} onChange={(e) => setPlatforms((old) => ({ ...old, [key]: e.target.checked }))} />
                {label}
              </label>
            ))}
          </div>
          <div className="flex flex-col sm:flex-row gap-3">
          <div className="flex-1">
            <label className="text-sm text-muted-foreground">目标国家代码（可留空）</label>
            <Input value={country} onChange={(e) => setCountry(e.target.value)} placeholder="us / gb / ca / au" />
          </div>
          <div className="flex items-end">
            <Button onClick={() => search.mutate()} disabled={search.isPending}>
              {search.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Search className="h-4 w-4 mr-2" />}
              {search.isPending ? "正在搜索..." : "开始搜索潜客"}
            </Button>
          </div>
          </div>
        </CardContent>
      </Card>
      {search.isError && <p className="text-sm text-destructive">{(search.error as Error).message}</p>}
      {search.data && (
        <Card><CardContent className="p-4 grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div><div className="text-muted-foreground">搜索来源</div><strong>{search.data.provider}</strong></div>
          <div><div className="text-muted-foreground">候选帖子</div><strong>{search.data.candidate_count}</strong></div>
          <div><div className="text-muted-foreground">明确提出需求</div><strong>{search.data.direct_requests}</strong></div>
          <div><div className="text-muted-foreground">高意向线索</div><strong>{search.data.high_intent_count}</strong></div>
        </CardContent></Card>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xl font-semibold mr-auto">已发现的帖子 ({leadsQuery.data?.count ?? 0})</h2>
        <select className="h-9 rounded-md border bg-background px-3 text-sm" value={platformFilter} onChange={(e) => setPlatformFilter(e.target.value)}>
          <option value="">全部平台</option>{["Reddit","TikTok","Instagram","Facebook","Quora","Forum","Web"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select className="h-9 rounded-md border bg-background px-3 text-sm" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">全部状态</option>{statusOptions.map((s) => <option key={s} value={s}>{statusLabels[s]}</option>)}
        </select>
        <select className="h-9 rounded-md border bg-background px-3 text-sm" value={minimumScore} onChange={(e) => setMinimumScore(Number(e.target.value))}>
          <option value={0}>全部分数</option><option value={50}>50分以上</option><option value={70}>70分以上</option><option value={85}>85分以上</option>
        </select>
      </div>
      {leadsQuery.isLoading ? <p className="text-muted-foreground">正在读取线索...</p> :
        leads.length ? <div className="grid gap-4">{leads.map((lead) => <LeadCard key={lead.id} lead={lead} onStatus={(id, status) => update.mutate({ id, status })} />)}</div> :
        <Card><CardContent className="p-8 text-center text-muted-foreground">还没有线索。先点击“开始搜索潜客”。</CardContent></Card>}
      <p className="text-xs text-muted-foreground">只保存公开帖子链接和公开摘要。分数表示文本与中文起名需求的匹配程度，不代表用户愿意付费。请先阅读原帖，遵守平台规则，再人工决定是否回复；不要群发或骚扰用户。</p>
    </div>
  );
}
