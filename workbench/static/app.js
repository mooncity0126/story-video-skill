'use strict';
const $ = (q, root = document) => root.querySelector(q);
const state = {projects: [], data: null, tab: 'shots', scene: 'all', filter: 'all', layout: 'cards', search: '', token: '', modal: null, busy: false, signature: ''};
const initialTab = new URLSearchParams(location.search).get('tab');
if (['overview','script','bible','shots','prompts','outputs','progress'].includes(initialTab)) state.tab = initialTab;
const icons = {
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  moon:'<path d="M20 14.3A8.5 8.5 0 0 1 9.7 4 8.5 8.5 0 1 0 20 14.3Z"/>',
  plus:'<path d="M12 4v16M4 12h16"/>', grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  list:'<path d="M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1"/>',
  edit:'<path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 15Z"/>',
  check:'<path d="m5 12 4 4L20 5"/>', alert:'<path d="m12 3 10 18H2Z"/><path d="M12 9v5M12 17v1"/>',
  pause:'<path d="M8 5v14M16 5v14"/>', play:'<path d="m8 5 12 7-12 7Z"/>',
  mic:'<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M6 11v1a6 6 0 0 0 12 0v-1M12 18v3M9 21h6"/>',
  arrow:'<path d="M4 12h15m-5-5 5 5-5 5"/>', close:'<path d="m6 6 12 12M18 6 6 18"/>',
  left:'<path d="m14 5-7 7 7 7"/>', right:'<path d="m9 5 7 7-7 7"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="2"/><path d="m3 18 5-5 4 4 4-6 5 7"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v12h5"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
  film:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4"/>',
  folder:'<path d="M3 7V5h7l2 3h9v12H3Z"/>', save:'<path d="M4 3h13l4 4v14H3V3Z"/><path d="M7 3v6h9V3M7 21v-8h10v8"/>'
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.film}</svg>`;
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const seconds = ms => Number(ms / 1000).toFixed(ms % 1000 ? 1 : 0);
const clock = ms => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2,'0')}`;
const number = n => Number(n || 0).toLocaleString('zh-CN');
const reviewNames = {preparing:'素材准备中', pending:'待确认', approved:'已确认', changes_requested:'需要修改', outdated:'需重新确认', completed:'已生成'};
const jobNames = {submitting:'正在提交',submission_unknown:'提交结果待核实',submitted:'已提交',waiting_remote:'生成中',succeeded:'已完成',remote_failed:'生成失败',rejected:'提交未成功',confirmed_not_submitted:'确认未提交'};
const palette = ['#284f77','#4f76a2','#7c99b8','#15334f','#37658a','#6687a9'];
const sceneName = id => state.data?.scene_names?.[id] || id;
function speakerName(id) {const c=state.data.bible.characters||{};return (Array.isArray(c)?c.find(x=>x.speaker_id===id||x.id===id):c[id])?.name||id;}
const shotState = s => s.completed ? 'completed' : (!s.reviewable && s.material_review.state === 'pending' ? 'preparing' : s.material_review.state);
const badge = (kind, text) => `<span class="badge ${kind}">${kind === 'approved' ? icon('check') : ''}${esc(text || reviewNames[kind] || kind)}</span>`;
const uniqueScenes = () => [...new Set(state.data.shots.map(s=>s.scene))];
const sceneColor = id => palette[Math.max(0, uniqueScenes().indexOf(id)) % palette.length];

