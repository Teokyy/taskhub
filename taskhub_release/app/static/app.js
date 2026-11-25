const tg=window.Telegram?.WebApp;
tg?.ready();tg?.expand();
const initData=tg?.initData||'';
const headers={'Content-Type':'application/json','X-Telegram-Init-Data':initData};
const view=document.querySelector('#view');
let me=null,tab='room',toastTimer,rooms=[],activeRoom=null,roomBudget=null,renderSequence=0;
const rub=n=>(n/100).toLocaleString('ru-RU',{maximumFractionDigits:2})+' ₽';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const isAdmin=()=>['owner','admin'].includes(activeRoom?.room_role);
const workMode=()=>activeRoom?.work_mode||null;
function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,2600)}
function roomApiPath(path,opts={}){
 if(!activeRoom)return path;
 const prefix='/rooms/'+activeRoom.id;
 if(path==='/tasks')return prefix+'/tasks';
 if(path.startsWith('/tasks?'))return prefix+'/tasks'+path.slice(6);
 if(path==='/tasks'&&opts.method==='POST')return prefix+'/tasks';
 let m=path.match(/^\/tasks\/(\d+)\/(claim|submit|moderate|review|refuse)$/);
 if(m)return prefix+'/tasks/'+m[1]+'/'+m[2];
 if(path==='/moderation')return prefix+'/moderation';
 if(path==='/review')return prefix+'/review';
 if(path==='/team')return prefix+'/team';
 if(path==='/ledger')return prefix+'/ledger';
 if(path==='/budget')return prefix+'/budget';
 if(path==='/projects')return prefix+'/projects';
 if(path.startsWith('/projects/'))return prefix+path;
 return path;
}
async function api(path,opts={}){
 let target=path;
 if(path!=='/me'&&path!=='/rooms'&&!path.startsWith('/rooms')){
  const roomScoped=/^\/(tasks|ledger|budget|projects|team|moderation|review)(\/|\?|$)/.test(path);
  if(roomScoped&&!activeRoom?.id)throw Error('Сначала выбери рабочую комнату. Загружаем её данные…');
  target=roomApiPath(path,opts);
 }
 const r=await fetch('/api'+target,{...opts,headers:{...headers,...opts.headers}});
 let data={};try{data=await r.json()}catch{}
 if(!r.ok)throw Error(data.detail||('Ошибка '+r.status));
 if(path==='/ledger'&&data&&Array.isArray(data.entries))return data.entries;
 return data;
}
function persistRoom(id){try{sessionStorage.setItem('taskhub_room_id',String(id))}catch{}}
async function loadRooms(){
 rooms=await api('/rooms');
 if(!rooms.length){activeRoom=null;return}
 let saved=null;try{saved=Number(sessionStorage.getItem('taskhub_room_id'))}catch{}
 activeRoom=rooms.find(r=>r.id===saved)||rooms[0];persistRoom(activeRoom.id);
}
function renderRoomSelector(){
 const host=document.querySelector('#room-switcher');if(!host)return;
 if(!rooms.length){host.innerHTML='';return}
 if(!activeRoom||!rooms.some(r=>r.id===activeRoom.id))activeRoom=rooms[0];
 host.innerHTML='<select id="room-select" class="room-select" aria-label="Рабочая комната">'+rooms.map(r=>'<option value="'+r.id+'" '+(r.id===activeRoom.id?'selected':'')+'>'+esc(r.name)+'</option>').join('')+'</select>';
 document.querySelector('#room-select').onchange=async e=>{
  const selectedRoom=rooms.find(r=>r.id===Number(e.target.value));
  if(!selectedRoom)return toast('Комната не найдена. Обнови список комнат.');
  activeRoom=selectedRoom;persistRoom(activeRoom.id);
  try{await refreshMe();setTab('room')}catch(error){toast(error.message)}
 };
}
async function refreshMe(){
 me=await api('/market/profile');
 if(activeRoom?.id){try{roomBudget=await api('/rooms/'+activeRoom.id+'/budget')}catch{}}
}
function nav(){document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab))}
function setTab(next){tab=(next==='home'||next==='mine'||next==='balance')?'profile':next;nav();render()}
async function toggleWorkMode(){if(!activeRoom?.id)return toast('Сначала выбери рабочую комнату');const next=workMode()==='customer'?'executor':'customer';try{await api('/rooms/'+activeRoom.id+'/mode',{method:'PUT',body:JSON.stringify({mode:next})});activeRoom={...activeRoom,work_mode:next};toast(next==='customer'?'Режим изменён: заказчик':'Режим изменён: исполнитель');render()}catch(e){toast(e.message)}}
function greeting(){const h=new Date().getHours();return h<6?'Доброй ночи':h<12?'Доброе утро':h<18?'Добрый день':'Добрый вечер'}
async function boot(){
 try{
  me=await api('/me');
  document.querySelector('#avatar').innerHTML=me.user.photo_url?'<img src="'+esc(me.user.photo_url)+'" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:14px">':esc((me.user.first_name||'T').slice(0,1).toUpperCase());
  await loadRooms();
  renderRoomSelector();
  await refreshMe();
  await render();
 }catch(e){
  const web=window.Telegram?.WebApp,data=web?.initData||'';
  const reason=!web?'Telegram WebApp не обнаружен.':!data?'Telegram не передал initData. Открой Mini App через кнопку бота.':'Сервер не принял запрос. Возможно, миграция комнат ещё не включена.';
  view.innerHTML='<div class="panel"><div class="eyebrow">SIGN IN REQUIRED</div><h2>Не получилось загрузить TaskHub</h2><p class="muted">'+esc(reason)+'</p><p class="muted">'+esc(e.message)+' · initData: '+data.length+' символов</p><button class="btn secondary" onclick="location.reload()">Повторить</button></div>';
 }
}
function showRoomOnboarding(){
 view.innerHTML='<section class="hello"><div class="eyebrow">TASKHUB WORKSPACE</div><h1>Начнём с комнаты</h1><p>Комната — отдельное пространство для команды, задач и выплат.</p></section><div class="panel"><label>Название комнаты</label><input class="field" id="new-room-name" maxlength="80" placeholder="Например, Моя команда"><button class="btn block" id="create-room">Создать комнату</button></div><div class="panel"><label>Есть код приглашения?</label><input class="field" id="join-room-code" placeholder="Вставь код от администратора"><button class="btn secondary block" id="join-room">Вступить в комнату</button></div>';
 document.querySelector('#join-room').onclick=joinRoomByCode;
 document.querySelector('#create-room').onclick=async()=>{const name=document.querySelector('#new-room-name').value.trim();if(!name)return toast('Введи название комнаты');try{await api('/rooms',{method:'POST',body:JSON.stringify({name})});await loadRooms();renderRoomSelector();await refreshMe();showModeSetup()}catch(e){toast(e.message)}};
}
function showModeSetup(){
 if(!activeRoom){view.innerHTML='<div class="panel"><h2>Комната не выбрана</h2><p class="muted">Обновляем список комнат. Попробуй ещё раз.</p><button class="btn secondary" id="reload-rooms">Повторить</button></div>';document.querySelector('#reload-rooms').onclick=async()=>{try{await loadRooms();renderRoomSelector();if(activeRoom)showModeSetup();else showRoomOnboarding()}catch(e){toast(e.message)}};return}
 view.innerHTML='<section class="hello"><div class="eyebrow">НАСТРОЙКА КОМНАТЫ</div><h1>'+esc(activeRoom?.name||'Комната')+'</h1><p>Выбери, как ты будешь работать в этой комнате. Режим можно изменить позже, когда не останется активных обязательств.</p></section><div class="panel"><button class="btn block" id="mode-customer">Я заказчик — создаю задачи и принимаю работу</button><button class="btn secondary block" style="margin-top:10px" id="mode-executor">Я исполнитель — беру задачи и выполняю их</button></div>';
 document.querySelector('#mode-customer').onclick=()=>setMode('customer');document.querySelector('#mode-executor').onclick=()=>setMode('executor');
}
async function setMode(mode){try{if(!activeRoom?.id){await loadRooms();renderRoomSelector()}if(!activeRoom?.id)throw Error('Не удалось определить комнату. Обнови экран и попробуй снова.');const roomId=activeRoom.id;await api('/rooms/'+roomId+'/mode',{method:'PUT',body:JSON.stringify({mode})});activeRoom={...activeRoom,work_mode:mode};persistRoom(roomId);await refreshMe();toast('Режим комнаты сохранён');setTab('room')}catch(e){toast(e.message)}}

