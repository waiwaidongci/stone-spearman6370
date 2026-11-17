import {DOMAIN} from '../constants/domain.js';
import {store,load,loadRows} from '../stores/research.js';
import {merges} from '../api/research.js';
import {studySelect} from '../components/common/study-select.js';
import {table} from '../components/common/table.js';
import {esc,fmt} from '../utils/format.js';
import {bindForm} from '../hooks/forms.js';

const STATUS_LABEL={awaiting:'等待另一方修订包',conflicts:'冲突待裁决',resolved:'裁决完成可落地',applied:'已整批落地',superseded:'已被新会话取代'};
const KIND_LABEL={same_item:'同项异改',delete_modify:'撤回遇修改',add_collision:'双方新增编号占用',key_occupation:'编号占用'};
const uuid=()=> (crypto.randomUUID?crypto.randomUUID():'idem-'+Date.now()+'-'+Math.random().toString(16).slice(2));

// Canonical JSON must match the backend checksum helper: sorted keys, no
// insignificant whitespace, non-ASCII kept as-is.
function canonical(value){
  if(value===null||typeof value!=='object')return JSON.stringify(value);
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
}
async function sha256(value){
  const buf=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(canonical(value)));
  return [...new Uint8Array(buf)].map(b=>b.toString(16).padStart(2,'0')).join('');
}

export async function render(){
  await load();await loadRows();
  const role=localStorage.getItem('research-role');
  const writable=role!=='viewer';
  const sessions=await merges.sessions(store.studyId);
  const active=store.mergeId&&sessions.find(s=>s.id===store.mergeId)?store.mergeId:(sessions[0]?.id||0);
  store.mergeId=active;
  const detail=active?await merges.get(active):null;
  // Base worksheet checksum: the same canonical SHA256 the backend computes.
  const basePayload=store.rows.map(r=>({id:r.id,source_key:r.source_key,values:r.values}));
  const baseChecksum=await sha256(basePayload);
  return `<div class="eyebrow">06 / OFFLINE MERGE WORKBENCH</div>
<h1>离线修订三方合并</h1>
<p class="lead">两家试验室基于同一份共同底稿各自离线修订。系统以稳定身份（行 id）跟踪改名：双方改不同测值自动合并；同项异改、撤回遇修改、编号占用留待裁决；编号互换可整体落地。冲突未决前正式观测保持不变。</p>
<div class="toolbar">${studySelect()}</div>
${writable?uploadPanel():'<p class="method-note">观察员仅可查看合并会话与合并依据。</p>'}
<h2>合并会话 <small>${sessions.length} 个 · 按批次隔离</small></h2>
${sessionTable(sessions,active)}
${detail?basisPanel(detail,role):'<p class="method-note">该批次暂无合并会话。先由任一试验室回传修订包。</p>'}`;
}

function uploadPanel(){
  const sample=store.rows.slice(0,3).map(r=>({id:r.id,source_key:r.source_key,values:r.values}));
  const full=JSON.stringify(store.rows.map(r=>({id:r.id,source_key:r.source_key,values:r.values})),null,2);
  return `<details ${store.rows.length?'':'hidden'}><summary>回传试验室修订包（JSON）</summary>
<form id="package-form" class="worksheet">
  <div class="inline-form">
    <label>试验室名称<input name="lab_name" required maxlength="80" placeholder="如 北山试验室"></label>
    <label>修订人<input name="author" required maxlength="120" value="${esc(localStorage.getItem('research-email')||'')}"></label>
    <label>修订号 revision<input name="revision" type="number" min="1" value="1" required></label>
    <label>共同底稿 SHA256（自动填充，与后端一致）<input name="base_checksum" required minlength="8" value="${baseChecksum||''}" readonly></label>
  </div>
  <label>修订包 rows（沿用底稿行带 id；新增行带 client_ref；撤回即从数组移除该 id）
    <textarea name="rows_json" class="json-box" required>${esc(full||JSON.stringify(sample,null,2))}</textarea>
  </label>
  <p class="checksum">每行必须含全部字段 ${DOMAIN.fields.map(f=>f[0]).join(' / ')}；同一 id 的改名会被识别为同一试件。</p>
  <button type="submit">接纳修订包 →</button>
</form></details>`;
}

function sessionTable(sessions,active){
  return table(['会话','状态','底稿SHA256','冲突','漂移次数','更新时间'],sessions.map(s=>[
    {html:`<a href="#merge/${s.id}">#${s.id} ${esc(s.note||'离线合并')}</a>`},
    {html:`<span class="status ${s.status}">${STATUS_LABEL[s.status]||s.status}</span>`},
    s.base_checksum.slice(0,16)+'…',
    s.conflict_count,
    s.drift_revision,
    s.updated_at,
  ]));
}