async function api(path, options = {}, renew = true) {
  const response = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', 'X-Workbench-Token':state.token, ...options.headers}});
  if (!response.ok) {
    let message = '无法读取本地项目，请稍后重试。';
    try {const body = await response.json(); message = typeof body.detail === 'string' ? body.detail : '请检查填写内容。';} catch {}
    if (renew && options.method && response.status === 403 && message === '请从本机工作台页面提交操作。') {
      state.token = (await api('/api/session')).write_token;
      return api(path, options, false);
    }
    throw new Error(message);
  }
  return response.json();
}
let toastTimer;
function toast(text) {const box=$('#toast');box.textContent=text;box.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.remove('visible'),4000);}
function renderIcons() {document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));}
async function loadProjects() {
  state.projects=await api('/api/projects');
  $('#project-select').innerHTML=state.projects.map(p=>`<option value="${esc(p.id)}">《${esc(p.title)}》 · ${esc(p.id.slice(-2))}</option>`).join('');
  if(state.data) $('#project-select').value=state.data.id;
}
async function selectProject(id) {
  state.loadingId=id;
  closeModal();state.scene='all';state.filter='all';state.search='';$('#search').value='';state.signature='';
  const data=await api('/api/projects/'+encodeURIComponent(id)).catch(error=>{if(state.loadingId===id)state.loadingId=null;throw error;});
  if(state.loadingId!==id)return;
  state.data=data;$('#project-select').value=id;state.signature=JSON.stringify({...data,updated_at:0});
  state.loadingId=null;
  const url=new URL(location.href);url.searchParams.set('project',id);history.replaceState(null,'',url);
  render();connection(true);
}
function connection(ok) {$('#connection-state').textContent=ok ? `已连接 · ${new Date().toLocaleTimeString('zh-CN',{hour12:false})} 更新` : '连接中断 · 正在重试';$('.status-dot').style.background=ok?'var(--green)':'var(--red)';}
async function refresh() {
  if(!state.data || state.busy || state.loadingId) return;
  try {
    const id=state.data.id;const data=await api('/api/projects/'+encodeURIComponent(id));
    if(state.loadingId||state.data.id!==id)return;
    const signature=JSON.stringify({...data,updated_at:0});
    state.data=data;
    if(signature!==state.signature){state.signature=signature;if(!state.modal)render();}
    connection(true);
  } catch {connection(false);}
}
function render() {
  const d=state.data;if(!d)return;
  renderProductionControls();
  $('#version-label').textContent=d.source_run?'重制版':'制作项目';
  const scenes=uniqueScenes().length;
  const tabs=[['overview','概况',''],['script','剧本',`${scenes} 场`],['bible','设定','人物 / 场景'],['shots','分镜',`${d.stats.visual_shot_count} 镜`],['prompts','提示词',`${d.stats.segment_count} 段`],['outputs','成片',d.final_video?'已完成':'待生成'],['progress','流程',d.paused?'已暂停':`${d.stats.completed_count}/${d.stats.segment_count}`]];
  $('#tabs').innerHTML=tabs.map(([id,label,count])=>`<button class="tab ${state.tab===id?'active':''}" data-tab="${id}" aria-current="${state.tab===id?'page':'false'}">${id==='overview'?'':`<i class="dot ${['outputs','progress'].includes(id)&&!d.final_video?'waiting':''}"></i>`}${label}<span class="tab-count">${count}</span></button>`).join('');
  $('#main').innerHTML=({shots:renderShots,overview:renderOverview,progress:renderProgress,script:renderScript,bible:renderBible,prompts:renderPrompts,outputs:renderOutputs}[state.tab] || renderShots)();
  if(state.tab==='bible')$('#main').insertAdjacentHTML('beforeend',referenceGallery());
}
function heading(title,subtitle,actions='') {return `<div class="page-heading"><div><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-actions">${actions}</div></div>`;}
const inputButton=()=>`<button class="button" data-action="input">${icon('edit')}制作输入</button>`;
function renderProductionControls() {
  const project = state.data;
  const control = project.control;
  const bar = $('#production-controls');
  bar.hidden = !control;
  if (!control) return;
  $('#production-status').textContent = control.message;
  const startButton = $('#start-generation');
  const labels = {prepare:project.paused ? '继续准备素材' : '开始准备素材', review:'检查分镜素材', budget:'调整积分上限', wait:'等待规划与素材'};
  startButton.lastElementChild.textContent = labels[control.action] ||
    (control.running ? '队列运行中' : project.paused || control.phase === 'interrupted' ? '继续生成' : '开始生成');
  startButton.disabled = state.busy || !control.can_start;
  $('#pause-generation').disabled = state.busy || !control.can_pause;
}

function openBudget(project) {
  const estimate = project.stats.estimated_credits;
  openModal(`<section class="modal small" role="dialog" aria-modal="true" aria-labelledby="start-title">
    <header class="modal-head"><h2 id="start-title">授权并开始生成</h2><button class="icon-button" data-action="close" aria-label="关闭">${icon('close')}</button></header>
    <form id="start-budget-form" class="form-content"><div class="form-field">
      <label for="start-budget">《${esc(project.title)}》本版积分上限</label>
      <input id="start-budget" type="number" min="1" max="10000000" step="1" required>
      <small>${estimate == null ? '' : '项目参考报价 ' + number(estimate) + ' 积分。'}此额度包含参考图、首帧、视频和返工。</small>
    </div></form><footer class="modal-foot"><button class="button" data-action="close">取消</button>
      <button class="button green" type="submit" form="start-budget-form">授权并开始</button></footer></section>`,
    {type:'budget', projectId:project.id});
}

async function sendProductionCommand(id, action, payload={}) {
  if (state.busy || id !== state.data?.id) return;
  state.busy = true;
  renderProductionControls();
  try {
    const control = await api('/api/projects/'+encodeURIComponent(id)+'/'+action, {
      method:'POST', body:JSON.stringify(payload)
    });
    if (id === state.data?.id) {
      state.data.control = control;
      if (state.modal?.type === 'budget') closeModal();
      toast(control.message);
    }
  } catch (error) {
    toast(error.message);
  } finally {
    state.busy = false;
    await refresh();
    renderProductionControls();
  }
}

