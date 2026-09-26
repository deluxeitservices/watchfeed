/* Shared UI helpers. The preview database is deliberately isolated; never request
   account-specific or mutation endpoints when the server reports demo mode. */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const COND = {NEW:"New",LIKE_NEW:"Like new",USED:"Pre-owned",VINTAGE:"Vintage"};
const SETS = {FULL_SET:"Full set",WATCH_ONLY:"Watch only",BOX_ONLY:"Box only",PAPERS_ONLY:"Papers only"};
const RATING = {TRUSTED:"✓ Trusted",OK:"OK",CAUTION:"⚠ Caution",BLOCKED:"⛔ Blocked"};
const TRACK = {CONTACTED:"Contacted",OFFERED:"Offered",BOUGHT:"Bought",PASSED:"Passed"};
let BASE = "GBP";
const flag = cc => cc && /^[A-Z]{2}$/.test(cc) ? String.fromCodePoint(...[...cc].map(c => 127397 + c.charCodeAt())) : "";
function money(v, cur = BASE){
  if (v == null) return "";
  try { return new Intl.NumberFormat("en-GB",{style:"currency",currency:cur,maximumFractionDigits:0}).format(v); }
  catch { return `${cur||""} ${Math.round(v).toLocaleString("en-GB")}`; }
}
function ago(iso){
  if (!iso) return "";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s/60))} min ago`;
  if (s < 86400) return `${Math.round(s/3600)} h ago`;
  if (s < 172800) return "Yesterday";
  return new Date(iso).toLocaleDateString("en-GB",{day:"numeric",month:"short"});
}
function photoUrl(id, size = "thumb"){ return window.photoOverride ? window.photoOverride(id, size) : `/media/${id}/${size}`; }

let PREVIEW_MODE = false;
const previewReady = fetch("/health", {credentials:"same-origin"}).then(r => r.json()).then(s => {
  PREVIEW_MODE = !!s.demo;
  if (PREVIEW_MODE) {
    let banner = $("demo-banner");
    if (!banner) {
      banner = document.createElement("div"); banner.id = "demo-banner"; banner.className = "demo-banner";
      banner.textContent = "Demo preview — these are fictional sample offers. Live WhatsApp ingestion and account-specific features are not enabled.";
      const header = document.querySelector("header.top") || document.querySelector("header");
      if (header) header.insertAdjacentElement("afterend", banner);
    }
    banner.hidden = false;
  }
  return PREVIEW_MODE;
}).catch(() => {
  PREVIEW_MODE = true;
  let banner = $("demo-banner");
  if (!banner) {
    banner = document.createElement("div"); banner.id = "demo-banner"; banner.className = "demo-banner";
    const header = document.querySelector("header.top") || document.querySelector("header");
    if (header) header.insertAdjacentElement("afterend", banner);
  }
  banner.hidden = false;
  banner.textContent = "Preview status unavailable — account-specific features are disabled until the server can be checked.";
  return true;
});
function isPreview(){ return previewReady; }

function previewSafe(path, method){
  if (!PREVIEW_MODE) return true;
  const read = method === "GET";
  return read && (path === "/api/facets" || /^\/api\/offers(?:\?.*|\/\d+)?$/.test(path));
}
async function api(path, method = "GET", body){
  await previewReady;
  if (!previewSafe(path, method)) throw new Error("This feature is not available in the demo preview.");
  const r = await fetch(path, {method, credentials:"same-origin", cache: method === "GET" ? "no-store" : "default",
    headers: body ? {"Content-Type":"application/json"} : {}, body: body ? JSON.stringify(body) : undefined});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
}
function toast(msg){
  let t = $("toast"); if (!t){ t = document.createElement("div"); t.id = "toast"; t.className = "toast"; }
  const dlg = $("modal"); (dlg && dlg.open ? dlg : document.body).append(t);
  t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2600);
}

function renderNav(active){
  const links = [["/","Market feed"],["/advisor","Buy advisor"],["/alerts","Alerts"],["/dealers","Dealers"],["/stock","Our stock"]];
  const nav = document.querySelector(".nav"), map = window.NAV_MAP || {};
  if (nav) nav.innerHTML = links.map(([h,t]) => `<a href="${map[h] || h}" class="${h === active ? "on" : ""}">${t}</a>`).join("");
  previewReady.then(demo => {
    if (!demo) api("/api/me").then(m => { BASE = m.base_currency || BASE; const u = document.querySelector(".user"); if (u) u.textContent = m.user; }).catch(() => {});
  });
}

function dealBadge(o){
  if (o.deal === "DEAL") return `<span class="badge deal" title="Priced ${Math.abs(Math.round(o.market_pct*100))}% below what other dealers ask for this reference">DEAL ${Math.round(o.market_pct*100)}%</span>`;
  if (o.deal === "CHECK") return `<span class="badge check" title="Far below market — probably a typo or misread price">check price</span>`;
  return "";
}
const ratingBadge = r => r ? `<span class="badge r-${r}">${RATING[r]}</span>` : "";
const trackBadge = t => t && t.status ? `<span class="badge t-${t.status}" title="${esc(t.note||"")}">${TRACK[t.status]} · ${esc(t.by||"")}</span>` : "";
function thumbCell(o){
  if (!o.photo_id) return `<div class="thumb ph">no photo</div>`;
  const tag = o.photo_kind === "REFERENCE" ? `<span class="tag">ref photo</span>` : o.photo_kind === "MESSAGE" ? `<span class="tag">msg photo</span>` : "";
  return `<div class="thumbwrap"><img class="thumb" loading="lazy" src="${photoUrl(o.photo_id)}" data-full="${photoUrl(o.photo_id,"full")}" alt="">${tag}</div>`;
}
function lightbox(src){
  let lb = $("lightbox");
  if (!lb){ lb = document.createElement("div"); lb.id = "lightbox"; lb.innerHTML = "<img alt=''>"; lb.onclick = () => lb.style.display = "none"; document.body.append(lb); }
  lb.querySelector("img").src = src; lb.style.display = "flex";
}
document.addEventListener("click", e => {
  const im = e.target.closest("img[data-full]"); if (im){ e.stopPropagation(); lightbox(im.dataset.full); return; }
  const a = e.target.closest("[data-offer],[data-dealer],[data-ref]");
  if (!a) return;
  e.preventDefault();
  if (a.dataset.offer) openOffer(a.dataset.offer);
  else if (a.dataset.dealer) openDealer(a.dataset.dealer);
  else if (a.dataset.ref) openReference(a.dataset.ref);
});
function modal(title, html){
  let d = $("modal");
  if (!d){ d = document.createElement("dialog"); d.id = "modal"; d.className = "modal";
    d.innerHTML = `<div class="hd"><strong id="mtitle"></strong><button onclick="modal.close()">Close</button></div><div class="bd" id="mbody"></div>`;
    d.addEventListener("click", e => { if (e.target === d) d.close(); }); document.body.append(d); }
  $("mtitle").textContent = title; $("mbody").innerHTML = html;
  if (!d.open) d.showModal();
  return $("mbody");
}
modal.close = () => { const d = $("modal"); if (d) d.close(); };
function briefTable(list){
  if (!list.length) return `<p class="muted">None in the last 30 days.</p>`;
  return `<div style="overflow:auto"><table><thead><tr><th></th><th>Seen</th><th>Type</th><th>Watch</th><th>Cond.</th><th>Set</th><th>Year</th><th>Price</th><th>Dealer</th></tr></thead><tbody>
  ${list.map(o => `<tr><td>${o.photo_id ? `<img class="thumb" style="width:40px;height:40px" src="${photoUrl(o.photo_id)}" data-full="${photoUrl(o.photo_id,"full")}" alt="">` : ""}</td>
    <td class="small">${ago(o.last_seen_at)}</td><td><span class="badge dir ${o.direction}">${o.direction}</span></td>
    <td><button class="linkish" data-offer="${o.id}">${esc(o.reference || o.family || o.brand || "—")}</button><div class="small muted">${esc([o.brand,o.family,o.dial_color].filter(Boolean).join(" · "))}</div></td>
    <td>${esc(COND[o.condition]||"")}</td><td>${esc(SETS[o.set_type]||"")}</td><td>${esc(o.year||"")}</td>
    <td class="price">${o.price != null ? esc(money(o.price,o.currency)) : ""}</td>
    <td>${o.dealer_key ? `<button class="linkish" data-dealer="${esc(o.dealer_key)}">${esc(o.dealer_name || "+"+o.dealer_phone)}</button>` : esc(o.dealer_name||"")}</td></tr>`).join("")}
  </tbody></table></div>`;
}
async function openOffer(id){
  const b = modal("Loading…", "<p class='muted'>Loading…</p>");
  const o = await api(`/api/offers/${id}`);
  $("mtitle").textContent = `${o.direction} · ${[o.brand, o.family].filter(Boolean).join(" ")} ${o.reference || ""}`;
  let msg = esc(o.original_message || "");
  const src = esc((o.source_text || "").trim()); if (src && msg.includes(src)) msg = msg.replace(src, `<mark>${src}</mark>`);
  const photos = (o.photos || []).map(p => `<img src="${photoUrl(p,"full")}" data-full="${photoUrl(p,"full")}" alt="Dealer photo">`).join("");
  const t = o.track || {};
  b.innerHTML = `${photos ? `<div class="gallery">${photos}</div>${o.photo_kind === "REFERENCE" ? `<p class="small muted">Reference photo from another listing of this model — not this exact watch.</p>` : ""}` : ""}
    <div class="row" style="margin-bottom:8px">${dealBadge(o)} ${o.in_stock ? `<span class="badge stockb">In our stock</span>` : ""}
      <span class="chip">${esc(COND[o.condition]||"Condition ?")}</span><span class="chip">${esc(SETS[o.set_type]||"Set ?")}</span>
      ${o.year ? `<span class="chip">${o.month ? String(o.month).padStart(2,"0")+"/" : ""}${o.year}</span>` : ""}
      ${o.price != null ? `<span class="chip"><b>${esc(money(o.price,o.currency))}</b></span>` : ""}
      ${o.reference_norm ? `<button class="linkish" data-ref="${esc(o.reference_norm)}">Price history for ${esc(o.reference)} →</button>` : ""}</div>
    <p style="margin:6px 0">Dealer: ${o.dealer_key ? `<button class="linkish" data-dealer="${esc(o.dealer_key)}">${esc(o.dealer_name || "")}</button>` : esc(o.dealer_name||"")}
      ${o.dealer_phone ? ` · <a href="https://wa.me/${esc(o.dealer_phone)}" target="_blank" rel="noopener">WhatsApp +${esc(o.dealer_phone)}</a>` : ""} ${ratingBadge(o.dealer_rating)}
      <span class="muted small"> · ${esc((o.groups||[]).join(", "))} · seen ${o.seen_count}×</span></p>
    <h2 style="margin-top:14px">Original message</h2><pre>${msg}</pre>
    <h2 style="margin-top:16px">Staff tracking</h2><div class="row">${Object.entries(TRACK).map(([k,v]) => `<button data-track="${k}" class="${t.status === k ? "primary" : ""}">${v}</button>`).join("")}<button data-track="">Clear</button></div>
    <textarea id="tnote" rows="2" style="width:100%;margin-top:8px" placeholder="Staff note">${esc(t.note||"")}</textarea>
    <p class="small muted">${t.status ? `${TRACK[t.status]} by ${esc(t.by)} · ${ago(t.at)}` : "Not handled yet"}</p>`;
  b.querySelectorAll("[data-track]").forEach(btn => btn.onclick = async () => {
    try { const r = await api(`/api/offers/${id}/track`, "POST", {status: btn.dataset.track || null, note: $("tnote").value});
      b.querySelectorAll("[data-track]").forEach(x => x.classList.toggle("primary", x.dataset.track === (r.status || "#")));
      toast("Saved"); document.dispatchEvent(new CustomEvent("offer-updated")); }
    catch (e) { toast(e.message); }
  });
}
async function openDealer(key){
  const b = modal("Dealer", "<p class='muted'>Loading…</p>");
  const d = await api(`/api/dealers/${encodeURIComponent(key)}`);
  $("mtitle").textContent = d.name || ("+" + d.phone);
  b.innerHTML = `<div class="row">${d.phone ? `<a href="https://wa.me/${esc(d.phone)}" target="_blank" rel="noopener">WhatsApp +${esc(d.phone)}</a>` : ""} ${ratingBadge(d.rating)}</div>
    <div class="kv"><div><span>Listings (90 days)</span><b>${d.listings}</b></div><div><span>Selling / wanting</span><b>${d.wts} / ${d.wtb}</b></div>
    <div><span>Prices vs other dealers</span><b>${d.vs_market_pct == null ? "—" : d.vs_market_pct + "%"}</b></div><div><span>Groups</span><b>${d.groups}</b></div>
    <div><span>Last seen</span><b>${ago(d.last_seen_at)||"—"}</b></div></div><p class="small muted">${(d.top_brands||[]).map(x => `${esc(x.brand)} (${x.count})`).join(" · ")} ${esc((d.group_names||[]).join(", "))}</p>
    <h2>Staff rating &amp; note</h2><div class="seg" id="rseg">${["TRUSTED","OK","CAUTION","BLOCKED"].map(r => `<button data-r="${r}" class="${d.rating === r ? "on" : ""}">${RATING[r]}</button>`).join("")}<button data-r="">None</button></div>
    <textarea id="dnote" rows="3" style="width:100%;margin-top:8px">${esc(d.note||"")}</textarea><div class="row"><button class="primary" id="dsave">Save</button></div>
    <h2 style="margin-top:14px">Recent listings</h2>${briefTable(d.recent||[])}`;
  let rating = d.rating || "";
  b.querySelectorAll("[data-r]").forEach(x => x.onclick = () => { rating = x.dataset.r; b.querySelectorAll("[data-r]").forEach(y => y.classList.toggle("on", y === x && rating)); });
  $("dsave").onclick = async () => { try { await api(`/api/dealers/${encodeURIComponent(key)}`, "PUT", {rating: rating || null, note: $("dnote").value}); toast("Dealer saved"); document.dispatchEvent(new CustomEvent("offer-updated")); } catch(e) { toast(e.message); } };
}
async function openReference(ref){
  const b = modal(ref, "<p class='muted'>Loading…</p>");
  const r = await api(`/api/refs/${encodeURIComponent(ref)}`);
  BASE = r.base_currency || BASE;
  $("mtitle").textContent = `${[r.brand, r.family].filter(Boolean).join(" ")} ${r.reference}`;
  b.innerHTML = `<div class="kv"><div><span>Typical ask (new, full set)</span><b>${r.typical_new_full_set ? money(r.typical_new_full_set) : "—"}</b></div>
    <div><span>Dealers selling (30d)</span><b>${r.wts_dealers_30d}</b></div><div><span>Dealers wanting (30d)</span><b>${r.wtb_dealers_30d}</b></div><div><span>In our stock</span><b>${r.in_stock.length}</b></div></div>
    <h2>Typical dealer asking price, weekly</h2><div id="refchart"></div><h2>Recent listings</h2>${briefTable(r.recent||[])}
    <p class="small"><a href="/advisor?ref=${encodeURIComponent(r.reference)}">Value a customer's ${esc(r.reference)} in the Buy advisor →</a></p>`;
  priceChart($("refchart"), r.history||[]);
}
function priceChart(el, hist){
  const pts = hist.map((h,i)=>({...h,i})), vals = pts.filter(p=>p.median!=null);
  if (vals.length < 2){ el.innerHTML = `<p class="muted small">Not enough priced listings yet to draw a trend (needs 2+ weeks of data).</p>`; return; }
  const W=760,H=240,L=64,R=12,T=12,B=28, lo=Math.min(...vals.map(p=>p.p25??p.median)), hi=Math.max(...vals.map(p=>p.p75??p.median));
  const raw=((hi-lo)||hi*.1)/3, mag=Math.pow(10,Math.floor(Math.log10(raw))), step=[1,2,2.5,5,10].map(m=>m*mag).find(v=>v>=raw);
  const y0=Math.max(0,Math.floor(lo/step)*step-(lo%step===0?step:0)), y1=Math.ceil(hi/step)*step;
  const x=i=>L+(W-L-R)*i/(pts.length-1), y=v=>T+(H-T-B)*(1-(v-y0)/(y1-y0));
  let line="", band=[], run=[];
  const flush=()=>{if(run.length>1)band.push(`M${run.map(p=>`${x(p.i)},${y(p.p75)}`).join("L")}L${run.slice().reverse().map(p=>`${x(p.i)},${y(p.p25)}`).join("L")}Z`);run=[];};
  pts.forEach(p=>{if(p.median!=null){line+=`${line.endsWith(" ")||!line?"M":"L"}${x(p.i).toFixed(1)},${y(p.median).toFixed(1)}`;} if(p.p25!=null&&p.p75!=null)run.push(p);else flush();});flush();
  const ticks=[];for(let v=y0;v<=y1+step/2;v+=step)ticks.push(v);
  el.className="chart";el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Weekly typical asking price">
    ${ticks.map(v=>`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#e8ece8"/><text class="axis" x="${L-8}" y="${y(v)+4}" text-anchor="end">${money(v)}</text>`).join("")}
    ${band.map(d=>`<path d="${d}" fill="var(--band)"/>`).join("")}<path d="${line}" fill="none" stroke="var(--brand2)" stroke-width="2"/>
    <rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="transparent" id="chit"/></svg><div class="tip"></div>`;
  const svg=el.querySelector("svg"), tip=el.querySelector(".tip");
  el.querySelector("#chit").addEventListener("mousemove",ev=>{const bb=svg.getBoundingClientRect(),sx=(ev.clientX-bb.left)*W/bb.width,p=pts[Math.max(0,Math.min(pts.length-1,Math.round((sx-L)/(W-L-R)*(pts.length-1))))];tip.innerHTML=`Week of ${new Date(p.week_start).toLocaleDateString("en-GB",{day:"numeric",month:"short"})}<br>${p.median!=null?`<b>${money(p.median)}</b> typical · ${p.count} asks`:"No priced listings"}`;tip.style.left=(x(p.i)*bb.width/W)+"px";tip.style.display="block";});
  el.querySelector("#chit").addEventListener("mouseleave",()=>tip.style.display="none");
}