function basisPanel(d,role){
  const plan=d.plan;
  if(!plan) return `<h2>会话 #${d.id} 合并依据</h2><p class="method-note">修订包尚未到齐（${STATUS_LABEL[d.status]}）。</p>${packageMeta(d)}`;
  const conflicts=plan.conflicts||[];
  const writable=role!=='viewer';
  return `<h2>会话 #${d.id} 合并依据 <small>${STATUS_LABEL[d.status]}</small></h2>
${packageMeta(d)}
<div class="strip"><span>底稿 SHA256 <b class="checksum">${d.base_checksum}</b></span><span>自动合并 ${plan.basis.filter(b=>['modified','added','unchanged'].includes(b.operation)).length} 行</span><span>撤回 ${plan.basis.filter(b=>b.operation==='deleted').length} 行</span><span>冲突 ${conflicts.length} 项</span></div>
${(plan.reused_decisions?.length||plan.dropped_decisions?.length)?`<p class="method-note">正式批次变化后已重算：沿用裁决 ${plan.reused_decisions.length} 项（三方内容未变），失效裁决 ${plan.dropped_decisions.length} 项（内容已变需重新裁决）。</p>`:''}
${conflicts.length?conflictPanel(conflicts,writable):''}
${writable&&d.status==='conflicts'?`<div class="toolbar"><button id="adjudicate-btn" type="button">提交本页裁决 →</button></div>`:''}
${writable&&d.status==='resolved'?`<div class="toolbar"><button id="apply-btn" type="button" ${role!=='admin'?'disabled title="仅负责人可整批落地"':''}>整批原子替换正式观测（仅负责人）</button></div>`:''}
${d.status==='applied'?`<p class="method-note">已落地。正式批次现为新底稿，新研究可在此基础上冻结输入；历史运行快照不受影响。结果校验 SHA256 <b class="checksum">${d.result_checksum||''}</b></p>`:''}
<h2>逐行合并依据 <small>共同底稿 → 甲/乙修订 → 处理结论</small></h2>
${basisTable(plan)}
<details><summary>完整合并计划 JSON（含冲突指纹与裁决依据）</summary><pre>${esc(JSON.stringify(plan,null,2))}</pre></details>`;
}

function packageMeta(d){
  const cell=slot=>{const p=d[slot];return p?`${esc(p.lab_name)} / ${esc(p.author)} / rev${p.revision}<br><small class="checksum">${p.checksum.slice(0,20)}… · ${esc(p.received_at||'')}</small>`:'<i>未到齐</i>';};
  return `<div class="inline-form">
    <label>甲实验室（A）<div class="pkg-card">${cell('revision_a')}</div></label>
    <label>乙实验室（B）<div class="pkg-card">${cell('revision_b')}</div></label>
  </div>`;
}

function conflictPanel(conflicts,writable){
  return conflicts.map(c=>{
    const body=conflictBody(c);
    const form=writable?conflictForm(c):'<p class="method-note">仅分析师/负责人可裁决。</p>';
    return `<section class="conflict-card" data-cid="${c.conflict_id}">
      <h3>${c.conflict_id} · ${KIND_LABEL[c.kind]||c.kind} <small>身份 ${esc(c.identity)}</small></h3>
      ${body}${form}
    </section>`;
  }).join('');
}

function conflictBody(c){
  const j=x=>esc(JSON.stringify(x,null,0));
  if(c.kind==='same_item')
    return table(['','编号','测值'],[['共同底稿',c.detail.base.source_key,j(c.detail.base.values)],['甲 A',c.detail.state_a.source_key,j(c.detail.state_a.values)],['乙 B',c.detail.state_b.source_key,j(c.detail.state_b.values)]]);
  if(c.kind==='delete_modify')
    return table(['','内容'],[['共同底稿',j(c.detail.base)],['撤回方',c.detail.delete_side+' 方要求删除'],['修改方',c.detail.keep_side+' 方保留为 '+j(c.detail.kept_state)]]);
  if(c.kind==='add_collision')
    return table(['方','身份','编号','测值'],c.detail.claims.map(cl=>[cl.side,cl.identity,cl.state.source_key,j(cl.state.values)]));
  return table(['占用方身份','来源','候选编号','测值'],c.detail.claimants.map(cl=>[cl.identity,cl.source,cl.state.source_key,j(cl.state.values)]));
}