function empty(title,desc,action=''){return '<div class="empty"><b>'+esc(title)+'</b>'+esc(desc)+(action||'')+'</div>'}
function taskCard(t,adminView=false){
 let action='';
 if(t.status==='available'&&workMode()==='executor'&&Number(t.created_by)!==Number(me.user.id))action='<button class="btn" data-claim="'+t.id+'">Откликнуться</button>';
 if(['in_progress','revision'].includes(t.status)&&Number(t.assignee_id)===Number(me.user.id))action='<button class="btn" data-submit="'+t.id+'">Сдать работу</button>'+(t.status==='in_progress'?'<button class="btn secondary" data-refuse="'+t.id+'">Отказаться</button>':'');
 const statuses={available:'Открыта',in_progress:'В работе',submitted:'На проверке',revision:'Нужна доработка',accepted:'Принята',rejected:'Отклонена',cancelled:'Отменена',not_completed:'Не выполнена'};
 return '<article class="panel task"><div class="task-top"><span class="pill">'+esc(t.category||'Другое')+'</span><span class="price">'+rub(t.reward_kop)+'</span></div><h3>'+esc(t.title)+'</h3><p>'+esc(t.description||'Без дополнительного описания')+'</p><div class="task-bottom"><span class="status">'+(statuses[t.status]||esc(t.status))+'</span><span class="deadline">'+(t.deadline?'До '+esc(t.deadline):'Без срока')+'</span></div>'+(adminView&&t.assignee_name?'<div class="divider"></div><span class="muted">Исполнитель: '+esc(t.assignee_name)+'</span>':'')+(action?'<div class="actions">'+action+'</div>':'')+'</article>'
}
function bindTaskActions(){view.querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>claim(b.dataset.claim));view.querySelectorAll('[data-submit]').forEach(b=>b.onclick=()=>submitForm(b.dataset.submit))}
async function render(){
 const renderToken=++renderSequence;
 nav();
 if(tab==='room'&&!activeRoom?.id){showRoomOnboarding();return}
 if(tab==='room'&&activeRoom&&!activeRoom.work_mode){showModeSetup();return}
 if(tab==='room'){
  const mode=workMode()==='customer'?'Заказчик':'Исполнитель';
  view.innerHTML='<section class="hello"><div class="eyebrow">РАБОЧЕЕ ПРОСТРАНСТВО</div><h1>'+esc(activeRoom?.name||'Комната')+'</h1><p>Команда, участники и настройки общего пространства.</p></section><section class="panel"><div class="eyebrow">ТВОЯ РОЛЬ В КОМНАТЕ</div><div style="font-size:22px;font-weight:850;margin:7px 0">'+mode+'</div><div class="muted">'+(isAdmin()?'Владелец комнаты':'Участник комнаты')+'</div><div class="divider"></div><div class="task-bottom"><span class="muted">Рабочий режим</span><button class="btn secondary" id="room-switch-mode">Изменить</button></div></section><div class="section-head"><h2>Управление комнатой</h2></div><section class="panel"><b>Участники и доступ</b><p class="muted" style="margin:6px 0 12px">Приглашения, роли и настройки комнаты.</p><button class="btn block" id="room-manage">Открыть управление</button></section><section class="panel"><b>Работа комнаты</b><p class="muted" style="margin:6px 0 12px">Общие задачи и результаты команды доступны на бирже.</p><button class="btn secondary block" id="room-exchange">Перейти на биржу</button></section>';
  document.querySelector('#room-switch-mode').onclick=toggleWorkMode;document.querySelector('#room-manage').onclick=()=>isAdmin()?showAdminHub():toast('Управление доступно владельцу и администраторам комнаты');document.querySelector('#room-exchange').onclick=()=>setTab('exchange');
 }else if(tab==='exchange'){
  view.innerHTML='<section class="hello"><div class="eyebrow">ОБЩАЯ ПЛОЩАДКА TASKHUB</div><h1>Биржа</h1><p>Публичные задания. Комната для этого не нужна.</p></section><div class="panel muted">Загружаем открытые задания…</div>';
  let list;try{list=await api('/market/tasks')}catch(e){if(renderToken===renderSequence)view.innerHTML=empty('Не удалось загрузить биржу',e.message,'<button class="btn secondary" onclick="render()">Повторить</button>');return}
  if(renderToken!==renderSequence)return;
  const categories=[...new Set(list.map(t=>t.category||'Другое'))].sort((a,b)=>a.localeCompare(b,'ru'));
  view.innerHTML='<section class="hello"><div class="eyebrow">ОБЩАЯ ПЛОЩАДКА</div><h1>Открытые задания</h1><p>Частные задания от заказчиков и компаний.</p></section><div class="filter-row"><input class="field" id="task-search" style="margin:0;min-width:140px;flex:1" placeholder="Поиск по задачам"><button class="filter-chip active" data-filter="all">Все</button>'+categories.map(c=>'<button class="filter-chip" data-filter="'+esc(c)+'">'+esc(c)+'</button>').join('')+'</div><div class="filter-row"><select class="field" id="deadline-filter" style="margin:0;min-width:150px;max-width:210px"><option value="all">Любой срок</option><option value="dated">Со сроком</option><option value="none">Без срока</option><option value="overdue">Просроченные</option></select></div><div id="task-list"></div>'+'<button class="btn block" id="new-task">Разместить задание</button>';
  let selected='all';
  const draw=()=>{const q=(document.querySelector('#task-search')?.value||'').toLocaleLowerCase('ru');const deadlineMode=document.querySelector('#deadline-filter')?.value||'all';const today=new Date();today.setHours(0,0,0,0);const filtered=list.filter(t=>{const hasDeadline=!!t.deadline;const deadline=t.deadline?new Date(t.deadline):null;const overdue=deadline&&!Number.isNaN(deadline.getTime())&&deadline<today&&!['accepted','cancelled','rejected','not_completed'].includes(t.status);return(selected==='all'||(t.category||'Другое')===selected)&&(!q||(t.title+' '+(t.description||'')).toLocaleLowerCase('ru').includes(q))&&(deadlineMode==='all'||(deadlineMode==='dated'&&hasDeadline)||(deadlineMode==='none'&&!hasDeadline)||(deadlineMode==='overdue'&&overdue))});document.querySelector('#task-list').innerHTML=filtered.length?filtered.map(t=>taskCard(t)).join(''):empty('Задачи не найдены','Измени запрос или выбери другой срок.');bindTaskActions()};
  view.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{selected=b.dataset.filter;view.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));draw()});
  document.querySelector('#task-search').oninput=draw;document.querySelector('#deadline-filter').onchange=draw;document.querySelector('#new-task')?.addEventListener('click',createForm);draw();
 }else if(tab==='profile'){
  view.innerHTML='<section class="hello"><h1>Профиль</h1><p>Единый аккаунт TaskHub — не зависит от выбранной комнаты.</p></section><div class="panel muted">Загружаем профиль…</div>';
  let profile;try{profile=await api('/market/profile')}catch(e){if(renderToken===renderSequence)view.innerHTML=empty('Не удалось загрузить профиль',e.message,'<button class="btn secondary" onclick="render()">Повторить</button>');return}
  if(renderToken!==renderSequence)return;
  me=profile;const mine=profile.tasks||[];
  const counts={in_progress:0,submitted:0,revision:0,accepted:0,rejected:0,not_completed:0};
  mine.forEach(t=>{if(t.assignee_id===me.user.id&&Object.prototype.hasOwnProperty.call(counts,t.status))counts[t.status]++});
  view.innerHTML='<section class="profile-row" style="margin:8px 2px 18px"><div class="big-avatar">'+esc((me.user.first_name||'T').slice(0,1).toUpperCase())+'</div><div style="min-width:0;flex:1"><div class="profile-name">'+esc(me.user.first_name||'Участник')+'</div><div class="muted">Пользователь TaskHub</div></div></section><section class="panel balance-card"><div class="task-top"><div><div class="eyebrow">МОЙ БАЛАНС</div><div style="font-size:25px;font-weight:850;margin-top:4px">'+rub(me.balance_kop||0)+'</div></div><button class="btn secondary" id="profile-finance">История</button></div></section><div class="section-head"><h2>Мои задачи</h2><span>'+mine.filter(t=>t.assignee_id===me.user.id).length+' ВСЕГО</span></div><div class="stats"><div class="stat"><small>В работе</small><strong>'+counts.in_progress+'</strong></div><div class="stat"><small>На проверке</small><strong>'+counts.submitted+'</strong></div><div class="stat"><small>На доработке</small><strong>'+counts.revision+'</strong></div><div class="stat"><small>Принято</small><strong>'+counts.accepted+'</strong></div></div><div class="section-head"><h2>Мои задания как заказчика</h2><span>'+mine.filter(t=>t.created_by===me.user.id).length+'</span></div><div id="my-market-tasks">'+(mine.filter(t=>t.created_by===me.user.id).length?mine.filter(t=>t.created_by===me.user.id).map(t=>taskCard(t)).join(''):empty('Ты ещё не размещал задания','Опубликуй первое задание на общей бирже.'))+'</div><div class="section-head"><h2>Команды</h2></div><section class="panel"><p class="muted">'+(rooms.length?'Состоишь в '+rooms.length+' комнатах. Командные настройки доступны отдельно.':'Ты не состоишь в командах. Это никак не ограничивает биржу.')+'</p><button class="btn secondary block" id="profile-rooms">Открыть комнаты</button></section>';
  document.querySelector('#profile-finance').onclick=showFinanceScreen;
  document.querySelector('#profile-rooms').onclick=()=>setTab('room');
}else if(tab==='admin'){
  showAdminHub();
 }
}
async function showFinanceScreen(){
 if(!activeRoom?.id)return toast('Сначала выбери рабочую комнату');
 try{
  const finance=await api('/rooms/'+activeRoom.id+'/ledger');
  const rows=finance.entries||[];
  view.innerHTML='<div class="section-head"><button class="btn ghost" id="finance-back">← Профиль</button><h2>Финансы</h2></div><section class="panel"><div class="eyebrow">ТЕКУЩИЙ БАЛАНС</div><div style="font-size:27px;font-weight:850;margin-top:5px">'+rub(finance.balance_kop||0)+'</div><div class="muted" style="font-size:12px;margin-top:5px">Начисления за принятые задачи</div></section><div class="section-head"><h2>История начислений</h2></div><section class="panel">'+(rows.length?rows.map(x=>'<div class="ledger-row"><div><b>'+esc(x.title||x.kind)+'</b><small>'+esc(x.note||'Начисление')+' · '+esc(x.created_at)+'</small></div><b class="positive">+'+rub(x.amount_kop)+'</b></div>').join(''):empty('Начислений пока нет','После приёмки задачи здесь появится запись.'))+'</section>';
  document.querySelector('#finance-back').onclick=()=>setTab('profile');
 }catch(e){toast(e.message)}
}
async function loadTelegramStatus(){if(!activeRoom?.id){const status=document.querySelector('#telegram-status');if(status)status.textContent='Сначала выбери рабочую комнату';return}try{const s=await api('/rooms/'+activeRoom.id+'/telegram');document.querySelector('#telegram-status').textContent=s.connected?'Подключена: '+s.chat_title:'Группа пока не подключена';document.querySelector('#telegram-disconnect').hidden=!s.connected}catch(e){document.querySelector('#telegram-status').textContent='Не удалось проверить подключение: '+e.message}}
async function createTelegramCode(){const b=document.querySelector('#telegram-connect');b.disabled=true;try{const r=await api('/rooms/'+activeRoom.id+'/telegram/code',{method:'POST'});const host=document.querySelector('#telegram-code-result');host.innerHTML='<div class="divider"></div><p class="muted">1. Добавь бота TaskHub в нужную группу. 2. Отправь там команду ниже. Код действует 10 минут.</p><div class="field" style="user-select:all;overflow-wrap:anywhere">/connect '+esc(r.code)+'</div><button class="btn secondary block" id="telegram-copy">Скопировать команду</button>';document.querySelector('#telegram-copy').onclick=async()=>{try{await navigator.clipboard.writeText('/connect '+r.code);toast('Команда скопирована')}catch{prompt('Скопируй команду','/connect '+r.code)}}}catch(e){toast(e.message)}finally{b.disabled=false}}
async function disconnectTelegram(){if(!confirm('Отключить Telegram-группу от этой комнаты?'))return;try{await api('/rooms/'+activeRoom.id+'/telegram',{method:'DELETE'});toast('Группа отключена');loadTelegramStatus()}catch(e){toast(e.message)}}

