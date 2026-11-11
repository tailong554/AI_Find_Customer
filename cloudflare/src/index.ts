export interface Env {
  DB: D1Database; SEARCH_QUEUE: Queue; ASSETS: Fetcher;
  SERPER_API_KEY: string; AGNES_API_KEY: string; AGNES_API_BASE?: string; AGNES_MODEL?: string;
  RESEND_API_KEY: string; FROM_EMAIL: string; REPLY_TO_EMAIL: string; PUBLIC_APP_BASE_URL: string;
  ACCESS_TEAM_DOMAIN: string; ACCESS_AUD: string; ADMIN_EMAIL: string; ORDER_WEBHOOK_SECRET: string;
  AUTO_FOLLOWUP_ENABLED?: string; SEARCH_COUNTRY_CODE?: string;
}
type SearchMessage = { runId: string; countryCode: string; limit: number };
const QUERIES = [
  'site:reddit.com "I want a Chinese name"',
  'site:reddit.com "help me choose" "Chinese name"',
  'site:reddit.com "looking for a Chinese name"',
  'site:reddit.com "what Chinese name should I choose"',
  'site:reddit.com "Chinese name suggestions" "for me"',
  'site:quora.com "Chinese name" "for me" suggestions',
  'site:tiktok.com "I want a Chinese name"',
  'site:tiktok.com "need a Chinese name"',
  'site:instagram.com "I want a Chinese name"',
  'site:facebook.com "looking for a Chinese name"',
  '"need a Chinese name" forum',
  '"help me pick a Chinese name" -dictionary',
];
const DIRECT = ["i want a chinese name","i need a chinese name","need a chinese name","looking for a chinese name","help me choose a chinese name","help me pick a chinese name","suggest a chinese name for me","recommend a chinese name for me","what chinese name should i","what would be a good chinese name for me","can someone give me a chinese name","choose a chinese name for me","pick a chinese name for me","chinese name for myself","chinese name suggestions for me"];
let jwksCache: { until: number; keys: JsonWebKey[] } | null = null;

const now = () => new Date().toISOString();
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" } });
const cleanSource = (s: string) => (s || "website").toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 40) || "website";
async function hash(s: string) { const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)); return Array.from(new Uint8Array(b)).map(x => x.toString(16).padStart(2,"0")).join(""); }
function b64(s: string) { const x=s.replace(/-/g,"+").replace(/_/g,"/"); const v=atob(x+"=".repeat((4-x.length%4)%4)); return Uint8Array.from(v,c=>c.charCodeAt(0)); }
function jwtPart(s: string) { return JSON.parse(new TextDecoder().decode(b64(s))); }
function platform(url: string) { const h=new URL(url).hostname.replace(/^www\./,""); if(h.endsWith("reddit.com"))return "Reddit"; if(h.endsWith("tiktok.com"))return "TikTok"; if(h.endsWith("instagram.com"))return "Instagram"; if(h.endsWith("facebook.com"))return "Facebook"; if(h.endsWith("quora.com"))return "Quora"; return "Web"; }