$('#start-generation').addEventListener('click', () => {
  const project = state.data;
  if (project.control.action === 'review') {
    state.scene='all';state.filter='all';state.search='';$('#search').value='';
    $('#tabs [data-tab="shots"]').click();
    const shot = project.shots.find(s => s.reviewable && s.material_review.state !== 'approved');
    if (shot) openShot(shot.id);
    return;
  }
  if (project.production.needs_budget || project.production.budget_credits == null) openBudget(project);
  else sendProductionCommand(project.id,'start');
});
$('#pause-generation').addEventListener('click', () => sendProductionCommand(state.data.id,'pause'));
document.addEventListener('submit', event => {
  if (event.target.id !== 'start-budget-form') return;
  event.preventDefault();
  sendProductionCommand(state.modal.projectId,'start',{authorized_budget_credits:Number($('#start-budget').value)});
});
function pauseNotice() {return state.data.paused?`<div class="notice">${icon('pause')}<span><strong>本次生成已暂存。</strong> 你可以继续检查素材，确认不会解除暂停或提交视频。</span>${badge('pending','等待你继续')}</div>`:'';}
function metric(label,value,unit,note,status,warning=false) {return `<div class="metric"><div class="metric-head"><span>${label}</span><span class="${warning?'warn':''}">${icon(warning?'clock':'check')}${status}</span></div><div class="metric-value">${value}<small>${unit}</small></div><div class="metric-note">${note}</div></div>`;}
function metrics() {const s=state.data.stats;return `<div class="metrics">${metric('计划总时长',clock(s.planned_duration_ms),'',`成片上限 ${clock(state.data.production.max_duration_seconds*1000)}`,'已规划')}${metric('平均镜长',s.visual_shot_count?(s.planned_duration_ms/1000/s.visual_shot_count).toFixed(2):'—','秒',`${s.segment_count} 个生成片段 · ${s.visual_shot_count} 个观看镜头`,'含片内切镜')}${metric('已有首帧',s.reused_image_count,'张','直接预览本地真实素材','可查看')}${metric('素材确认',`${s.approved_count}<span style="font-size:16px;color:var(--muted)"> / ${s.segment_count}</span>`,'段',s.completed_count===s.segment_count&&s.segment_count?'历史已生成片段无需重新确认':`${s.reviewable_count} 段可确认 · ${s.completed_count} 段已生成`,s.reviewable_count?'待检查':s.pending_count?'准备中':'已就绪',!!s.pending_count)}</div>`;}
function timeline() {
  const d=state.data;let elapsed=0;const seen=new Set();let labels='';
  const bars=d.shots.map(s=>{const left=elapsed/d.stats.planned_duration_ms*100;elapsed+=s.duration_ms;
    if(!seen.has(s.scene)){labels+=`<span style="left:${left}%">${esc(s.scene)} ${esc(sceneName(s.scene))}</span>`;seen.add(s.scene);}
    return `<button data-open="${s.id}" style="flex:${s.duration_ms};--scene-color:${sceneColor(s.scene)}" aria-label="检查${s.id}素材" title="${esc(s.id+' '+s.title)} · ${seconds(s.duration_ms)}秒">${s.material_review.state==='approved'?'<i class="approved-mark"></i>':''}${s.internal_shots.slice(1).map(c=>`<i class="cut-marker" style="left:${c.start_ms/s.duration_ms*100}%"></i>`).join('')}${s.id.replace(/^S0*/,'')}</button>`;
  }).join('');
  return `<section class="panel timeline-panel" aria-label="分镜节奏条"><div class="panel-label"><strong>节奏条</strong><span class="muted">宽度 = 时长，颜色 = 场次；点一格检查素材</span><span class="muted" style="margin-left:auto">计划 ${clock(d.stats.planned_duration_ms)}</span></div><div class="scene-labels">${labels}</div><div class="rhythm">${bars}</div><div class="ticks">${Array.from({length:6},(_,i)=>`<span>${Math.round(d.stats.planned_duration_ms/1000*i/5)}s</span>`).join('')}</div><div class="legend">${uniqueScenes().map(s=>`<span><i class="swatch" style="--scene-color:${sceneColor(s)}"></i>${esc(sceneName(s))}</span>`).join('')}<span><i class="line-swatch"></i>顶部短线 = 已确认</span><span>虚线 = 片段内部切镜</span></div></section>`;
}
function filteredShots() {return state.data.shots.filter(s=>(state.scene==='all'||s.scene===state.scene)&&(state.filter==='all'||shotState(s)===state.filter)&&(!state.search||[s.id,s.title,s.start_state,s.end_state,...s.dialogue.map(x=>x.text)].join(' ').toLowerCase().includes(state.search.toLowerCase())));}
function card(s) {const kind=shotState(s);return `<article class="shot-card" data-shot="${s.id}"><button class="shot-thumb" data-open="${s.id}" aria-label="查看${s.id}首帧">${s.first_image?`<img src="${esc(s.first_image)}" alt="${s.id} 首帧" loading="lazy">`:icon('image')}<span class="thumb-time">${seconds(s.duration_ms)}s</span><span class="thumb-icon">${icon(s.video?'play':'image')}</span></button><div class="shot-copy"><div class="shot-title"><span class="shot-number">${s.id.replace('S','')}</span><button data-open="${s.id}" title="${esc(s.title)}">${esc(s.title)}</button></div><div class="shot-tags">${badge('',''+seconds(s.duration_ms)+' 秒')}${badge('',`${Math.max(1,s.internal_shots.length)} 镜`)}${badge(kind)}</div><div class="beat-row"><b>起</b><span>${esc(s.start_state||'等待起始状态')}</span></div><div class="beat-row"><b>动</b><span>${esc(s.action||'等待动作规划')}</span></div><div class="beat-row"><b>终</b><span>${esc(s.end_state||'等待结束状态')}</span></div><div class="shot-bottom"><span>${icon('mic')} ${s.dialogue.length} 句台词</span><button class="text-button" data-open="${s.id}">检查素材 ${icon('arrow')}</button></div></div></article>`;}
function empty(title,text,button='') {return `<div class="empty-state">${icon('film')}<h2>${title}</h2><p>${text}</p>${button}</div>`;}
const canConfirmInBulk = s => s.reviewable && ['pending','outdated'].includes(s.material_review.state);
function renderShots() {
  const d=state.data;const shots=filteredShots();
  const actions=`<div class="segmented" aria-label="分镜显示方式"><button data-layout="cards" class="${state.layout==='cards'?'active':''}">${icon('grid')}卡片</button><button data-layout="list" class="${state.layout==='list'?'active':''}">${icon('list')}列表</button></div>${inputButton()}`;
  if(!d.shots.length) return heading('分镜与素材','先保存创意或剧本，再由 AI 规划分镜',inputButton())+empty('还没有分镜','制作需求会保存在当前项目中。在对话里让我继续，我会读取并完成剧本、分镜和素材准备。',`<button class="button primary" data-action="input">填写制作需求</button>`);
  const reviewable=shots.filter(canConfirmInBulk);
  return heading('分镜与素材',`${d.stats.visual_shot_count} 镜 · ${seconds(d.stats.planned_duration_ms)} 秒 · ${uniqueScenes().length} 场`,actions)+pauseNotice()+metrics()+timeline()+`<div class="filters"><div class="chips"><button class="chip ${state.scene==='all'?'active':''}" data-scene="all">全部</button>${uniqueScenes().map(s=>`<button class="chip ${state.scene===s?'active':''}" data-scene="${esc(s)}">${esc(s)} ${esc(sceneName(s))}</button>`).join('')}</div><select class="filter-select" id="review-filter" aria-label="筛选确认状态"><option value="all">全部状态</option>${Object.entries(reviewNames).map(([v,l])=>`<option value="${v}" ${state.filter===v?'selected':''}>${l}</option>`).join('')}</select></div><div class="shot-grid ${state.layout==='list'?'list':''}">${shots.map(card).join('')||empty('没有匹配的分镜','换一个关键词或筛选条件试试。')}</div><div class="review-summary"><span>查看首帧、对白、时长与提示词后确认；素材变更会自动要求重新确认。</span><button class="button green" data-action="bulk" ${reviewable.length?'':'disabled'}>${icon('check')}确认当前筛选的素材${reviewable.length?`（${reviewable.length}）`:''}</button></div>`;
}
function stages() {return `<div class="panel stage-track">${state.data.stages.map((s,i)=>`<div class="stage ${s.state}"><div class="stage-icon">${s.state==='done'?icon('check'):i+1}</div>${s.label}</div>`).join('')}</div>`;}
function issues() {return `<div class="issue-list">${state.data.issues.map(i=>`<div class="issue">${icon(i.kind==='pause'?'pause':'alert')}<div><h3>${esc(i.title)}</h3><p>${esc(i.detail)}</p></div></div>`).join('')||'<div class="issue">'+icon('check')+'<div><h3>目前没有待处理提示</h3><p>流程会随着本地项目记录更新。</p></div></div>'}</div>`;}
function renderOverview() {return heading(`《${esc(state.data.title)}》`,'从创作到成片，每个阶段都有据可查',inputButton())+metrics()+`<div class="stack">${stages()}<div class="two-column"><div class="panel"><h2>当前待办</h2>${issues()}</div><div class="panel"><h2>本次制作</h2>${productionFacts()}<button class="button" data-tab="shots">检查分镜素材 ${icon('arrow')}</button></div></div></div>`;}
function productionFacts() {const p=state.data.production;return [['画幅',p.aspect_ratio],['画面风格',p.visual_style],['视频模型',p.model],['声音','视频模型原生声音'],['镜头调度',p.scheduling==='serial'?'逐镜串行':'按依赖并行'],['提示词',{direct:'当前 AI 直写',local:'本地 Prompt Rewriter',online:'服务端在线优化'}[p.prompt_mode]],['已授权积分上限',p.budget_credits==null?'待确认':number(p.budget_credits)]].map(([k,v])=>`<div class="key-value"><span>${k}</span><strong>${esc(v)}</strong></div>`).join('');}
function scriptHTML(text) {return text.split(/\n\s*\n/).map(block=>{if(block.startsWith('# '))return `<h1>${esc(block.slice(2))}</h1>`;if(block.startsWith('## '))return `<h2>${esc(block.slice(3))}</h2>`;let html=esc(block).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');return `<p class="${block.startsWith('**')?'dialogue-line':''}">${html}</p>`;}).join('');}
function renderScript() {const d=state.data;return heading('剧本','先读完整冲突，再看镜头如何承接',`<button class="button" data-action="input">${icon('edit')}编辑制作输入</button>`)+`<div class="two-column"><article class="panel script-paper">${d.script?scriptHTML(d.script):empty('还没有剧本','可以在制作输入中粘贴剧本或填写创意。')}</article><aside class="stack"><section class="panel"><h2>故事信息</h2><div class="key-value"><span>对白</span><strong>${d.shots.reduce((n,s)=>n+s.dialogue.length,0)} 句</strong></div><div class="key-value"><span>场次</span><strong>${uniqueScenes().length} 场</strong></div><div class="key-value"><span>计划时长</span><strong>${clock(d.stats.planned_duration_ms)}</strong></div></section><section class="panel"><h2>修改会如何生效</h2><p class="muted">新的输入会单独保存为制作需求。让我继续处理后，再更新剧本、分镜和提示词，避免覆盖已经确认的素材。</p><button class="button" data-action="input">填写或修改需求</button></section></aside></div>`;}
function renderBible() {const b=state.data.bible;const characters=Array.isArray(b.characters)?b.characters:Object.values(b.characters||{});return heading('人物与场景','统一身份、声音设定与空间关系',inputButton())+`<div class="setting-grid">${characters.map(c=>`<article class="panel setting-card"><div class="avatar">${esc((c.name||c.id||'人').slice(0,1))}</div><h2>${esc(c.name||c.id)}</h2><span class="label">外观</span><p>${esc(c.appearance||c.description||'尚未填写')}</p><span class="label">声音</span><p>${esc(c.voice_description||'由视频模型原生生成')}</p>${c.dramatic_profile?`<span class="label">人物目标与关系</span><p>${Object.values(c.dramatic_profile).map(esc).join('<br>')}</p>`:''}</article>`).join('')}${(b.scenes||[]).map(s=>`<article class="panel setting-card full"><span class="label">${esc(s.id)}</span><h2>场景与连续性</h2><p>${esc(s.description)}</p>${(s.anchors||[]).map(a=>`<p>· ${esc(a)}</p>`).join('')}</article>`).join('')}${!characters.length?empty('人物设定尚未准备','保存制作需求后，由 AI 完成人物和场景设定。'):''}</div>`;}
function renderPrompts() {const shots=filteredShots();return heading('视频提示词',{direct:'显示当前 AI 写好并提交的正文',local:'显示本地改写后实际提交的正文',online:'显示送交服务端的原文，服务端会在生成时进行优化'}[state.data.production.prompt_mode],inputButton())+`<div class="prompt-list">${shots.map(s=>`<details class="panel prompt-item"><summary><span class="shot-number">${s.id}</span><strong>${esc(s.title)}</strong><span class="muted">${seconds(s.duration_ms)} 秒 ${badge(shotState(s))}</span></summary><pre>${esc(s.prompt||'尚未准备最终提示词')}</pre><div class="prompt-actions"><button class="button" data-copy="${s.id}">${icon('copy')}复制提示词</button><button class="button" data-open="${s.id}">连同素材一起检查 ${icon('arrow')}</button></div></details>`).join('')||empty('没有可显示的提示词','分镜和实际首帧准备好后，这里会显示最终正文。')}</div>`;}
function renderProgress() {const d=state.data;return heading('流程监视','每 5 秒更新任务进度和积分预留',`<button class="button" data-action="refresh">${icon('refresh')}立即刷新</button>`)+`<div class="stack">${stages()}${issues()}<section class="panel"><div class="panel-label"><strong>任务记录</strong><span class="muted">${d.stats.active_count} 个进行中 · 已预留 ${number(d.stats.reserved_credits)} 积分</span></div>${d.jobs.length?`<div class="table-wrap"><table><thead><tr><th>任务</th><th>类型</th><th>片段</th><th>状态</th><th>积分</th><th>素材</th></tr></thead><tbody>${d.jobs.map(j=>`<tr><td class="mono">${esc(j.id)}</td><td>${j.local_edit?'本地剪辑':j.role==='video'?'视频':'图片'}</td><td>${esc(j.shot_id||'—')}</td><td>${badge(j.status==='succeeded'?'approved':'pending',jobNames[j.status]||j.status)}</td><td>${number(j.credits)}</td><td>${j.role==='video'&&j.media?`<button class="text-button" data-open="${esc(j.shot_id)}">查看</button>`:'—'}</td></tr>`).join('')}</tbody></table></div>`:empty('尚未提交视频任务','确认已有素材后，可在页面顶部开始生成。')}</section><section class="panel"><h2>工作台操作记录</h2><ul class="events">${d.events.slice(-10).reverse().map(v=>`<li><time>${new Date(v.at*1000).toLocaleString('zh-CN',{hour12:false})}</time>${esc(v.text)}</li>`).join('')||'<li class="muted">还没有素材确认或输入修改记录。</li>'}</ul></section></div>`;}
function renderOutputs() {const d=state.data;return heading('成片','保留完整原生音轨，生成完成后在这里播放',d.source_run?`<button class="button" data-project="${esc(d.source_run)}">查看上一版 ${icon('arrow')}</button>`:'')+(d.final_video?`<div class="output-layout"><section class="panel"><video class="output-video" src="${esc(d.final_video)}" controls preload="metadata" playsinline></video></section><aside class="panel"><h2>交付信息</h2>${productionFacts()}<div class="key-value"><span>已完成片段</span><strong>${d.stats.completed_count}</strong></div><a class="button" href="${esc(d.final_video)}" download>保存成片</a></aside></div>`:empty('新版成片尚未生成',d.paused?'本次制作仍处于暂停状态。分镜、首帧和最终提示词已经保存，可先检查素材。':'准备完成并继续制作后，成片会在这里出现。',`<button class="button primary" data-tab="shots">先检查素材 ${icon('arrow')}</button>`));}

function referenceGallery() {
  const refs=state.data.references||[];
  return refs.length?`<section class="reference-section"><h2>已有参考素材 <small class="muted">${refs.length} 张</small></h2><p class="muted">点击查看原图；提交前在分镜中确认实际选用的素材。</p><div class="reference-grid">${refs.map((r,i)=>`<button class="panel reference-card" data-reference="${i}" aria-label="查看参考图${esc(r.id)}"><img src="${esc(r.image)}" alt="${esc(r.id)} 参考素材" loading="lazy"><strong>${esc(r.id)}</strong><span class="muted">${r.reused?'复用已有素材':'项目参考素材'}</span></button>`).join('')}</div></section>`:'';
}
function openReference(index) {
  const ref=state.data.references?.[index];if(!ref)return;
  openModal(`<section class="modal small" role="dialog" aria-modal="true" aria-labelledby="reference-title"><header class="modal-head"><h2 id="reference-title">参考素材 ${esc(ref.id)}</h2><button class="icon-button" data-action="close" aria-label="关闭参考素材">${icon('close')}</button></header><div class="reference-preview"><img src="${esc(ref.image)}" alt="${esc(ref.id)} 原图"></div></section>`,{type:'reference'});
}
function openModal(html, modalState) {state.modal={...modalState,previousFocus:document.activeElement};$('#modal-root').innerHTML=`<div class="overlay">${html}</div>`;document.body.style.overflow='hidden';requestAnimationFrame(()=>$('.modal button,.modal input,.modal textarea')?.focus());}
function closeModal() {const focus=state.modal?.previousFocus;if(state.modal)state.signature='';state.modal=null;$('#modal-root').innerHTML='';document.body.style.overflow='';focus?.focus?.();}
function openShot(id,detailTab='materials') {const s=state.data.shots.find(s=>s.id===id);if(!s)return;const i=state.data.shots.indexOf(s);const material=detailTab==='materials';
  const dialogue=s.dialogue.map(d=>`<div class="dialogue-bubble"><div class="dialogue-meta"><strong>${esc(speakerName(d.speaker_id||d.speaker||d.character_id))}</strong><span>${d.speech_start_ms!=null?(d.speech_start_ms/1000).toFixed(2)+' – '+(d.speech_end_ms/1000).toFixed(2)+' 秒':''}</span></div><p>${esc(d.text)}</p>${d.delivery?`<small class="muted">${esc(d.delivery)}</small>`:''}</div>`).join('');
  const content=material?`<div class="detail-block"><h3>本段作用</h3><p>${esc(s.purpose||s.title)}</p></div><div class="detail-block"><h3>起始状态 → 结束状态</h3><p>${esc(s.start_state)}</p><p style="margin-top:7px">${icon('arrow')} ${esc(s.end_state)}</p></div><div class="detail-block"><h3>对白 · ${s.dialogue.length} 句</h3>${dialogue||'<p class="muted">本段没有对白。</p>'}</div><div class="detail-block"><h3>时长安排</h3><p>${esc(s.timing_reason||'按已保存分镜时长执行。')}</p></div>${s.internal_shots.length>1?`<div class="detail-block"><h3>内部切镜</h3>${s.internal_shots.slice(1).map(c=>`<p>${(c.start_ms/1000).toFixed(2)}秒 · ${esc(c.cut_reason)}</p>`).join('')}</div>`:''}`:`<div class="detail-block"><button class="button" data-copy="${s.id}">${icon('copy')}复制完整提示词</button></div><pre class="prompt-code">${esc(s.prompt||'最终提示词尚未准备')}</pre>`;
  openModal(`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="shot-modal-title"><header class="modal-head"><div><h2 id="shot-modal-title"><span class="shot-number">${s.id}</span> 检查分镜素材</h2><div class="subtitle">${seconds(s.duration_ms)} 秒 · ${esc(sceneName(s.scene))} · ${s.dialogue.length} 句对白</div></div><div class="modal-actions"><button class="icon-button" data-prev="${i-1}" aria-label="上一片段" ${i?'':'disabled'}>${icon('left')}</button><button class="icon-button" data-prev="${i+1}" aria-label="下一片段" ${i<state.data.shots.length-1?'':'disabled'}>${icon('right')}</button><button class="icon-button" data-action="close" aria-label="关闭素材检查">${icon('close')}</button></div></header><div class="modal-body"><div class="preview-side">${s.video?`<video src="${esc(s.video)}" ${s.first_image?`poster="${esc(s.first_image)}"`:''} controls preload="metadata" playsinline></video>`:s.first_image?`<img src="${esc(s.first_image)}" alt="${s.id} 实际提交首帧">`:'<p class="muted">首帧尚未准备</p>'}<span class="caption">${s.video?'已生成候选 · 完整原生音画':'当前片段的实际首帧'}</span>${s.last_image?`<details><summary>查看目标尾帧</summary><img src="${esc(s.last_image)}" alt="目标尾帧"></details>`:''}${badge(shotState(s))}</div><div class="details-side">${state.data.production.prompt_mode==='online'?'<p class="hint">本片启用了在线优化。此处确认送交服务端的原文。</p>':''}<div class="detail-tabs"><button class="${material?'active':''}" data-detail-tab="materials">素材与对白</button><button class="${!material?'active':''}" data-detail-tab="prompt">完整视频提示词</button></div>${content}<label class="note-label" for="review-note">修改意见 / 确认备注</label><textarea id="review-note" placeholder="例如：首帧中的人物位置需要调整；这句对白应更坚定。" ${s.reviewable?'':'disabled'}>${esc(s.material_review.note||'')}</textarea><div class="error-text" id="modal-error" role="alert">${esc(s.preparation_error||'')}</div></div></div><footer class="modal-foot"><span class="hint">${state.data.paused?'当前项目已暂停，确认仅保存记录。':'确认仅适用于当前素材、提示词与时长。'}</span><div class="buttons"><button class="button danger" data-decision="changes_requested" ${s.reviewable?'':'disabled'}>需要修改</button><button class="button green" data-decision="approved" ${s.reviewable?'':'disabled'}>${icon('check')}${s.material_review.state==='approved'?'重新确认当前版本':'确认这段素材'}</button></div></footer></section>`,{type:'shot',shot:JSON.parse(JSON.stringify(s)),detailTab});
}
function openBulk() {const selected=filteredShots().filter(canConfirmInBulk);if(!selected.length)return;openModal(`<section class="modal small" role="dialog" aria-modal="true" aria-labelledby="bulk-title"><header class="modal-head"><h2 id="bulk-title">确认 ${selected.length} 段当前素材</h2><button class="icon-button" data-action="close" aria-label="关闭">${icon('close')}</button></header><div class="dialog-content"><p>请先查看这些片段的首帧、对白、时长和最终提示词。确认后，仅这一版本的素材可以用于下一次生成。</p><p><strong>${selected.map(s=>s.id).join('、')}</strong></p><p>这不会解除暂停、授权新预算或自动提交视频。</p><div class="error-text" id="modal-error" role="alert"></div></div><footer class="modal-foot"><div class="buttons"><button class="button" data-action="close">返回检查</button><button class="button green" data-action="confirm-bulk">${icon('check')}确认这些素材</button></div></footer></section>`,{type:'bulk',shots:JSON.parse(JSON.stringify(selected))});}
async function decide(items) {if(state.busy)return;state.busy=true;try {await api(`/api/projects/${state.data.id}/reviews`,{method:'POST',body:JSON.stringify({items})});closeModal();state.busy=false;await refresh();render();toast(items.some(x=>x.decision==='changes_requested')?'修改意见已保存，等待处理。':'素材确认已保存。'+(state.data.paused?'项目仍保持暂停。':''));}catch(e){$('#modal-error').textContent=e.message;}finally{state.busy=false;}}
function openInput(isNew=false) {const d=state.data;const b=isNew?{}:(d.brief||{});const p=isNew?{max_duration_seconds:300,aspect_ratio:'9:16',visual_style:'写实'}:d.production;openModal(`<section class="modal" style="width:min(820px,100%)" role="dialog" aria-modal="true" aria-labelledby="input-title"><header class="modal-head"><div><h2 id="input-title">${isNew?'新建制作项目':'基本制作输入'}</h2><div class="subtitle">保存创意、剧本与制作要求，供下一次 AI 规划读取</div></div><button class="icon-button" data-action="close" aria-label="关闭制作输入">${icon('close')}</button></header><form id="brief-form" class="form-content"><div class="form-grid"><div class="form-field full"><label for="brief-title">片名</label><input type="text" id="brief-title" name="title" required maxlength="100" value="${esc(b.title||(isNew?'':d.title))}" placeholder="为这个故事起个名字"></div><div class="form-field full"><label for="brief-idea">创意与制作要求</label><textarea id="brief-idea" name="idea" maxlength="10000" placeholder="描述故事、冲突、人物情绪，或这一版希望修改的地方…">${esc(b.idea||'')}</textarea></div><div class="form-field full"><label for="brief-script">已有剧本（可选）</label><textarea class="script-input" id="brief-script" name="script" maxlength="100000" placeholder="直接粘贴剧本；没有剧本也可以只填写创意。">${esc(b.script??(isNew?'':d.script))}</textarea></div><div class="form-field"><label for="brief-duration">成片时长上限（秒）</label><input type="number" id="brief-duration" name="max_duration_seconds" min="5" max="3600" required value="${b.max_duration_seconds||p.max_duration_seconds}"><small>这是上限，无需为了填满时长拉长镜头。</small></div><div class="form-field"><label for="brief-ratio">画幅</label><select id="brief-ratio" name="aspect_ratio">${[['9:16','9:16 · 竖屏'],['16:9','16:9 · 横屏'],['1:1','1:1 · 方形']].map(([v,l])=>`<option value="${v}" ${(b.aspect_ratio||p.aspect_ratio)===v?'selected':''}>${l}</option>`).join('')}</select></div><div class="form-field"><label for="brief-style">画面风格</label><input type="text" id="brief-style" name="visual_style" maxlength="200" value="${esc(b.visual_style||p.visual_style)}"></div><div class="form-field"><label for="brief-budget">预算意向（积分，可选）</label><input type="number" id="brief-budget" name="budget_credits" min="0" max="10000000" value="${b.budget_credits??''}" placeholder="例如 6000"><small>仅保存计划额度，生成前另行确认实际用量。</small></div></div><div class="error-text" id="modal-error" role="alert"></div></form><footer class="modal-foot"><span class="hint">保存后，在对话中让我继续处理；不会自动开始生成。</span><div class="buttons"><button class="button" data-action="close">取消</button><button class="button primary" type="submit" form="brief-form">${icon('save')}${isNew?'创建并保存需求':'保存制作需求'}</button></div></footer></section>`,{type:'input',isNew});setTimeout(()=>$('#brief-title')?.focus(),30);}
async function saveInput(form) {if(state.busy)return;state.busy=true;const values=Object.fromEntries(new FormData(form));values.max_duration_seconds=Number(values.max_duration_seconds);values.budget_credits=values.budget_credits===''?null:Number(values.budget_credits);const isNew=state.modal.isNew;try{const result=await api(isNew?'/api/projects':`/api/projects/${state.data.id}/brief`,{method:isNew?'POST':'PUT',body:JSON.stringify(values)});closeModal();state.busy=false;await loadProjects();if(isNew){state.tab='overview';await selectProject(result.id);}else{await refresh();render();}toast('制作需求已保存。让我继续时会读取这里的内容。');}catch(e){$('#modal-error').textContent=e.message;}finally{state.busy=false;}}
async function copyPrompt(id) {const s=state.data.shots.find(s=>s.id===id);try{await navigator.clipboard.writeText(s.prompt);toast('提示词已复制');}catch{toast('浏览器未允许复制，请展开提示词手动选择。');}}

document.addEventListener('click',async event=>{const el=event.target.closest('button,a[data-project]');if(event.target.classList.contains('overlay')){closeModal();return;}if(!el||el.disabled)return;
  if(el.dataset.tab){
    state.tab=el.dataset.tab;
    const url=new URL(location.href);
    url.searchParams.set('tab',state.tab);
    history.replaceState(null,'',url);
    render();return;
  }
  if(el.dataset.layout){state.layout=el.dataset.layout;render();return;}
  if(el.dataset.scene){state.scene=el.dataset.scene;render();return;}
  if(el.dataset.open){openShot(el.dataset.open);return;}
  if(el.dataset.reference!==undefined){openReference(Number(el.dataset.reference));return;}
  if(el.dataset.copy){await copyPrompt(el.dataset.copy);return;}
  if(el.dataset.project){state.tab='outputs';await selectProject(el.dataset.project);return;}
  if(el.dataset.prev!==undefined){const s=state.data.shots[Number(el.dataset.prev)];if(s)openShot(s.id);return;}
  if(el.dataset.detailTab){const note=$('#review-note')?.value;const id=state.modal.shot.id;openShot(id,el.dataset.detailTab);if(note!==undefined)$('#review-note').value=note;return;}
  if(el.dataset.decision){const s=state.modal.shot;await decide([{shot_id:s.id,fingerprint:s.material_review.fingerprint,decision:el.dataset.decision,note:$('#review-note').value}]);return;}
  const action=el.dataset.action;
  if(action==='close')closeModal();
  if(action==='input')openInput();
  if(action==='bulk')openBulk();
  if(action==='confirm-bulk')await decide(state.modal.shots.map(s=>({shot_id:s.id,fingerprint:s.material_review.fingerprint,decision:'approved',note:''})));
  if(action==='refresh'){await refresh();toast('已刷新本地任务记录');}
});
document.addEventListener('submit',event=>{if(event.target.id==='brief-form'){event.preventDefault();saveInput(event.target);}});
document.addEventListener('change',event=>{if(event.target.id==='review-filter'){state.filter=event.target.value;render();}});
$('#project-select').addEventListener('change',event=>selectProject(event.target.value).catch(e=>toast(e.message)));
$('#search').addEventListener('input',event=>{state.search=event.target.value;if(!['shots','prompts'].includes(state.tab))state.tab='shots';render();});
$('#theme-toggle').addEventListener('click',()=>{document.body.classList.toggle('dark');try{localStorage.setItem('story-workbench-theme',document.body.classList.contains('dark')?'dark':'light');}catch{}});
$('#workflow-settings').addEventListener('click',()=>openWorkflowSettings());
$('#new-project').addEventListener('click',newProjectWithSettings);
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();$('#search').focus();}if(event.key==='Escape')closeModal();if(event.key==='Tab'&&state.modal){const focusable=[...$('.modal').querySelectorAll('button:not(:disabled),input,textarea:not(:disabled),select,a[href]')];const first=focusable[0],last=focusable[focusable.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}});
async function boot(){renderIcons();try{if(localStorage.getItem('story-workbench-theme')==='dark')document.body.classList.add('dark');}catch{}try{const session=await api('/api/session');state.token=session.write_token;await loadProjects();const wanted=new URL(location.href).searchParams.get('project');const id=state.projects.some(p=>p.id===wanted)?wanted:state.projects[0]?.id;if(id)await selectProject(id);else{$('#main').innerHTML=empty('创建第一个剧情视频项目','填写创意或已有剧本，从这里开始。',`<button class="button primary" id="first-project">新建项目</button>`);$('#first-project').onclick=newProjectWithSettings;}
    const settings=await api('/api/workflow-settings');
    if(!settings.configured||new URL(location.href).searchParams.get('setup')==='1')await openWorkflowSettings(false,settings);
  }catch(e){$('#main').innerHTML=empty('暂时无法连接工作台',esc(e.message));}setInterval(refresh,5000);}
boot();