async function showAdminHub(){
 if(!activeRoom?.id)return toast('Сначала выбери рабочую комнату');
 const sections=[['Модерация','Проверка новых задач','open-moderation'],['Проверка работ','Приёмка и доработка','open-review'],['Все задачи','Общий список комнаты','open-all'],['Команда','Участники и роли','open-team'],['Финансы','Бюджет комнаты','open-budget']];
 view.innerHTML='<div class="section-head"><button class="btn ghost" id="admin-back">← Профиль</button><h2>Управление</h2></div>'+sections.map((s,i)=>'<button class="panel admin-link" data-admin="'+s[2]+'" style="display:flex;width:100%;align-items:center;text-align:left;justify-content:space-between;color:var(--text)"><span><b>'+s[0]+'</b><small class="muted" style="display:block;margin-top:4px">'+s[1]+'</small></span><span class="muted">›</span></button>').join('')+'<section class="panel" style="margin-top:12px"><h3>Telegram-группа</h3><p class="muted" id="telegram-status">Проверяем подключение…</p><div class="actions"><button class="btn secondary" id="telegram-connect">Подключить группу</button><button class="btn danger" id="telegram-disconnect" hidden>Отключить</button></div><div id="telegram-code-result"></div></section>';
 loadTelegramStatus();
 document.querySelector('#telegram-connect').onclick=createTelegramCode;
 document.querySelector('#telegram-disconnect').onclick=disconnectTelegram;
 document.querySelector('#admin-back').onclick=()=>setTab('profile');
 view.querySelectorAll('[data-admin]').forEach(b=>b.onclick=()=>{const actions={'open-moderation':showModeration,'open-review':showReview,'open-all':showAllTasks,'open-team':showTeam,'open-budget':showBudgetSettings};actions[b.dataset.admin]?.()});
}
async function claim(id){try{await api('/tasks/'+id+'/claim',{method:'POST'});await refreshMe();toast('Задача закреплена за тобой');setTab('mine')}catch(e){toast(e.message)}}
function submitForm(id){
 document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="submitbox"><section class="modal-sheet"><div class="eyebrow">TASK DELIVERY</div><h2>Сдать работу</h2><p class="muted">Коротко опиши, что сделано. Можно добавить ссылку.</p><textarea class="field" id="submission" placeholder="Результат, ссылка или комментарий"></textarea><div class="actions"><button class="btn" id="send">Отправить на проверку</button><button class="btn secondary" id="cancel-submit">Отмена</button></div></section>');
 document.querySelector('#cancel-submit').onclick=()=>document.querySelector('#submitbox').remove();document.querySelector('#submitbox').onclick=e=>{if(e.target.id==='submitbox')e.target.remove()};
 document.querySelector('#send').onclick=async()=>{const body=document.querySelector('#submission').value.trim();if(!body)return toast('Добавь описание результата');try{await api('/tasks/'+id+'/submit',{method:'POST',body:JSON.stringify({body})});toast('Работа отправлена на проверку');await refreshMe();setTab('mine')}catch(e){toast(e.message)}};
}
function createForm(){
 if(workMode()!=='customer')return toast('Переключись в режим заказчика, чтобы создавать задачи');
 document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="createbox"><section class="modal-sheet"><div class="eyebrow">TASK SPECIFICATION</div><h2>Новая задача</h2><label>Название</label><input class="field" id="title" maxlength="120" placeholder="Например, подготовить пост"><label>Описание и ожидаемый результат</label><textarea class="field" id="description" placeholder="Что нужно сделать? Какой результат должен получить заказчик?"></textarea><label>Категория</label><select class="field" id="category"><option>Контент</option><option>Социальные сети</option><option>Discord</option><option>Исследования</option><option>Дизайн</option><option>Техническое</option><option>Другое</option></select><label>Модель оплаты</label><select class="field" id="pricing"><option value="fixed">Фиксированная сумма</option><option value="normative">По нормативному времени</option></select><div id="fixed-fields"><label>Фиксированная оплата, ₽</label><input class="field" id="reward" type="number" min="0" step="1" placeholder="0"></div><div id="norm-fields" hidden><label>Нормативное время, минут</label><input class="field" id="norm" type="number" min="1" step="5" value="60"><div class="muted" style="font-size:12px;margin:-5px 0 12px">Базовая ставка — 100 ₽/час</div></div><label>Сложность</label><select class="field" id="difficulty"><option value="1">1.0 · Обычная</option><option value="1.2">1.2 · Немного сложнее</option><option value="1.5">1.5 · Сложная</option><option value="1.8">1.8 · Очень сложная</option><option value="2">2.0 · Экспертная</option></select><label>Срочность</label><select class="field" id="urgency"><option value="1">1.0 · Обычная</option><option value="1.1">1.1 · Повышенная</option><option value="1.25">1.25 · Срочная</option><option value="1.5">1.5 · Критическая</option></select><label>Срок выполнения</label><input class="field" id="deadline" type="date"><div class="panel" style="background:#202720;margin:10px 0"><div class="eyebrow">РАСЧЁТ ОПЛАТЫ</div><div id="price-preview" style="font-size:23px;font-weight:850;margin-top:6px">0 ₽</div><div class="muted" style="font-size:12px">Итоговая сумма зависит от нормативного времени, сложности и срочности. Качество учитывается при приёмке.</div></div><div class="actions"><button class="btn" id="create">Отправить на модерацию</button><button class="btn secondary" id="cancel-create">Отмена</button></div></section>');
 document.querySelector('#category').insertAdjacentHTML('afterend','<label>Проект (необязательно)</label><select class="field" id="task-project"><option value="">Без проекта</option></select><label>Этап (необязательно)</label><select class="field" id="task-stage" disabled><option value="">Без этапа</option></select>');
 const projectSelect=document.querySelector('#task-project'),stageSelect=document.querySelector('#task-stage');
 (async()=>{try{const projects=await api('/projects');projectSelect.innerHTML='<option value="">Без проекта</option>'+projects.map(p=>'<option value="'+p.id+'">'+esc(p.title)+' · '+rub(p.budget_kop)+'</option>').join('')}catch(e){toast('Не удалось загрузить проекты: '+e.message)}})();
 projectSelect.onchange=async()=>{stageSelect.innerHTML='<option value="">Без этапа</option>';stageSelect.disabled=!projectSelect.value;if(!projectSelect.value)return;try{const stages=await api('/projects/'+projectSelect.value+'/stages');stageSelect.innerHTML='<option value="">Без этапа</option>'+stages.map(s=>'<option value="'+s.id+'">'+esc(s.title)+' · '+rub(s.budget_kop)+'</option>').join('')}catch(e){toast(e.message)}};
 const pricing=document.querySelector('#pricing'),fixed=document.querySelector('#fixed-fields'),norm=document.querySelector('#norm-fields');
 const estimate=()=>{const base=pricing.value==='fixed'?Number(document.querySelector('#reward').value||0):100*Number(document.querySelector('#norm').value||0)/60*Number(document.querySelector('#difficulty').value)*Number(document.querySelector('#urgency').value);document.querySelector('#price-preview').textContent=Math.round(base*100)/100+' ₽'};
 pricing.onchange=()=>{fixed.hidden=pricing.value!=='fixed';norm.hidden=pricing.value!=='normative';estimate()};
 ['reward','norm','difficulty','urgency'].forEach(id=>document.querySelector('#'+id).addEventListener('input',estimate));
 document.querySelector('#cancel-create').onclick=()=>document.querySelector('#createbox').remove();document.querySelector('#createbox').onclick=e=>{if(e.target.id==='createbox')e.target.remove()};
 document.querySelector('#create').onclick=async()=>{const title=document.querySelector('#title').value.trim(),model=pricing.value,reward=Number(document.querySelector('#reward').value||0),normMinutes=Number(document.querySelector('#norm').value||0);if(title.length<2)return toast('Добавь название задачи');if(model==='fixed'&&reward<0)return toast('Проверь сумму оплаты');if(model==='normative'&&normMinutes<=0)return toast('Укажи нормативное время');const payload={title,description:document.querySelector('#description').value,category:document.querySelector('#category').value,pricing_model:model,reward_rub:model==='fixed'?reward:null,norm_minutes:model==='normative'?normMinutes:0,difficulty:Number(document.querySelector('#difficulty').value),urgency:Number(document.querySelector('#urgency').value),deadline:document.querySelector('#deadline').value||null};payload.project_id=projectSelect.value?Number(projectSelect.value):null;payload.stage_id=stageSelect.value?Number(stageSelect.value):null;const publishButton=document.querySelector('#create');publishButton.disabled=true;publishButton.textContent='Публикуем…';try{await api('/tasks',{method:'POST',body:JSON.stringify(payload)});document.querySelector('#createbox')?.remove();toast('Задача отправлена на модерацию');setTab('mine')}catch(e){toast(e.message);publishButton.disabled=false;publishButton.textContent='Опубликовать задачу'}};
}
async function showModeration(){
 try{
  const rows=await api('/moderation');
  view.innerHTML='<div class="section-head"><button class="btn ghost" id="admin-section-back">← Управление</button><h2>Модерация задач</h2><span>'+rows.length+' ОЖИДАЮТ</span></div>'+(rows.length?rows.map(t=>'<article class="panel"><span class="pill">Ожидает одобрения</span><h3>'+esc(t.title)+'</h3><p>'+esc(t.description||'Без описания')+'</p><div class="muted">Автор: '+esc(t.creator_name||'Участник')+' · '+esc(t.category)+'</div><div class="task-bottom" style="margin-top:12px"><span>Предложенная оплата</span><b>'+rub(t.reward_kop)+'</b></div><div class="muted" style="font-size:12px">Сложность ×'+t.difficulty+' · Срочность ×'+t.urgency+' · Норматив '+t.norm_minutes+' мин.</div><div class="actions"><button class="btn" data-moderate="approve" data-id="'+t.id+'">Одобрить и опубликовать</button><button class="btn danger" data-moderate="reject" data-id="'+t.id+'">Отклонить</button></div></article>').join(''):empty('Очередь пуста','Новых задач на модерацию нет.'));
  document.querySelector('#admin-section-back').onclick=showAdminHub;
  view.querySelectorAll('[data-moderate]').forEach(b=>b.onclick=async()=>{let comment='';if(b.dataset.moderate==='reject'){comment=prompt('Причина отклонения')||'';if(!comment.trim())return toast('Укажи причину');}try{await api('/tasks/'+b.dataset.id+'/moderate',{method:'POST',body:JSON.stringify({decision:b.dataset.moderate,comment})});toast(b.dataset.moderate==='approve'?'Задача опубликована':'Задача отклонена');showModeration()}catch(e){toast(e.message)}});
 }catch(e){toast(e.message)}
}
async function showAllTasks(){try{const rows=await api('/tasks?status=all');view.innerHTML='<div class="section-head"><button class="btn ghost" id="admin-section-back">← Управление</button><h2>Все задачи</h2><span>'+rows.length+' ВСЕГО</span></div>'+(rows.length?rows.map(t=>'<article class="panel task"><div class="task-top"><span class="pill">'+esc(t.status)+'</span><span class="price">'+rub(t.reward_kop)+'</span></div><h3>'+esc(t.title)+'</h3><p>'+esc(t.description||'')+'</p><div class="muted">Исполнитель: '+esc(t.assignee_name||'Не назначен')+'</div><div class="muted">Создал: '+esc(t.created_by||'—')+'</div></article>').join(''):empty('Задач пока нет','Создай первую задачу для команды.'));document.querySelector('#admin-section-back').onclick=showAdminHub}catch(e){toast(e.message)}}
async function showTeam(){
 try{
  const rows=await api('/team');
  view.innerHTML='<div class="section-head"><button class="btn ghost" id="admin-section-back">← Управление</button><h2>Участники комнаты</h2><span>'+rows.length+' ЧЕЛОВЕК</span></div>'+(rows.length?rows.map(u=>'<article class="panel profile-row"><div class="big-avatar" style="overflow:hidden">'+(u.photo_url?'<img src="'+esc(u.photo_url)+'" style="width:100%;height:100%;object-fit:cover">':esc((u.first_name||'?').slice(0,1)))+'</div><div style="flex:1;min-width:0"><b>'+esc(u.first_name)+'</b><div class="muted">@'+esc(u.username||'без username')+' · '+esc(u.room_role==='owner'?'Владелец':u.room_role==='admin'?'Администратор':'Участник')+'</div><div class="muted" style="font-size:12px">В работе: '+u.active_tasks+' · На проверке: '+u.submitted_tasks+'</div><div class="actions">'+(u.room_role==='owner'?'':(u.room_role==='admin'?'<button class="btn secondary" data-role="member" data-user="'+u.id+'">Снять админа</button>':(activeRoom.room_role==='owner'?'<button class="btn secondary" data-role="admin" data-user="'+u.id+'">Назначить админом</button>':''))+'<button class="btn danger" data-remove-member="'+u.id+'" '+(u.room_role==='admin'&&activeRoom.room_role!=='owner'?'disabled':'')+'>Удалить</button>')+'</div></div></article>').join(''):empty('Команда пока пустая','Участники появятся после входа по приглашению.'));document.querySelector('#admin-section-back').onclick=showAdminHub;
  view.querySelectorAll('[data-role]').forEach(b=>b.onclick=async()=>{try{await api('/rooms/'+activeRoom.id+'/members/'+b.dataset.user,{method:'PATCH',body:JSON.stringify({room_role:b.dataset.role})});toast('Роль участника обновлена');showTeam()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-remove-member]').forEach(b=>b.onclick=async()=>{if(!confirm('Удалить участника из комнаты? История и начисления сохранятся.'))return;try{await api('/rooms/'+activeRoom.id+'/members/'+b.dataset.removeMember,{method:'DELETE'});toast('Участник удалён из комнаты');showTeam()}catch(e){toast(e.message)}});
 }catch(e){toast(e.message)}
}
async function showRoomInvites(){
 if(!isAdmin())return toast('Нет прав');
 try{
  const rows=await api('/rooms/'+activeRoom.id+'/invites');
  view.innerHTML='<div class="section-head"><h2>Приглашения</h2><span>ДОСТУП В КОМНАТУ</span></div><section class="panel"><p class="muted" style="margin-top:0">Создай одноразовый код или ограничь его срок и число использований. Полный код показывается только один раз.</p><label>Срок действия</label><select class="field" id="invite-days"><option value="1">1 день</option><option value="7" selected>7 дней</option><option value="30">30 дней</option></select><label>Максимум использований</label><select class="field" id="invite-uses"><option value="1">1 человек</option><option value="5">5 человек</option><option value="10" selected>10 человек</option><option value="25">25 человек</option><option value="100">100 человек</option></select><button class="btn block" id="invite-create">Создать приглашение</button><div id="new-invite-result"></div></section><div class="section-head"><h2>Ранее созданные</h2><span>'+rows.length+'</span></div>'+(rows.length?rows.map(i=>'<article class="panel"><div class="task-top"><b>Приглашение #'+i.id+'</b><span class="pill">'+(i.revoked_at?'Отозвано':i.max_uses!==null&&i.uses_count>=i.max_uses?'Лимит исчерпан':'Активно')+'</span></div><div class="muted" style="font-size:12px">Использовано: '+i.uses_count+(i.max_uses===null?'': ' / '+i.max_uses)+' · До: '+esc(i.expires_at||'без срока')+'</div>'+(i.revoked_at?'':'<div class="actions"><button class="btn danger" data-revoke-invite="'+i.id+'">Отозвать</button></div>')+'</article>').join(''):empty('Приглашений пока нет','Создай код выше.'));
  document.querySelector('#invite-create').onclick=async()=>{const button=document.querySelector('#invite-create');button.disabled=true;try{const result=await api('/rooms/'+activeRoom.id+'/invites',{method:'POST',body:JSON.stringify({expires_in_days:Number(document.querySelector('#invite-days').value),max_uses:Number(document.querySelector('#invite-uses').value)})});document.querySelector('#new-invite-result').innerHTML='<div class="divider"></div><b>Новый код — сохрани и отправь участникам</b><div class="field" style="overflow-wrap:anywhere;user-select:all">'+esc(result.code)+'</div><button class="btn secondary block" id="copy-invite">Скопировать код</button>';document.querySelector('#copy-invite').onclick=async()=>{try{await navigator.clipboard.writeText(result.code);toast('Код скопирован')}catch{prompt('Скопируй код приглашения',result.code)}};toast('Приглашение создано');}catch(e){toast(e.message)}finally{button.disabled=false}};
  view.querySelectorAll('[data-revoke-invite]').forEach(b=>b.onclick=async()=>{if(!confirm('Отозвать приглашение?'))return;try{await api('/rooms/'+activeRoom.id+'/invites/'+b.dataset.revokeInvite,{method:'DELETE'});toast('Приглашение отозвано');showRoomInvites()}catch(e){toast(e.message)}});
 }catch(e){toast(e.message)}
}
async function joinRoomByCode(){
 const code=document.querySelector('#join-room-code')?.value.trim();if(!code)return toast('Введи код приглашения');
 try{const result=await api('/rooms/join',{method:'POST',body:JSON.stringify({code})});await loadRooms();renderRoomSelector();await refreshMe();toast('Ты вступил в комнату «'+result.room_name+'»');showModeSetup()}catch(e){toast(e.message)}
}
async function showReview(){
 try{const rows=await api('/review');view.innerHTML='<div class="section-head"><button class="btn ghost" id="admin-section-back">← Управление</button><h2>Проверка работ</h2><span>'+rows.length+' НА ПРОВЕРКЕ</span></div>'+(rows.length?rows.map(t=>'<article class="panel"><span class="pill">На проверке</span><h3>'+esc(t.title)+'</h3><div class="muted">Исполнитель: '+esc(t.assignee_name)+'</div><div class="divider"></div><p style="white-space:pre-wrap">'+esc(t.submission||'Без описания')+'</p><div class="actions"><button class="btn" data-review="accept" data-id="'+t.id+'">Принять</button><button class="btn secondary" data-review="revision" data-id="'+t.id+'">На доработку</button><button class="btn danger" data-review="reject" data-id="'+t.id+'">Отклонить</button></div></article>').join(''):empty('Всё проверено','Новых сдач пока нет.'));document.querySelector('#admin-section-back').onclick=showAdminHub;view.querySelectorAll('[data-review]').forEach(b=>b.onclick=async()=>{let comment='';if(b.dataset.review!=='accept'){comment=prompt('Что нужно исправить?')||'';if(!comment.trim())return}let quality='good';if(b.dataset.review==='accept'){quality=prompt('Качество: acceptable (90%), good (100%), excellent (110%), outstanding (120%)','good')||'good';if(!['acceptable','good','excellent','outstanding'].includes(quality))return toast('Неизвестная оценка качества')}try{await api('/tasks/'+b.dataset.id+'/review',{method:'POST',body:JSON.stringify({decision:b.dataset.review,comment,quality})});toast('Решение сохранено');showReview()}catch(e){toast(e.message)}})}
 catch(e){toast(e.message)}
}
document.querySelector('.nav-inner').addEventListener('click',e=>{const b=e.target.closest('button[data-tab]');if(b)setTab(b.dataset.tab)});
document.querySelector('#avatar')?.addEventListener('click',()=>setTab('profile'));
boot();