async function adminAllowed(req: Request, env: Env) {
  if (!env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD || !env.ADMIN_EMAIL) return false;
  const parts=(req.headers.get("Cf-Access-Jwt-Assertion")||"").split(".");
  if(parts.length!==3)return false;
  try {
    const h=jwtPart(parts[0]), p=jwtPart(parts[1]);
    const team=env.ACCESS_TEAM_DOMAIN.replace(/^https?:\/\//,"").replace(/\/$/,"");
    const iss="https://"+team;
    if(h.alg!=="RS256" || !h.kid || p.iss!==iss || !Array.isArray(p.aud) || !p.aud.includes(env.ACCESS_AUD))return false;
    if(Number(p.exp)<=Date.now()/1000 || (p.nbf && Number(p.nbf)>Date.now()/1000))return false;
    if(String(p.email||"").toLowerCase()!==env.ADMIN_EMAIL.toLowerCase())return false;
    if(!jwksCache || jwksCache.until<Date.now()) {
      const r=await fetch(iss+"/cdn-cgi/access/certs"); if(!r.ok)return false;
      const d:any=await r.json(); jwksCache={keys:d.keys||[],until:Date.now()+600000};
    }
    const key=jwksCache.keys.find((k:any)=>k.kid===h.kid); if(!key)return false;
    const cryptoKey=await crypto.subtle.importKey("jwk",key,{name:"RSASSA-PKCS1-v1_5",hash:"SHA-256"},false,["verify"]);
    return crypto.subtle.verify("RSASSA-PKCS1-v1_5",cryptoKey,b64(parts[2]),new TextEncoder().encode(parts[0]+"."+parts[1]));
  } catch { return false; }
}

function heuristic(row:any) {
  const t=(row.title+" "+row.snippet).toLowerCase(), hit=DIRECT.find(x=>t.includes(x));
  if(hit)return {intent_type:"direct_request",intent_score:78,purchase_signal:"possible",need_summary:"The author wants a personal Chinese name.",buying_signal_summary:"They are asking for personalized suggestions; willingness to pay is not confirmed.",evidence_quote:hit,reason:"First-person request for a personal Chinese name.",reply_draft:"A good Chinese name should sound natural and carry a meaning that fits you. If you share the meaning or style you prefer, I can suggest a few options and explain each character.",need_clarity_score:3,purchase_stage_score:2,urgency_score:0,service_fit_score:3,contactability_score:2,score_explanation:"Clear personal request; urgency and payment intent are not stated."};
  return {intent_type:"irrelevant",intent_score:5,purchase_signal:"unknown",need_summary:"",buying_signal_summary:"No clear buying signal.",evidence_quote:"",reason:"No explicit first-person request was found.",reply_draft:"",need_clarity_score:0,purchase_stage_score:0,urgency_score:0,service_fit_score:0,contactability_score:0,score_explanation:"No explicit personal request identified."};
}
function weighted(x:any) {
  const n=(v:any,m:number)=>Math.max(0,Math.min(m,Number.isFinite(Number(v))?Math.round(Number(v)):0));
  const a=n(x.need_clarity_score,3), b=n(x.purchase_stage_score,3), c=n(x.urgency_score,3), d=n(x.service_fit_score,3), e=n(x.contactability_score,2);
  return {need_clarity_score:a,purchase_stage_score:b,urgency_score:c,service_fit_score:d,contactability_score:e,intent_score:Math.round(a/3*25+b/3*25+c/3*15+d/3*25+e/2*10)};
}
async function findCandidates(env:Env, country:string, limit:number) {
  if(!env.SERPER_API_KEY)throw new Error("SERPER_API_KEY is not configured.");
  const map=new Map<string,any>(); let raw=0;
  for(const q of QUERIES) {
    const r=await fetch("https://google.serper.dev/search",{method:"POST",headers:{"X-API-KEY":env.SERPER_API_KEY,"content-type":"application/json"},body:JSON.stringify({q,gl:country||"us",hl:"en",num:10})});
    if(!r.ok)throw new Error("Serper HTTP "+r.status);
    const d:any=await r.json(); raw+=(d.organic||[]).length;
    for(const x of d.organic||[]) {
      if(!x.link)continue;
      let u:URL; try{u=new URL(x.link)}catch{continue}
      if(u.protocol!=="https:" && u.protocol!=="http:")continue;
      ["utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid"].forEach(k=>u.searchParams.delete(k));u.hash="";
      const link=u.toString().replace(/\/$/,""); if(map.has(link))continue;
      map.set(link,{id:await hash(link),title:String(x.title||"").slice(0,500),url:link,snippet:String(x.snippet||"").slice(0,1800),source_platform:platform(link),source_domain:u.hostname,search_query:q});
      if(map.size>=limit)break;
    }
    if(map.size>=limit)break;
  }
  return {rows:Array.from(map.values()),raw};
}
async function aiClassify(env:Env, rows:any[]) {
  const fallback=rows.map(r=>({...r,...heuristic(r)})); if(!env.AGNES_API_KEY)return fallback;
  try {
    const prompt=rows.map(r=>({id:r.id,title:r.title,snippet:r.snippet.slice(0,900),platform:r.source_platform}));
    const r=await fetch((env.AGNES_API_BASE||"https://apihub.agnes-ai.com/v1").replace(/\/$/,"")+"/chat/completions",{method:"POST",headers:{authorization:"Bearer "+env.AGNES_API_KEY,"content-type":"application/json"},body:JSON.stringify({model:env.AGNES_MODEL||"agnes-2.5-flash",temperature:0.1,response_format:{type:"json_object"},messages:[
      {role:"system",content:"Classify public posts for a personal Chinese-name service. Use only explicit text. Do not infer ethnicity, age, gender, location, income, identity or willingness to pay. A direct_request must ask for a personal Chinese name. Return JSON {\"leads\":[{\"id\":\"...\",\"intent_type\":\"direct_request|research|irrelevant\",\"purchase_signal\":\"explicit|possible|unknown\",\"need_clarity_score\":0,\"purchase_stage_score\":0,\"urgency_score\":0,\"service_fit_score\":0,\"contactability_score\":0,\"need_summary\":\"...\",\"buying_signal_summary\":\"...\",\"evidence_quote\":\"exact substring or empty\",\"reason\":\"...\",\"score_explanation\":\"...\",\"reply_draft\":\"short helpful public reply\"}]}. Score 0-3 except contactability 0-2. Never extract private contact details or include links."},
      {role:"user",content:JSON.stringify(prompt)}
    ]})});
    if(!r.ok)throw new Error("Agnes HTTP "+r.status);
    const d:any=await r.json();let raw=String(d.choices?.[0]?.message?.content||"{}").trim();
    raw=raw.replace(/^\x60{3}(?:json)?\s*/i,"").replace(/\s*\x60{3}$/,"");
    const parsed=JSON.parse(raw), byId=new Map((parsed.leads||[]).map((x:any)=>[String(x.id),x]));
    return rows.map((row,i)=>{
      const base=fallback[i], x:any=byId.get(row.id)||{}, type=["direct_request","research","irrelevant"].includes(x.intent_type)?x.intent_type:base.intent_type;
      const dims=weighted(x);if(type==="irrelevant")Object.assign(dims,weighted({}));
      if(type==="research")dims.intent_score=Math.min(55,dims.intent_score);
      const quote=String(x.evidence_quote||"");
      return {...row,...base,...x,...dims,intent_type:type,purchase_signal:["explicit","possible","unknown"].includes(x.purchase_signal)?x.purchase_signal:base.purchase_signal,
        evidence_quote:quote && (row.title+" "+row.snippet).toLowerCase().includes(quote.toLowerCase())?quote:base.evidence_quote,
        reply_draft:type==="direct_request"?String(x.reply_draft||base.reply_draft).slice(0,1200):""};
    });
  } catch { return fallback; }
}
async function runSearch(env:Env, runId:string, country:string, limit:number) {
  await env.DB.prepare("UPDATE search_runs SET status='running',started_at=? WHERE id=?").bind(now(),runId).run();
  try {
    const found=await findCandidates(env,country,Math.max(1,Math.min(50,limit))), rows:any[]=[];
    for(let i=0;i<found.rows.length;i+=10)rows.push(...await aiClassify(env,found.rows.slice(i,i+10)));
    const stamp=now();
    const stmts=rows.map(x=>env.DB.prepare("INSERT INTO leads (id,title,url,snippet,source_platform,source_domain,search_query,intent_type,intent_score,purchase_signal,reason,evidence_quote,reply_draft,need_summary,buying_signal_summary,need_clarity_score,purchase_stage_score,urgency_score,service_fit_score,contactability_score,score_explanation,first_seen_at,last_seen_at,search_count,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'new',?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,snippet=excluded.snippet,source_platform=excluded.source_platform,source_domain=excluded.source_domain,search_query=excluded.search_query,intent_type=excluded.intent_type,intent_score=excluded.intent_score,purchase_signal=excluded.purchase_signal,reason=excluded.reason,evidence_quote=excluded.evidence_quote,reply_draft=excluded.reply_draft,need_summary=excluded.need_summary,buying_signal_summary=excluded.buying_signal_summary,need_clarity_score=excluded.need_clarity_score,purchase_stage_score=excluded.purchase_stage_score,urgency_score=excluded.urgency_score,service_fit_score=excluded.service_fit_score,contactability_score=excluded.contactability_score,score_explanation=excluded.score_explanation,last_seen_at=excluded.last_seen_at,search_count=leads.search_count+1,updated_at=excluded.updated_at")
      .bind(x.id,x.title,x.url,x.snippet,x.source_platform,x.source_domain,x.search_query,x.intent_type,x.intent_score,x.purchase_signal,x.reason,x.evidence_quote,x.reply_draft,x.need_summary||"",x.buying_signal_summary||"",x.need_clarity_score||0,x.purchase_stage_score||0,x.urgency_score||0,x.service_fit_score||0,x.contactability_score||0,x.score_explanation||"",stamp,stamp,stamp,stamp));
    if(stmts.length)await env.DB.batch(stmts);
    const counts:Record<string,number>={};rows.forEach(x=>counts[x.source_platform]=(counts[x.source_platform]||0)+1);
    await env.DB.prepare("UPDATE search_runs SET status='completed',provider='Serper + Agnes AI',queries_total=?,queries_completed=?,raw_result_count=?,candidate_count=?,direct_requests=?,high_intent_count=?,platform_counts_json=?,finished_at=?,error_summary='' WHERE id=?")
      .bind(QUERIES.length,QUERIES.length,found.raw,rows.length,rows.filter(x=>x.intent_type==="direct_request").length,rows.filter(x=>x.intent_score>=70).length,JSON.stringify(counts),now(),runId).run();
  } catch(e:any) {
    await env.DB.prepare("UPDATE search_runs SET status='failed',error_summary=?,finished_at=? WHERE id=?").bind(String(e?.message||e).slice(0,1000),now(),runId).run();
    throw e;
  }
}
async function sendMail(env:Env,to:string,subject:string,text:string) {
  if(!env.RESEND_API_KEY||!env.FROM_EMAIL)throw new Error("Resend not configured.");
  const r=await fetch("https://api.resend.com/emails",{method:"POST",headers:{authorization:"Bearer "+env.RESEND_API_KEY,"content-type":"application/json"},body:JSON.stringify({from:env.FROM_EMAIL,to:[to],subject,text,reply_to:env.REPLY_TO_EMAIL||undefined})});
  const d:any=await r.json().catch(()=>({}));if(!r.ok)throw new Error("Resend HTTP "+r.status);return String(d.id||"");
}
async function emailEvent(env:Env,leadId:string,email:string,step:number,subject:string,status:string,providerId="") {
  await env.DB.prepare("INSERT INTO email_events (id,lead_id,email,step,subject,status,provider_message_id,error,created_at) VALUES (?,?,?,?,?,?,?,'',?)")
    .bind(crypto.randomUUID(),leadId,email,step,subject,status,providerId,now()).run();
}
async function publicSubmit(req:Request,env:Env) {
  const b:any=await req.json().catch(()=>({})), email=String(b.email||"").trim().toLowerCase();
  if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))return json({error:"Invalid email."},422);
  if(b.consent!==true)return json({error:"Explicit consent required."},403);
  if(!env.PUBLIC_APP_BASE_URL?.startsWith("https://"))return json({error:"Configure PUBLIC_APP_BASE_URL with HTTPS."},503);
  const eh=await hash(email), day=new Date(Date.now()-86400000).toISOString();
  const count=await env.DB.prepare("SELECT COUNT(*) n FROM consents WHERE email_hash=? AND created_at>=?").bind(eh,day).first<any>();
  if(Number(count?.n||0)>=5)return json({error:"Too many submissions for this email today."},429);
  const id=crypto.randomUUID(), token=crypto.randomUUID()+crypto.randomUUID(), stamp=now(), source=cleanSource(String(b.source||"website")), ref=String(b.referral_code||"").slice(0,80);
  await env.DB.prepare("INSERT INTO consents (id,lead_id,email,email_hash,source,referral_code,preferred_style,consent_text,verify_token_hash,expires_at,verified_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'',?)")
    .bind(id,ref||id,email,eh,source,ref,String(b.preferred_style||"").slice(0,300),"I agree to receive emails about my Chinese-name request and related service follow-up. I can unsubscribe at any time.",await hash(token),new Date(Date.now()+86400000).toISOString(),stamp).run();
  const link=env.PUBLIC_APP_BASE_URL.replace(/\/$/,"")+"/name-request?token="+encodeURIComponent(token);
  try {
    const mid=await sendMail(env,email,"Confirm your Chinese name request","Please confirm your email and consent within 24 hours:\n\n"+link+"\n\nNo follow-up emails will be sent until you confirm. Ignore this email if you did not request it.");
    await emailEvent(env,id,email,0,"Confirm your Chinese name request","sent",mid);
  } catch {
    await env.DB.prepare("DELETE FROM consents WHERE id=?").bind(id).run();
    return json({error:"Could not send confirmation email. Check Resend configuration."},502);
  }
  return json({status:"confirmation_sent"});
}
async function verifySubmit(req:Request,env:Env) {
  const token=new URL(req.url).searchParams.get("token")||"", stamp=now();
  if(!token||token.length>200)return json({error:"Invalid token."},400);
  const c=await env.DB.prepare("SELECT * FROM consents WHERE verify_token_hash=? AND verified_at='' AND expires_at>?").bind(await hash(token),stamp).first<any>();
  if(!c)return json({error:"Confirmation link invalid or expired."},404);
  await env.DB.prepare("UPDATE consents SET verified_at=? WHERE id=?").bind(stamp,c.id).run();
  let leadId=String(c.lead_id||""), exists=leadId?await env.DB.prepare("SELECT id FROM leads WHERE id=?").bind(leadId).first<any>():null;
  if(!exists) {
    leadId=String(c.id);
    const sourcePlatform=String(c.source||"website").replace(/(^|[-_])([a-z])/g,(_:string,a:string,b:string)=>a+b.toUpperCase());
    await env.DB.prepare("INSERT OR IGNORE INTO leads (id,title,url,snippet,source_platform,source_domain,search_query,intent_type,intent_score,purchase_signal,reason,evidence_quote,reply_draft,need_summary,buying_signal_summary,need_clarity_score,purchase_stage_score,urgency_score,service_fit_score,contactability_score,score_explanation,first_seen_at,last_seen_at,search_count,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'direct_request',70,'possible',?,?,?,?,?,?,?,?,?,?,?,?,?,1,'email_consented',?,?)")
      .bind(leadId,"Verified Chinese name request","https://lead.local/"+leadId,String(c.preferred_style||"Customer submitted a Chinese-name request."),sourcePlatform,"website","Public double opt-in","Email ownership and consent verified.","","", "The customer submitted a Chinese-name request and verified their email.","Email consent explicitly confirmed.",3,2,0,3,2,"Verified request and explicit email consent",stamp,stamp,stamp,stamp).run();
  } else await env.DB.prepare("UPDATE leads SET status='email_consented',updated_at=? WHERE id=?").bind(stamp,leadId).run();
  await env.DB.prepare("INSERT INTO email_followups (lead_id,email,consent_confirmed,status,step,next_send_at,last_sent_at,last_reply_at,last_reply_subject,last_error,created_at,updated_at) VALUES (?,?,1,'consented',0,'','','','','',?,?) ON CONFLICT(lead_id) DO UPDATE SET email=excluded.email,consent_confirmed=1,status=CASE WHEN email_followups.step=0 THEN 'consented' ELSE email_followups.status END,updated_at=excluded.updated_at")
    .bind(leadId,c.email,stamp,stamp).run();
  return json({status:"verified",message:"Email verified and added to the follow-up queue."});
}
async function sendLead(env:Env,leadId:string,automatic:boolean):Promise<Response|null> {
  const f=await env.DB.prepare("SELECT * FROM email_followups WHERE lead_id=?").bind(leadId).first<any>();
  if(!f||Number(f.consent_confirmed)!==1)return json({error:"Verified consent required."},403);
  if(["replied","unsubscribed","completed","sending"].includes(String(f.status)))return json({error:"Follow-up stopped."},409);
  if(automatic&&f.status!=="active")return json({error:"No active follow-up."},409);
  if(!automatic&&(Number(f.step)>0||f.status!=="consented"))return json({error:"First email already sent or consent not ready."},409);
  const day=new Date().toISOString().slice(0,10), sent=await env.DB.prepare("SELECT COUNT(*) n FROM email_events WHERE status='sent' AND created_at LIKE ?").bind(day+"%").first<any>();
  if(Number(sent?.n||0)>=80)return json({error:"Daily email safety cap reached."},429);
  const step=Number(f.step)+1;
  const messages:any={
    1:["Your Chinese name request","Thanks for sharing your interest in choosing a Chinese name. If you reply with your preferred meaning, style, or pronunciation, I can prepare a few thoughtful options and explain each character.\n\nIf you no longer want these emails, reply STOP.\n\nChinese Name Studio"],
    2:["A quick follow-up on your Chinese name","I’m following up on your Chinese-name request. If you’re still interested, reply with a meaning or style you like and I can help you explore suitable options.\n\nIf you no longer want these emails, reply STOP.\n\nChinese Name Studio"],
    3:["Closing the loop on your Chinese name","This is my last follow-up about your Chinese-name request. Reply whenever convenient if you still need help. Otherwise, no action is needed.\n\nIf you no longer want these emails, reply STOP.\n\nChinese Name Studio"],
  };
  if(!messages[step])return json({error:"Email sequence complete."},409);
  try {
    const mid=await sendMail(env,String(f.email),messages[step][0],messages[step][1]), t=now();
    await emailEvent(env,leadId,String(f.email),step,messages[step][0],"sent",mid);
    if(step===1)await env.DB.prepare("UPDATE email_followups SET status='active',step=1,last_sent_at=?,next_send_at=?,updated_at=? WHERE lead_id=?").bind(t,new Date(Date.now()+3*86400000).toISOString(),t,leadId).run();
    else if(step===2)await env.DB.prepare("UPDATE email_followups SET status='active',step=2,last_sent_at=?,next_send_at=?,updated_at=? WHERE lead_id=?").bind(t,new Date(Date.now()+4*86400000).toISOString(),t,leadId).run();
    else await env.DB.prepare("UPDATE email_followups SET status='completed',step=3,last_sent_at=?,next_send_at='',updated_at=? WHERE lead_id=?").bind(t,t,leadId).run();
    if(step===1)await env.DB.prepare("UPDATE leads SET status='email_sent',updated_at=? WHERE id=?").bind(t,leadId).run();
    return null;
  } catch { await env.DB.prepare("UPDATE email_followups SET last_error='Email provider send failed.',updated_at=? WHERE lead_id=?").bind(now(),leadId).run(); return json({error:"Email provider send failed."},502); }
}
async function dueFollowups(env:Env) {
  if(env.AUTO_FOLLOWUP_ENABLED!=="true")return;
  const f=await env.DB.prepare("SELECT lead_id FROM email_followups WHERE status='active' AND consent_confirmed=1 AND step IN (1,2) AND next_send_at<>'' AND next_send_at<=? ORDER BY next_send_at LIMIT 1").bind(now()).first<any>();
  if(f)await sendLead(env,String(f.lead_id),true);
}
async function receiveEmail(message:{from:string;to:string;raw:ReadableStream<Uint8Array>},env:Env) {
  const sender=String(message.from||"").trim().toLowerCase();if(!sender)return;
  let raw="";try{raw=await new Response(message.raw).text()}catch{}
  const stop=/(^|\n)\s*(stop|unsubscribe|remove me|opt out)\s*($|\n)/i.test(raw), status=stop?"unsubscribed":"replied";
  const rows=await env.DB.prepare("SELECT lead_id FROM email_followups WHERE lower(email)=? AND status IN ('active','consented')").bind(sender).all<any>();
  for(const r of rows.results||[]) {
    const t=now(),id=String(r.lead_id);
    await env.DB.prepare("UPDATE email_followups SET status=?,next_send_at='',last_reply_at=?,last_reply_subject=?,updated_at=? WHERE lead_id=?").bind(status,t,stop?"Unsubscribe request":"Customer replied",t,id).run();
    await env.DB.prepare("UPDATE leads SET status=?,updated_at=? WHERE id=?").bind(status,t,id).run();
  }
}
async function dashboard(env:Env) {
  const day=new Date().toISOString().slice(0,10);
  const [total,today,direct,high,active,platforms,runs,verified,orders]=await Promise.all([
    env.DB.prepare("SELECT COUNT(*) n FROM leads").first<any>(),
    env.DB.prepare("SELECT COUNT(*) n FROM leads WHERE created_at LIKE ?").bind(day+"%").first<any>(),
    env.DB.prepare("SELECT COUNT(*) n FROM leads WHERE created_at LIKE ? AND intent_type='direct_request'").bind(day+"%").first<any>(),
    env.DB.prepare("SELECT COUNT(*) n FROM leads WHERE created_at LIKE ? AND intent_score>=70").bind(day+"%").first<any>(),
    env.DB.prepare("SELECT * FROM search_runs WHERE status IN ('queued','running') ORDER BY started_at DESC LIMIT 1").first<any>(),
    env.DB.prepare("SELECT source_platform,COUNT(*) n FROM leads GROUP BY source_platform").all<any>(),
    env.DB.prepare("SELECT * FROM search_runs ORDER BY started_at DESC LIMIT 8").all<any>(),
    env.DB.prepare("SELECT COUNT(*) n FROM consents WHERE verified_at<>''").first<any>(),
    env.DB.prepare("SELECT COUNT(*) n FROM orders").first<any>(),
  ]);
  const map:Record<string,number>={};for(const r of platforms.results||[])map[String(r.source_platform)]=Number(r.n);
  const todayRuns=(runs.results||[]).filter((r:any)=>String(r.started_at||"").startsWith(day));
  const analyzed=todayRuns.reduce((s:number,r:any)=>s+Number(r.candidate_count||0),0);
  return {total_leads:Number(total?.n||0),today_new_leads:Number(today?.n||0),today_analyzed:analyzed,today_direct_requests:Number(direct?.n||0),today_worth_contacting:Number(high?.n||0),verified_consents:Number(verified?.n||0),total_orders:Number(orders?.n||0),average_intent_score:0,
    funnel:{analyzed,related:Number(direct?.n||0),purchase_intent:0,worth_contacting:Number(high?.n||0),contacted:0},platform_counts:map,email_status_counts:{},
    social_capabilities:{TikTok:{mode:"Google-indexed public pages"},Instagram:{mode:"Google-indexed public pages"},Facebook:{mode:"Google-indexed public pages"}},
    active_run:active||null,recent_runs:runs.results||[]};
}
async function revenue(env:Env) {
  const [orders,consents]=await Promise.all([
    env.DB.prepare("SELECT source,currency,COUNT(*) orders,SUM(amount) revenue,COUNT(DISTINCT email_hash) customers FROM orders GROUP BY source,currency").all<any>(),
    env.DB.prepare("SELECT source,COUNT(*) submitted,SUM(CASE WHEN verified_at<>'' THEN 1 ELSE 0 END) verified FROM consents GROUP BY source").all<any>(),
  ]);
  const map=new Map<string,any>();
  for(const r of consents.results||[])map.set(String(r.source),{source:String(r.source),submitted:Number(r.submitted),verified:Number(r.verified),orders:0,customers:0,revenue_by_currency:{}});
  for(const r of orders.results||[]) {
    const s=String(r.source),x=map.get(s)||{source:s,submitted:0,verified:0,orders:0,customers:0,revenue_by_currency:{}};
    x.orders+=Number(r.orders);x.customers+=Number(r.customers);x.revenue_by_currency[String(r.currency)]=Number(r.revenue||0);map.set(s,x);
  }
  const platforms=Array.from(map.values()).map(x=>({...x,conversion_rate:x.verified?Math.round(x.orders/x.verified*10000)/100:0}));
  return {platforms,total_orders:platforms.reduce((s,x)=>s+x.orders,0),total_verified_consents:platforms.reduce((s,x)=>s+x.verified,0),note:"Revenue is grouped by currency; orders require a trusted payment webhook."};
}
async function admin(req:Request,env:Env,path:string) {
  if(!await adminAllowed(req,env))return json({error:"Cloudflare Access sign-in required."},401);
  if(path==="/name-leads/search"&&req.method==="POST") {
    const b:any=await req.json().catch(()=>({})),id=crypto.randomUUID();
    await env.DB.prepare("INSERT INTO search_runs (id,status,provider,queries_total,started_at) VALUES (?,'queued','Serper + Agnes AI',?,?)").bind(id,QUERIES.length,now()).run();
    await env.SEARCH_QUEUE.send({runId:id,countryCode:String(b.country_code||env.SEARCH_COUNTRY_CODE||"us"),limit:Math.max(1,Math.min(50,Number(b.max_candidates||50)))});
    return json({run_id:id,status:"queued"},202);
  }
  if(path.startsWith("/name-leads/search-runs/")&&req.method==="GET") {
    const id=path.split("/").pop()||"",r=await env.DB.prepare("SELECT * FROM search_runs WHERE id=?").bind(id).first<any>();
    return r?json(r):json({error:"Search run not found."},404);
  }
  if(path==="/name-leads/dashboard"&&req.method==="GET")return json(await dashboard(env));
  if(path==="/name-leads/revenue"&&req.method==="GET")return json(await revenue(env));
  if(path==="/name-leads"&&req.method==="GET") {
    const u=new URL(req.url),status=u.searchParams.get("status"),platform=u.searchParams.get("platform"),min=Math.max(0,Math.min(100,Number(u.searchParams.get("min_score")||0)));
    let sql="SELECT * FROM leads WHERE intent_score>=?",args:any[]=[min];
    if(status){sql+=" AND status=?";args.push(status)}if(platform){sql+=" AND source_platform=?";args.push(platform)}
    const r=await env.DB.prepare(sql+" ORDER BY intent_score DESC,updated_at DESC LIMIT 200").bind(...args).all<any>();
    return json({leads:r.results||[],count:(r.results||[]).length});
  }
  const status=path.match(/^\/name-leads\/([^/]+)\/status$/);
  if(status&&req.method==="PATCH") {
    const b:any=await req.json().catch(()=>({})),allowed=["new","reviewed","ready_to_contact","contacted","email_consented","email_sent","replied","converted","dismissed","unsubscribed"];
    if(!allowed.includes(String(b.status)))return json({error:"Invalid status."},400);
    await env.DB.prepare("UPDATE leads SET status=?,updated_at=? WHERE id=?").bind(String(b.status),now(),decodeURIComponent(status[1])).run();
    return json({status:"updated"});
  }
  const email=path.match(/^\/name-leads\/([^/]+)\/email\/start$/);
  if(email&&req.method==="POST")return (await sendLead(env,decodeURIComponent(email[1]),false))||json({status:"sent"});
  return json({error:"Admin route not found."},404);
}
async function api(req:Request,env:Env) {
  const path=new URL(req.url).pathname.replace(/^\/api\/v1/,"");
  if(path==="/health"&&req.method==="GET")return json({status:"ok",runtime:"cloudflare-workers",database:"d1"});
  if(path==="/public/name-request"&&req.method==="POST")return publicSubmit(req,env);
  if(path==="/public/name-request/verify"&&req.method==="GET")return verifySubmit(req,env);
  if(path==="/public/orders"&&req.method==="POST") {
    const expected=env.ORDER_WEBHOOK_SECRET||"";if(!expected||req.headers.get("X-Order-Webhook-Secret")!==expected)return json({error:"Invalid webhook secret."},401);
    const b:any=await req.json().catch(()=>({})),email=String(b.email||"").trim().toLowerCase();
    if(!email||!b.order_id||!Number.isFinite(Number(b.amount)))return json({error:"Invalid order payload."},422);
    const eh=await hash(email),c=await env.DB.prepare("SELECT lead_id,source,referral_code FROM consents WHERE email_hash=? AND verified_at<>'' ORDER BY verified_at DESC LIMIT 1").bind(eh).first<any>();
    try{await env.DB.prepare("INSERT INTO orders (order_id,lead_id,email_hash,source,referral_code,product,amount,currency,ordered_at) VALUES (?,?,?,?,?,?,?,?,?)")
      .bind(String(b.order_id),String(c?.lead_id||""),eh,cleanSource(String(b.source||c?.source||"direct")),String(b.referral_code||c?.referral_code||""),String(b.product||"Chinese name service"),Number(b.amount),String(b.currency||"USD").toUpperCase(),now()).run()}
    catch{return json({error:"Duplicate or invalid order."},409)}
    return json({status:"recorded",order_id:String(b.order_id)});
  }
  if(path.startsWith("/admin/"))return admin(req,env,path.replace(/^\/admin/,""));
  return json({error:"Not found."},404);
}
export default {
  async fetch(req:Request,env:Env,ctx:ExecutionContext):Promise<Response> {
    if(new URL(req.url).pathname.startsWith("/api/")){try{return await api(req,env)}catch(e:any){console.error("API error",String(e?.message||e));return json({error:"Internal API error."},500)}}
    return env.ASSETS.fetch(req);
  },
  async scheduled(event:ScheduledController,env:Env,ctx:ExecutionContext) {
    if(event.cron==="0 */6 * * *") {
      const id=crypto.randomUUID();await env.DB.prepare("INSERT INTO search_runs (id,status,provider,queries_total,started_at) VALUES (?,'queued','Serper + Agnes AI',?,?)").bind(id,QUERIES.length,now()).run();
      ctx.waitUntil(env.SEARCH_QUEUE.send({runId:id,countryCode:env.SEARCH_COUNTRY_CODE||"us",limit:50}));
    }
    if(event.cron==="*/15 * * * *")ctx.waitUntil(dueFollowups(env));
  },
  async queue(batch:MessageBatch<SearchMessage>,env:Env) {
    for(const m of batch.messages){try{await runSearch(env,m.body.runId,m.body.countryCode,m.body.limit);m.ack()}catch(e){console.error("Queue search failed",String(e));m.retry()}}
  },
  async email(message:{from:string;to:string;raw:ReadableStream<Uint8Array>},env:Env) { await receiveEmail(message,env); },
};
