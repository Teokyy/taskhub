const tg=window.Telegram?.WebApp;
tg?.ready();tg?.expand();
const initData=tg?.initData||'';
const headers={'Content-Type':'application/json','X-Telegram-Init-Data':initData};
const view=document.querySelector('#view');
let me=null,tab='home',toastTimer;
const rub=n=>(n/100).toLocaleString('ru-RU',{maximumFractionDigits:2})+' ₽';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const isAdmin=()=>me?.user?.role==='admin';
function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,2600)}
async function api(path,opts={}){const r=await fetch('/api'+path,{...opts,headers:{...headers,...opts.headers}});let data={};try{data=await r.json()}catch{}if(!r.ok)throw Error(data.detail||('Ошибка '+r.status));return data}
function nav(){document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab))}
function setTab(next){tab=next;nav();render()}
function greeting(){const h=new Date().getHours();return h<6?'Доброй ночи':h<12?'Доброе утро':h<18?'Добрый день':'Добрый вечер'}
async function boot(){
 try{me=await api('/me');document.querySelector('#avatar').textContent=(me.user.first_name||'T').slice(0,1).toUpperCase();document.querySelector('#role-label').textContent=isAdmin()?'ADMIN SPACE':'TEAM SPACE';await render()}
 catch(e){const web=window.Telegram?.WebApp,data=web?.initData||'';const reason=!web?'Telegram WebApp не обнаружен.':!data?'Telegram не передал initData. Открой Mini App через кнопку бота.':'Сервер не принял Telegram initData. Проверь BOT_TOKEN в Railway.';view.innerHTML='<div class="panel"><div class="eyebrow">SIGN IN REQUIRED</div><h2>Не получилось войти</h2><p class="muted">'+esc(reason)+'</p><p class="muted">'+esc(e.message)+' · initData: '+data.length+' символов</p><button class="btn secondary" onclick="location.reload()">Повторить</button></div>'}
}
function empty(title,desc,action=''){return '<div class="empty"><b>'+esc(title)+'</b>'+esc(desc)+(action||'')+'</div>'}
function taskCard(t,adminView=false){
 let action='';
 if(t.status==='available'&&!isAdmin())action='<button class="btn" data-claim="'+t.id+'">Взять задачу</button>';
 if(['in_progress','revision'].includes(t.status)&&Number(t.assignee_id)===Number(me.user.id))action='<button class="btn" data-submit="'+t.id+'">Сдать работу</button>';
 const statuses={available:'Открыта',in_progress:'В работе',submitted:'На проверке',revision:'Нужна доработка',accepted:'Принята',rejected:'Отклонена',cancelled:'Отменена',not_completed:'Не выполнена'};
 return '<article class="panel task"><div class="task-top"><span class="pill">'+esc(t.category||'Другое')+'</span><span class="price">'+rub(t.reward_kop)+'</span></div><h3>'+esc(t.title)+'</h3><p>'+esc(t.description||'Без дополнительного описания')+'</p><div class="task-bottom"><span class="status">'+(statuses[t.status]||esc(t.status))+'</span><span class="deadline">'+(t.deadline?'До '+esc(t.deadline):'Без срока')+'</span></div>'+(adminView&&t.assignee_name?'<div class="divider"></div><span class="muted">Исполнитель: '+esc(t.assignee_name)+'</span>':'')+(action?'<div class="actions">'+action+'</div>':'')+'</article>'
}
function bindTaskActions(){view.querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>claim(b.dataset.claim));view.querySelectorAll('[data-submit]').forEach(b=>b.onclick=()=>submitForm(b.dataset.submit))}
async function render(){
 nav();
 if(tab==='home'){
  const count=Object.values(me.counts||{}).reduce((a,b)=>a+b,0);
  view.innerHTML='<section class="hello"><div class="eyebrow">'+(isAdmin()?'YOUR WORKSPACE':'YOUR TEAM SPACE')+'</div><h1>'+greeting()+', '+esc(me.user.first_name||'друг')+'.</h1><p>Всё важное по задачам — в одном месте.</p></section><section class="hero"><div class="eyebrow">ТВОЙ БАЛАНС</div><div class="amount">'+rub(me.balance_kop)+'</div><div class="hero-foot">Начислено за принятые задачи</div></section><div class="stats"><div class="stat"><small>Мои задачи</small><strong>'+count+'</strong></div><div class="stat"><small>На проверке</small><strong>'+(me.counts?.submitted||0)+'</strong></div></div><div class="section-head"><h2>Быстрый доступ</h2><span>WORKSPACE</span></div><div class="panel"><div class="task-top"><div><b>Каталог задач</b><div class="muted" style="font-size:12px">Найди следующую задачу</div></div><button class="btn secondary" id="open-tasks">Открыть ↗</button></div></div>'+(isAdmin()?'<div class="section-head"><h2>Управление</h2><span>ADMIN</span></div><div class="panel"><p class="muted" style="margin-top:0;font-size:13px">Создавай задачи и проверяй работу команды.</p><div class="actions"><button class="btn" id="new-task">＋ Новая задача</button><button class="btn secondary" id="open-review">Проверка работ</button></div></div>':'');
  document.querySelector('#open-tasks').onclick=()=>setTab('tasks');document.querySelector('#new-task')?.addEventListener('click',createForm);document.querySelector('#open-review')?.addEventListener('click',showReview);
 }else if(tab==='tasks'){
  const list=await api('/tasks');view.innerHTML='<div class="section-head"><h2>Открытые задачи</h2><span>'+list.length+' ДОСТУПНО</span></div>'+(list.length?list.map(t=>taskCard(t)).join(''):empty('Пока тихо','Новых задач пока нет. Загляни чуть позже.'))+(isAdmin()?'<button class="btn block" id="new-task">＋ Создать задачу</button>':'');document.querySelector('#new-task')?.addEventListener('click',createForm);bindTaskActions();
 }else if(tab==='mine'){
  const list=await api('/tasks?status=mine');view.innerHTML='<div class="section-head"><h2>Мои задачи</h2><span>'+list.length+' ВСЕГО</span></div>'+(list.length?list.map(t=>taskCard(t)).join(''):empty('Здесь пока пусто','Возьми задачу из каталога — она появится здесь.','<div class="actions" style="justify-content:center"><button class="btn" id="go-catalog">К задачам</button></div>'));document.querySelector('#go-catalog')?.addEventListener('click',()=>setTab('tasks'));bindTaskActions();
 }else if(tab==='balance'){
  const rows=await api('/ledger');view.innerHTML='<div class="section-head"><h2>Финансы</h2><span>ИСТОРИЯ</span></div><section class="hero"><div class="eyebrow">ВСЕГО НАЧИСЛЕНО</div><div class="amount">'+rub(me.balance_kop)+'</div><div class="hero-foot">Внутренний баланс TaskHub</div></section><div class="panel">'+(rows.length?rows.map(x=>'<div class="ledger-row"><div><b>'+esc(x.title||x.kind)+'</b><small>'+esc(x.note||'Начисление')+' · '+esc(x.created_at)+'</small></div><b class="positive">+'+rub(x.amount_kop)+'</b></div>').join(''):empty('Начислений пока нет','После принятия задачи здесь появится запись.'))+'</div>';
 }else{
  view.innerHTML='<div class="section-head"><h2>Профиль</h2><span>ACCOUNT</span></div><div class="panel"><div class="profile-row"><div class="big-avatar">'+esc((me.user.first_name||'T').slice(0,1).toUpperCase())+'</div><div><div class="profile-name">'+esc(me.user.first_name)+'</div><div class="muted">@'+esc(me.user.username||'telegram')+'</div></div></div><div class="divider"></div><div class="task-bottom"><span class="muted">Роль</span><b>'+(isAdmin()?'Администратор':'Исполнитель')+'</b></div><div class="task-bottom" style="margin-top:12px"><span class="muted">Telegram ID</span><b>'+esc(me.user.telegram_id)+'</b></div></div>';
 }
}
async function claim(id){try{await api('/tasks/'+id+'/claim',{method:'POST'});me=await api('/me');toast('Задача закреплена за тобой');setTab('mine')}catch(e){toast(e.message)}}
function submitForm(id){
 view.insertAdjacentHTML('afterbegin','<section class="panel" id="submitbox"><div class="eyebrow">TASK DELIVERY</div><h2>Сдать работу</h2><p class="muted">Коротко опиши, что сделано. Можно добавить ссылку.</p><textarea class="field" id="submission" placeholder="Результат, ссылка или комментарий"></textarea><div class="actions"><button class="btn" id="send">Отправить на проверку</button><button class="btn secondary" id="cancel-submit">Отмена</button></div></section>');
 document.querySelector('#cancel-submit').onclick=()=>document.querySelector('#submitbox').remove();
 document.querySelector('#send').onclick=async()=>{const body=document.querySelector('#submission').value.trim();if(!body)return toast('Добавь описание результата');try{await api('/tasks/'+id+'/submit',{method:'POST',body:JSON.stringify({body})});toast('Работа отправлена на проверку');me=await api('/me');setTab('mine')}catch(e){toast(e.message)}};
}
function createForm(){
 view.insertAdjacentHTML('afterbegin','<section class="panel" id="createbox"><div class="eyebrow">NEW TASK</div><h2>Новая задача</h2><label>Название</label><input class="field" id="title" maxlength="120" placeholder="Например, подготовить пост"><label>Описание</label><textarea class="field" id="description" placeholder="Что нужно сделать и какой результат ожидается?"></textarea><label>Категория</label><select class="field" id="category"><option>Контент</option><option>Социальные сети</option><option>Discord</option><option>Исследования</option><option>Дизайн</option><option>Техническое</option><option>Другое</option></select><label>Оплата, ₽</label><input class="field" id="reward" type="number" min="0" step="1" placeholder="0"><label>Срок</label><input class="field" id="deadline" type="date"><div class="actions"><button class="btn" id="create">Опубликовать задачу</button><button class="btn secondary" id="cancel-create">Отмена</button></div></section>');
 document.querySelector('#cancel-create').onclick=()=>document.querySelector('#createbox').remove();
 document.querySelector('#create').onclick=async()=>{const title=document.querySelector('#title').value.trim(),reward=Number(document.querySelector('#reward').value);if(title.length<2)return toast('Добавь название задачи');if(!Number.isFinite(reward)||reward<0)return toast('Проверь сумму оплаты');try{await api('/tasks',{method:'POST',body:JSON.stringify({title,description:document.querySelector('#description').value,category:document.querySelector('#category').value,reward_rub:reward,deadline:document.querySelector('#deadline').value||null})});toast('Задача опубликована');setTab('tasks')}catch(e){toast(e.message)}};
}
async function showReview(){
 try{const rows=await api('/review');view.innerHTML='<div class="section-head"><h2>Проверка работ</h2><span>'+rows.length+' НА ПРОВЕРКЕ</span></div>'+(rows.length?rows.map(t=>'<article class="panel"><span class="pill">На проверке</span><h3>'+esc(t.title)+'</h3><div class="muted">Исполнитель: '+esc(t.assignee_name)+'</div><div class="divider"></div><p style="white-space:pre-wrap">'+esc(t.submission||'Без описания')+'</p><div class="actions"><button class="btn" data-review="accept" data-id="'+t.id+'">Принять</button><button class="btn secondary" data-review="revision" data-id="'+t.id+'">На доработку</button><button class="btn danger" data-review="reject" data-id="'+t.id+'">Отклонить</button></div></article>').join(''):empty('Всё проверено','Новых сдач пока нет.'));view.querySelectorAll('[data-review]').forEach(b=>b.onclick=async()=>{let comment='';if(b.dataset.review!=='accept'){comment=prompt('Что нужно исправить?')||'';if(!comment.trim())return}try{await api('/tasks/'+b.dataset.id+'/review',{method:'POST',body:JSON.stringify({decision:b.dataset.review,comment})});toast('Решение сохранено');showReview()}catch(e){toast(e.message)}})}
 catch(e){toast(e.message)}
}
document.querySelector('.nav-inner').addEventListener('click',e=>{const b=e.target.closest('button[data-tab]');if(b)setTab(b.dataset.tab)});
boot();