function conflictForm(c){
  if(c.kind==='same_item')
    return `<div class="inline-form" data-decision="${c.conflict_id}" data-kind="${c.kind}">
      <label>裁决<select name="choice"><option value="A">采用甲实验室</option><option value="B">采用乙实验室</option></select></label>
      <label>改编号（可选，留空沿用所选方案编号）<input name="rename_key" maxlength="100" placeholder="可选"></label>
    </div>`;
  if(c.kind==='delete_modify')
    return `<div class="inline-form" data-decision="${c.conflict_id}" data-kind="${c.kind}">
      <label>裁决<select name="choice"><option value="keep">保留修改方内容</option><option value="delete">执行撤回（删除该行）</option></select></label>
      <label>改编号（可选）<input name="rename_key" maxlength="100" placeholder="可选"></label>
    </div>`;
  if(c.kind==='add_collision')
    return `<div class="inline-form" data-decision="${c.conflict_id}" data-kind="${c.kind}">
      <label>裁决<select name="choice"><option value="A">保留甲实验室新增行</option><option value="B">保留乙实验室新增行</option><option value="drop">双方新增全部舍弃</option></select></label>
      <label>改编号（可选）<input name="rename_key" maxlength="100" placeholder="可选"></label>
    </div>`;
  const idents=c.detail.claimants.map(cl=>cl.identity);
  return `<div data-decision="${c.conflict_id}" data-kind="${c.kind}">
    <p>为每个占用方指定最终编号（互换场景直接交换填写即可，落地方案内编号必须互不重复）：</p>
    <div class="inline-form">${idents.map(idt=>`<label>${esc(idt)} 最终编号<input name="resolution" data-ident="${esc(idt)}" required value=""></label>`).join('')}</div>
  </div>`;
}

function basisTable(plan){
  const opLabel={unchanged:'未变',modified:'自动合并修改',added:'新增',deleted:'撤回',conflict:'待裁决'};
  return table(['身份','共同底稿','甲修订','乙修订','处理','依据'],plan.basis.map(e=>[
    e.identity,
    e.base?`${e.base.source_key} ${fmt(e.base.values)}`:'—（新增）',
    e.side_a?`${e.side_a.source_key} ${fmt(e.side_a.values)}`:'<i>撤回/无</i>',
    e.side_b?`${e.side_b.source_key} ${fmt(e.side_b.values)}`:'<i>撤回/无</i>',
    {html:`<span class="status ${e.operation==='conflict'?'failed':'completed'}">${opLabel[e.operation]||e.operation}</span>`},
    e.reason+(e.conflict_ids?.length?` · ${e.conflict_ids.join(', ')}`:''),
  ]));
}

export function bind(refresh){
  const sel=document.getElementById('study-select');
  if(sel)sel.onchange=()=>{store.studyId=+sel.value;store.mergeId=0;refresh();};
  bindForm('package-form',async data=>{
    let rows;
    try{rows=JSON.parse(data.rows_json);}catch(e){throw new Error('修订包不是合法 JSON：'+e.message);}
    const payload={lab_name:data.lab_name,author:data.author,revision:+data.revision,
      base_checksum:data.base_checksum,idempotency_key:uuid(),rows};
    const s=await merges.upload(store.studyId,payload);
    store.mergeId=s.id;await load();refresh();
  });
  document.querySelectorAll('#content a[href^="#merge/"]').forEach(a=>{
    a.onclick=()=>{store.mergeId=+a.getAttribute('href').split('/')[1];};
  });
  const btn=document.getElementById('adjudicate-btn');
  if(btn)btn.onclick=async()=>{
    const decisions=[];
    for(const node of document.querySelectorAll('[data-decision]')){
      const cid=node.dataset.decision;const kind=node.dataset.kind;
      if(kind==='key_occupation'){
        const resolution={};
        node.querySelectorAll('input[name=resolution]').forEach(inp=>resolution[inp.dataset.ident]=inp.value.trim());
        if(Object.values(resolution).some(v=>!v))throw new Error(cid+' 的最终编号不能为空');
        decisions.push({conflict_id:cid,resolution});
      }else{
        const d={conflict_id:cid,choice:node.querySelector('[name=choice]').value};
        const rk=node.querySelector('[name=rename_key]')?.value.trim();
        if(rk)d.rename_key=rk;
        decisions.push(d);
      }
    }
    if(!decisions)return;
    const out=await merges.decide(store.mergeId,{decisions,idempotency_key:uuid()});
    store.mergeId=out.id;refresh();
  };
  const applyBtn=document.getElementById('apply-btn');
  if(applyBtn)applyBtn.onclick=async()=>{
    if(!confirm('整批替换正式观测？该动作在一个事务内原子完成，历史研究快照不变。'))return;
    const out=await merges.apply(store.mergeId,uuid());
    store.mergeId=out.id;await loadRows();refresh();
  };
}
