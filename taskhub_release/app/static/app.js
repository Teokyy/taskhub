const tg=window.Telegram?.WebApp;
tg?.ready();tg?.expand();
const initData=tg?.initData||'';
const headers={'Content-Type':'application/json','X-Telegram-Init-Data':initData};
const view=document.querySelector('#view');
const startupParams=new URLSearchParams(window.location.search);
let me=null,tab=['room','exchange','profile','chats'].includes(startupParams.get('tab'))?startupParams.get('tab'):'exchange',toastTimer,rooms=[],activeRoom=null,roomBudget=null,renderSequence=0;
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
  const roomScoped=/^\/(tasks|ledger|budget|projects|team|moderation|review)(\/|\?|$)/.test(path)&&!path.startsWith('/moderation/disputes');
  if(roomScoped&&!activeRoom?.id)throw Error('Сначала выбери рабочую комнату. Загружаем её данные…');
  target=roomApiPath(path,opts);
 }
 const r=await fetch('/api'+target,{...opts,headers:{...headers,...opts.headers}});
 let data={};try{data=await r.json()}catch{}
 if(!r.ok){const detail=data.detail||('Ошибка '+r.status);const error=Error(detail+' ['+path+']');error.status=r.status;error.apiPath=path;throw error;}
 if(path==='/ledger'&&data&&Array.isArray(data.entries))return data.entries;
 return data;
}
async function establishSession(){
 // A fresh Telegram initData is authoritative on every Mini App launch.
 // Prefer it over sessionStorage so an old session cannot strand the user.
 if(initData){
  headers['X-Telegram-Init-Data']=initData;
  const session=await api('/session',{method:'POST'});
  if(!session?.token)throw Error('Сервер не выдал сессию TaskHub [POST /session]');
  headers['X-Telegram-Init-Data']=session.token;
  try{sessionStorage.setItem('taskhub_session',session.token)}catch{}
  return;
 }
 // Fallback is only for WebViews that do not provide initData on reopen.
 let cached='';
 try{cached=sessionStorage.getItem('taskhub_session')||''}catch{}
 if(!cached)throw Error('Telegram не передал initData и сохранённой сессии нет. Открой TaskHub через кнопку бота.');
 headers['X-Telegram-Init-Data']=cached;
 try{
  const session=await api('/session',{method:'POST'});
  if(!session?.token)throw Error('Сервер не обновил сессию TaskHub [POST /session]');
  headers['X-Telegram-Init-Data']=session.token;
  try{sessionStorage.setItem('taskhub_session',session.token)}catch{}
 }catch(error){
  try{sessionStorage.removeItem('taskhub_session')}catch{}
  throw error;
 }
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

const TASKHUB_THEMES=[{id:'system',label:'Системная'},{id:'dark',label:'Тёмная'},{id:'light',label:'Светлая'}];
function getTaskhubTheme(){try{return localStorage.getItem('taskhub_theme')||'system'}catch{return 'system'}}
function applyTaskhubTheme(theme){
 const value=['system','dark','light'].includes(theme)?theme:'system';
 try{localStorage.setItem('taskhub_theme',value)}catch{}
 if(value==='system')document.documentElement.removeAttribute('data-theme');
 else document.documentElement.setAttribute('data-theme',value);
 document.querySelectorAll('[data-theme-choice]').forEach(b=>{b.classList.toggle('active',b.dataset.themeChoice===value);b.setAttribute('aria-pressed',String(b.dataset.themeChoice===value))});
}
function renderThemeSettings(){
 const current=getTaskhubTheme();
 return '<section class="panel"><div class="task-top"><div><b>Оформление</b><div class="muted" style="font-size:12px;margin-top:3px">Выбери внешний вид TaskHub</div></div><span aria-hidden="true">◐</span></div><div class="theme-options">'+TASKHUB_THEMES.map(t=>'<button type="button" class="theme-option '+(t.id===current?'active':'')+'" data-theme-choice="'+t.id+'" aria-pressed="'+(t.id===current)+'">'+t.label+'</button>').join('')+'</div></section>';
}


function openThemePicker(){
 const old=document.querySelector('#theme-picker');if(old)old.remove();
 document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="theme-picker"><section class="modal-sheet" style="max-width:380px"><div class="modal-head"><div><div class="eyebrow">ВНЕШНИЙ ВИД</div><h2 style="margin:5px 0 0">Тема приложения</h2></div><button class="modal-close" id="theme-picker-close" aria-label="Закрыть">×</button></div>'+renderThemeSettings()+'</section></div>');
 const modal=document.querySelector('#theme-picker');
 modal.querySelector('#theme-picker-close').onclick=()=>modal.remove();
 modal.onclick=e=>{if(e.target===modal)modal.remove()};
 modal.querySelectorAll('[data-theme-choice]').forEach(b=>b.onclick=()=>{applyTaskhubTheme(b.dataset.themeChoice);modal.remove()});
}

function nav(){document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab))}
function setTab(next){tab=(next==='home'||next==='mine'||next==='balance')?'profile':next;nav();render()}
async function toggleWorkMode(){if(!activeRoom?.id)return toast('Сначала выбери рабочую комнату');const next=workMode()==='customer'?'executor':'customer';try{await api('/rooms/'+activeRoom.id+'/mode',{method:'PUT',body:JSON.stringify({mode:next})});activeRoom={...activeRoom,work_mode:next};toast(next==='customer'?'Режим изменён: заказчик':'Режим изменён: исполнитель');render()}catch(e){toast(e.message)}}
function greeting(){const h=new Date().getHours();return h<6?'Доброй ночи':h<12?'Доброе утро':h<18?'Добрый день':'Добрый вечер'}
function startPresenceHeartbeat(){
 const ping=()=>api('/presence',{method:'POST'}).catch(()=>{});
 ping();setInterval(ping,60000);
}

let onboardingState={role:'both',categories:[],tree:[],step:1};
function closeOnboarding(){document.querySelector('#onboarding-modal')?.remove()}
function renderOnboardingStep(){
 const host=document.querySelector('#onboarding-content');if(!host)return;
 const roleLabels={customer:'Заказчик',executor:'Исполнитель',both:'И то, и другое'};
 let body='';
 if(onboardingState.step===1){
  body='<div class="eyebrow">ШАГ 1 ИЗ 3</div><h2 style="margin:6px 0 8px">Как ты планируешь пользоваться TaskHub?</h2><p class="muted">Это поможет настроить стартовые разделы. Роль можно будет изменить позже.</p>'+
  ['customer','executor','both'].map(k=>'<button type="button" class="panel block" data-onboard-role="'+k+'" style="text-align:left;border-color:'+(onboardingState.role===k?'var(--lime)':'var(--line)')+';background:'+(onboardingState.role===k?'#263a25':'var(--panel)')+';color:var(--text);margin:9px 0"><b>'+(k==='customer'?'🧾 Ищу исполнителей':k==='executor'?'💼 Ищу задания и подработку':'🔁 И то, и другое')+'</b><div class="muted" style="font-size:12px;margin-top:4px">'+(k==='customer'?'Буду публиковать задачи и выбирать исполнителей':k==='executor'?'Буду находить задания и откликаться':'Хочу и размещать задачи, и брать чужие')+'</div></button>').join('')+
  '<button class="btn block" id="onboard-next">Продолжить</button>';
 }else if(onboardingState.step===2){
  const selected=new Set(onboardingState.categories);
  body='<div class="eyebrow">ШАГ 2 ИЗ 3</div><h2 style="margin:6px 0 8px">Какие задания тебе интересны?</h2><p class="muted">Выбери направления, чтобы получать уведомления о новых заданиях. Можно пропустить этот шаг. Не больше 20 категорий.</p><div class="muted" id="onboard-count">'+selected.size+' / 20 выбрано</div>'+
  onboardingState.tree.map(p=>'<section class="panel" style="margin:9px 0"><label style="display:flex;gap:10px;align-items:center;color:var(--text);font-weight:800"><input type="checkbox" data-onboard-category value="'+p.id+'" '+(selected.has(Number(p.id))?'checked':'')+'> '+esc(p.name)+'</label><div style="padding-left:22px">'+p.children.map(c=>'<label style="display:flex;gap:9px;align-items:center;padding:5px 0"><input type="checkbox" data-onboard-category value="'+c.id+'" '+(selected.has(Number(c.id))?'checked':'')+'> '+esc(c.name)+'</label>').join('')+'</div></section>').join('')+
  '<div class="actions"><button class="btn secondary" id="onboard-back">Назад</button><button class="btn" id="onboard-next" style="flex:1">Сохранить и продолжить</button></div>';
 }else{
  body='<div class="eyebrow">ШАГ 3 ИЗ 3</div><h2 style="margin:6px 0 8px">Всё готово!</h2><p class="muted">Ты выбрал режим: <b>'+roleLabels[onboardingState.role]+'</b>.</p><section class="panel"><b>Что дальше?</b><p class="muted" style="margin:7px 0 0">'+(onboardingState.role==='customer'?'Размести первое задание на бирже и выбери подходящего исполнителя.':onboardingState.role==='executor'?'Открой биржу, найди подходящее задание и отправь отклик.':'Можешь разместить своё задание или найти работу на общей бирже.')+'</p></section>'+
  (onboardingState.role!=='executor'?'<button class="btn block" id="onboard-create">➕ Разместить задание</button>':'')+
  '<button class="btn '+(onboardingState.role==='executor'?'':'secondary')+' block" id="onboard-market" style="margin-top:9px">🔎 Открыть биржу</button><button class="btn ghost block" id="onboard-finish" style="margin-top:5px">Позже</button>';
 }
 host.innerHTML=body;
 host.querySelectorAll('[data-onboard-role]').forEach(b=>b.onclick=()=>{onboardingState.role=b.dataset.onboardRole;renderOnboardingStep()});
 host.querySelectorAll('[data-onboard-category]').forEach(b=>b.onchange=()=>{const ids=[...host.querySelectorAll('[data-onboard-category]:checked')].map(x=>Number(x.value));if(ids.length>20){b.checked=false;toast('Максимум 20 категорий');return}onboardingState.categories=ids;const count=host.querySelector('#onboard-count');if(count)count.textContent=ids.length+' / 20 выбрано'});
 host.querySelector('#onboard-back')?.addEventListener('click',()=>{onboardingState.step=1;renderOnboardingStep()});
 host.querySelector('#onboard-next')?.addEventListener('click',async()=>{
  try{
   if(onboardingState.step===1){await api('/onboarding',{method:'PUT',body:JSON.stringify({role_flags:onboardingState.role})});onboardingState.step=2;renderOnboardingStep();return}
   await api('/market/subscriptions',{method:'PUT',body:JSON.stringify({category_ids:onboardingState.categories})});
   onboardingState.step=3;renderOnboardingStep();
  }catch(e){toast(e.message)}
 });
 const finish=async()=>{try{await api('/onboarding',{method:'PUT',body:JSON.stringify({completed:true})});closeOnboarding();toast('Настройка TaskHub завершена')}catch(e){toast(e.message)}};
 host.querySelector('#onboard-finish')?.addEventListener('click',finish);
 host.querySelector('#onboard-market')?.addEventListener('click',async()=>{await finish();setTab('exchange')});
 host.querySelector('#onboard-create')?.addEventListener('click',async()=>{await finish();setTab('exchange');setTimeout(()=>createForm('market'),0)});
}
async function openOnboarding(force=false){
 try{
  let status,tree,subscriptions;
  try{status=await api('/onboarding')}catch(e){throw Error('Не удалось загрузить настройки знакомства: '+e.message)}
  try{tree=await api('/categories')}catch(e){throw Error('Не удалось загрузить категории: '+e.message)}
  try{subscriptions=await api('/market/subscriptions')}catch(e){throw Error('Не удалось загрузить подписки: '+e.message)}
  if(!force&&(status.completed||status.skipped))return;
  onboardingState={role:['customer','executor','both'].includes(status.role_flags)?status.role_flags:'both',categories:subscriptions.map(x=>Number(x.id)),tree,step:1};
  document.querySelector('#onboarding-modal')?.remove();
  document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="onboarding-modal"><section class="modal-sheet" style="max-width:500px"><div class="modal-head"><div><div class="eyebrow">ЗНАКОМСТВО С TASKHUB</div><div class="muted" style="font-size:12px">Короткая настройка · около 2 минут</div></div><button class="modal-close" id="onboard-close" aria-label="Закрыть">×</button></div><div id="onboarding-content" style="margin-top:16px"></div><button class="btn ghost block" id="onboard-skip" style="margin-top:8px">Пропустить настройку</button></section></div>');
  document.querySelector('#onboard-close').onclick=closeOnboarding;
  document.querySelector('#onboard-skip').onclick=async()=>{try{await api('/onboarding',{method:'PUT',body:JSON.stringify({skipped:true})});closeOnboarding()}catch(e){toast(e.message)}};
  renderOnboardingStep();
 }catch(e){toast('Не удалось открыть знакомство с TaskHub: '+e.message)}
}

async function boot(){
 try{
  await establishSession();
  me=await api('/me');
  document.querySelector('#avatar').innerHTML=me.user.photo_url?'<img src="'+esc(me.user.photo_url)+'" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:14px">':esc((me.user.first_name||'T').slice(0,1).toUpperCase());
  activeRoom=null;rooms=[];
  await loadRooms();
  renderRoomSelector();
  await refreshMe();
  startPresenceHeartbeat();
  await render();
  if(startupParams.get('action')==='create'&&tab==='exchange')createForm('market');
  else await openOnboarding(false);
 }catch(e){
  const web=window.Telegram?.WebApp,data=web?.initData||'';
  const reason=e.status===401?'Сессия Telegram не подтверждена или истекла. Закрой Mini App и открой его заново через кнопку бота. Если ошибка повторится, нужно проверить BOT_TOKEN в Railway.':!web?'Telegram WebApp не обнаружен.':!data?'Telegram не передал initData. Открой Mini App через кнопку бота.':'Не удалось загрузить данные с сервера. Проверь API и миграцию базы данных.';
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
function openDeliveryRoute(taskId){
 const task=[...(me?.tasks||[]),...(window.marketTaskCache||[])].find(t=>Number(t.id)===Number(taskId));
 if(!task)return toast('Не удалось загрузить адреса доставки');
 const pickup=(task.pickup_address||'').trim(),delivery=(task.delivery_address||'').trim();
 if(!pickup&&!delivery)return toast('В задании не указаны точки маршрута');
 const destination=delivery||pickup,waypoints=pickup&&delivery?pickup:'';
 const open=(origin)=>{
  const params=new URLSearchParams({api:'1',destination,travelmode:task.transport==='walk'?'walking':task.transport==='bicycle'?'bicycling':'driving'});
  if(origin)params.set('origin',origin);
  if(waypoints)params.set('waypoints',waypoints);
  window.open('https://www.google.com/maps/dir/?'+params.toString(),'_blank','noopener');
 };
 if(!navigator.geolocation){open('');return}
 const button=document.querySelector('[data-delivery-route="'+taskId+'"]');
 if(button){button.disabled=true;button.textContent='Определяем местоположение…'}
 navigator.geolocation.getCurrentPosition(
  position=>{open(position.coords.latitude+','+position.coords.longitude);if(button){button.disabled=false;button.textContent='Показать маршрут'}},
  ()=>{open('');if(button){button.disabled=false;button.textContent='Маршрут без моей геопозиции'}},
  {enableHighAccuracy:true,timeout:10000,maximumAge:60000}
 );
}
function taskCard(t,adminView=false){
 const marketTask=t.room_id===null||t.room_id===undefined,own=Number(t.created_by)===Number(me.user.id),executor=Number(t.assignee_id)===Number(me.user.id);
 let action='';
 if(marketTask&&['open','available'].includes(t.status)){
  if(!own)action='<button class="btn" data-market-response="'+t.id+'">Откликнуться</button>';
  else action='<button class="btn secondary" data-view-responses="'+t.id+'">Отклики</button><button class="btn secondary" data-edit-market="'+t.id+'">Редактировать</button><button class="btn danger" data-delete-market="'+t.id+'">Снять с публикации</button>';
 }
 if(marketTask&&t.status==='in_progress'){
  if(own)action='<button class="btn" data-complete-market="'+t.id+'">Подтвердить выполнение</button><button class="btn secondary" data-open-deal-chat="'+t.id+'">Открыть чат</button><button class="btn secondary" data-edit-market="'+t.id+'">Изменить описание/срок</button>';
  if(executor)action='<button class="btn secondary" data-open-deal-chat="'+t.id+'">Открыть чат</button>';
  if(own||executor)action+='<button class="btn danger" data-dispute-market="'+t.id+'">Пожаловаться</button>';
  if(own||executor){
   if(t.cancel_requested_by){
    if(Number(t.cancel_requested_by)===Number(me.user.id))action+='<div class="muted">Запрос на отмену отправлен. Ждём ответа второй стороны.</div>';
    else action+='<button class="btn" data-cancel-deal="'+t.id+'">Подтвердить отмену</button><button class="btn danger" data-cancel-reject="'+t.id+'">Отклонить отмену</button>';
   }else action+='<button class="btn danger" data-cancel-deal="'+t.id+'">Запросить отмену</button>';
  }
 }
 if(marketTask&&t.status==='done'&&(own||executor))action='<button class="btn secondary" data-review-task="'+t.id+'">Оставить отзыв</button>';
 if(!marketTask&&t.status==='available'&&workMode()==='executor'&&!own)action='<button class="btn" data-claim="'+t.id+'">Взять задачу</button>';
 if(['in_progress','revision'].includes(t.status)&&executor&&!marketTask)action='<button class="btn" data-submit="'+t.id+'" data-market="0">Сдать работу</button>';
 const statuses={open:'Открыта',available:'Открыта',in_progress:'В работе',submitted:'На проверке',revision:'Нужна доработка',accepted:'Принята',done:'Завершена',disputed:'Спор',rejected:'Отклонена',cancelled:'Отменена',not_completed:'Не выполнена'};
 const irl=marketTask&&t.task_mode==='irl';
 const routeButton=irl&&executor&&t.status==='in_progress'&&(t.pickup_address||t.delivery_address)?'<button class="btn secondary" data-delivery-route="'+t.id+'">Показать маршрут</button>':'';
 const photosButton=irl?'<button class="btn secondary" data-task-photos="'+t.id+'">Фотографии</button>':'';
 const irlDetails=irl?'<div class="muted" style="font-size:12px;margin-top:8px">📍 '+esc(t.city||'Город не указан')+(t.pickup_address?'<br>Забрать: '+esc(t.pickup_address):'')+(t.delivery_address?'<br>Доставить: '+esc(t.delivery_address):'')+(t.parcel_weight_kg?'<br>Вес: '+esc(t.parcel_weight_kg)+' кг':'')+((t.parcel_length_cm&&t.parcel_width_cm&&t.parcel_height_cm)?'<br>Габариты: '+esc(t.parcel_length_cm)+' × '+esc(t.parcel_width_cm)+' × '+esc(t.parcel_height_cm)+' см':'')+'</div>':'';
 return '<article class="panel task"><div class="task-top"><span class="pill">'+esc(t.category||'Другое')+(irl?' · IRL':'')+'</span><span class="price">'+rub(t.reward_kop)+'</span></div><h3>'+esc(t.title)+'</h3><p>'+esc(t.description||'Без дополнительного описания')+'</p>'+irlDetails+'<div class="task-bottom"><span class="status">'+(statuses[t.status]||esc(t.status))+'</span><span class="deadline">'+(t.deadline?'До '+esc(t.deadline):'Без срока')+'</span></div>'+(adminView&&t.assignee_name?'<div class="divider"></div><span class="muted">Исполнитель: '+esc(t.assignee_name)+'</span>':'')+((action||routeButton||photosButton)?'<div class="actions">'+action+routeButton+photosButton+'</div>':'')+'</article>'
}
async function showTaskPhotos(taskId){
 try{
  const files=await api('/market/tasks/'+taskId+'/attachments');if(!files.length)return toast('Фотографий к заданию пока нет');
  const urls=[];for(const file of files){const response=await fetch(file.url,{headers:{'X-Telegram-Init-Data':headers['X-Telegram-Init-Data']}});if(!response.ok)throw Error('Не удалось загрузить фотографию');urls.push(URL.createObjectURL(await response.blob()))}
  const modal=document.createElement('div');modal.className='modal-backdrop';modal.innerHTML='<section class="modal-sheet"><div class="modal-head"><h2>Фотографии задания</h2><button class="modal-close" id="photos-close">×</button></div><div style="display:grid;gap:10px">'+urls.map(url=>'<img src="'+url+'" alt="Фото посылки" style="width:100%;max-height:60vh;object-fit:contain;border-radius:12px">').join('')+'</div></section>';document.body.append(modal);
  const close=()=>{urls.forEach(url=>URL.revokeObjectURL(url));modal.remove()};modal.querySelector('#photos-close').onclick=close;modal.onclick=e=>{if(e.target===modal)close()};
 }catch(e){toast(e.message)}
}
function bindTaskActions(){
 view.querySelectorAll('[data-task-photos]').forEach(b=>b.onclick=()=>showTaskPhotos(b.dataset.taskPhotos));
 view.querySelectorAll('[data-delivery-route]').forEach(b=>b.onclick=()=>openDeliveryRoute(b.dataset.deliveryRoute));
 view.querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>claim(b.dataset.claim));
 view.querySelectorAll('[data-market-claim]').forEach(b=>b.onclick=()=>claimMarketTask(b.dataset.marketClaim));
 view.querySelectorAll('[data-submit]').forEach(b=>b.onclick=()=>submitForm(b.dataset.submit,b.dataset.market==='1'));
 view.querySelectorAll('[data-market-review]').forEach(b=>b.onclick=()=>reviewMarketTask(b.dataset.id,b.dataset.marketReview));
 view.querySelectorAll('[data-edit-market]').forEach(b=>b.onclick=()=>{const task=(me.tasks||[]).find(t=>String(t.id)===b.dataset.editMarket)||(window.marketTaskCache||[]).find(t=>String(t.id)===b.dataset.editMarket);if(task)createForm('market',task);else api('/market/tasks?status=mine').then(rows=>{const t=rows.find(x=>String(x.id)===b.dataset.editMarket);if(t)createForm('market',t);else toast('Задание не найдено')}).catch(e=>toast(e.message))});
 view.querySelectorAll('[data-delete-market]').forEach(b=>b.onclick=()=>deleteMarketTask(b.dataset.deleteMarket));
 view.querySelectorAll('[data-market-response]').forEach(b=>b.onclick=()=>createMarketResponse(b.dataset.marketResponse));
 view.querySelectorAll('[data-view-responses]').forEach(b=>b.onclick=()=>showMarketResponses(b.dataset.viewResponses));
 view.querySelectorAll('[data-complete-market]').forEach(b=>b.onclick=()=>completeMarketTask(b.dataset.completeMarket));
 view.querySelectorAll('[data-cancel-deal]').forEach(b=>b.onclick=()=>requestDealCancellation(b.dataset.cancelDeal));
 view.querySelectorAll('[data-cancel-reject]').forEach(b=>b.onclick=async()=>{if(!confirm('Отклонить запрос на отмену сделки?'))return;try{await api('/market/tasks/'+b.dataset.cancelReject+'/cancel-request/reject',{method:'POST'});toast('Запрос отклонён');await render()}catch(e){toast(e.message)}});
 view.querySelectorAll('[data-open-deal-chat]').forEach(b=>b.onclick=()=>showDealChatByTask(b.dataset.openDealChat));
 view.querySelectorAll('[data-dispute-market]').forEach(b=>b.onclick=()=>createMarketDispute(b.dataset.disputeMarket));
 view.querySelectorAll('[data-review-task]').forEach(b=>b.onclick=()=>createMarketReview(b.dataset.reviewTask));
}

async function createMarketResponse(taskId){
 const message=prompt("Коротко расскажи, почему ты подходишь для этого задания","");
 if(message===null)return;
 const deadline=prompt("Предлагаемый срок (необязательно). Оставь пустым, если не нужно","");
 try{await api('/market/tasks/'+taskId+'/responses',{method:'POST',body:JSON.stringify({message,proposed_deadline:deadline||null})});toast('Отклик отправлен заказчику');await render()}catch(e){toast(e.message)}
}
async function showMarketResponses(taskId){
 try{const rows=await api('/market/tasks/'+taskId+'/responses');if(!rows.length){toast('Откликов пока нет');return}
 const html=rows.map(r=>'<article class="panel"><b>'+esc(r.executor_name||'Исполнитель')+'</b><p>'+esc(r.message||'Без сопроводительного сообщения')+'</p><div class="muted">'+esc(r.proposed_deadline||'Срок не предложен')+' · '+esc(r.status)+'</div>'+(r.status==='sent'?'<button class="btn" data-accept-response="'+r.id+'">Выбрать исполнителя</button>':'')+'</article>').join('');
 view.innerHTML='<div class="section-head"><button class="btn ghost" id="responses-back">← Биржа</button><h2>Отклики</h2></div>'+html;document.querySelector('#responses-back').onclick=()=>setTab('exchange');
 view.querySelectorAll('[data-accept-response]').forEach(b=>b.onclick=async()=>{try{await api('/market/responses/'+b.dataset.acceptResponse+'/accept',{method:'POST'});toast('Исполнитель выбран, чат открыт');await setTab('profile')}catch(e){toast(e.message)}});
 }catch(e){toast(e.message)}
}
async function requestDealCancellation(id){if(!confirm('Отправить второй стороне запрос на взаимную отмену? Сделка закроется только после согласия обоих.'))return;try{const r=await api('/market/tasks/'+id+'/cancel-request',{method:'POST'});toast(r.status==='cancelled'?'Сделка отменена по взаимному согласию':'Запрос отправлен второй стороне');await render()}catch(e){toast(e.message)}}
async function completeMarketTask(id){if(!confirm('Подтвердить выполнение задания?'))return;try{await api('/market/tasks/'+id+'/complete',{method:'POST'});toast('Сделка завершена');await render()}catch(e){toast(e.message)}}
async function createMarketDispute(id){const reason=prompt('Опиши проблему по сделке');if(!reason)return;try{await api('/market/tasks/'+id+'/disputes',{method:'POST',body:JSON.stringify({reason})});toast('Жалоба отправлена модератору');await render()}catch(e){toast(e.message)}}
async function createMarketReview(id){const rating=Number(prompt('Оценка от 1 до 5','5'));if(!Number.isInteger(rating)||rating<1||rating>5)return toast('Оценка должна быть от 1 до 5');const text=prompt('Короткий отзыв (необязательно)','')||'';try{await api('/market/tasks/'+id+'/reviews',{method:'POST',body:JSON.stringify({rating,text})});toast('Отзыв сохранён')}catch(e){toast(e.message)}}
async function showDealChatByTask(taskId){const rows=await api('/market/chats');const chat=rows.find(x=>String(x.task_id)===String(taskId));if(chat)showDealChat(chat.chat_id,chat.title);else toast('Чат сделки не найден')}
async function showDealChat(chatId,title){
 let lastId=0;view.innerHTML='<div class="section-head"><button class="btn ghost" id="chat-back">← Профиль</button><h2>'+esc(title||'Чат сделки')+'</h2></div><div id="deal-messages" class="panel" style="min-height:220px"></div><textarea class="field" id="deal-message" maxlength="2000" placeholder="Напиши сообщение…"></textarea><button class="btn block" id="send-deal-message">Отправить</button>';
 document.querySelector('#chat-back').onclick=()=>setTab('profile');
 const load=async()=>{try{const rows=await api('/market/chats/'+chatId+'/messages?after_id='+lastId);const box=document.querySelector('#deal-messages');if(!box)return;rows.forEach(x=>{lastId=Math.max(lastId,x.id);box.insertAdjacentHTML('beforeend','<div style="padding:8px 0;border-bottom:1px solid var(--line)" data-message-row="'+x.id+'"><b>'+esc(x.sender_name)+'</b><p style="margin:4px 0">'+esc(x.text)+'</p><small class="muted">'+esc(x.created_at)+'</small>'+(Number(x.sender_id)===Number(me.user.id)?'<button class="btn ghost" data-hide-message="'+x.id+'">Скрыть для собеседника</button>':'')+'</div>')});view.querySelectorAll('[data-hide-message]').forEach(b=>b.onclick=async()=>{try{await api('/market/messages/'+b.dataset.hideMessage+'/hide',{method:'POST'});b.disabled=true;b.textContent='Скрыто для собеседника'}catch(e){toast(e.message)}});}catch(e){toast(e.message)}};

 await load();const timer=setInterval(()=>{if(document.querySelector('#deal-messages'))load();else clearInterval(timer)},4000);
 document.querySelector('#send-deal-message').onclick=async()=>{const el=document.querySelector('#deal-message'),text=el.value.trim();if(!text)return;try{const r=await api('/market/chats/'+chatId+'/messages',{method:'POST',body:JSON.stringify({body:text})});el.value='';if(r.warning_external_contact)toast('Напоминание: не передавай личные контакты незнакомым пользователям');await load()}catch(e){toast(e.message)}};
}

async function render(){
 const renderToken=++renderSequence;
 nav();
 const roomSwitcher=document.querySelector('#room-switcher');if(roomSwitcher)roomSwitcher.hidden=tab!=='room';
 if(tab==='room'&&!activeRoom?.id){showRoomOnboarding();return}
 if(tab==='room'&&activeRoom&&!activeRoom.work_mode){showModeSetup();return}
 if(tab==='room'){
  const mode=workMode()==='customer'?'Заказчик':'Исполнитель';
  view.innerHTML='<section class="hello"><div class="eyebrow">РАБОЧЕЕ ПРОСТРАНСТВО</div><h1>'+esc(activeRoom?.name||'Комната')+'</h1><p>Команда, участники и настройки общего пространства.</p></section><section class="panel"><div class="eyebrow">ТВОЯ РОЛЬ В КОМНАТЕ</div><div style="font-size:22px;font-weight:850;margin:7px 0">'+mode+'</div><div class="muted">'+(isAdmin()?'Владелец комнаты':'Участник комнаты')+'</div><div class="divider"></div><div class="task-bottom"><span class="muted">Рабочий режим</span><button class="btn secondary" id="room-switch-mode">Изменить</button></div></section><div class="section-head"><h2>Управление комнатой</h2></div><section class="panel"><b>Участники и доступ</b><p class="muted" style="margin:6px 0 12px">Приглашения, роли и настройки комнаты.</p><button class="btn block" id="room-manage">Открыть управление</button></section><section class="panel"><b>Задачи команды</b><p class="muted" style="margin:6px 0 12px">Внутренние задачи, доступные участникам этой комнаты.</p><button class="btn block" id="room-tasks">Открыть задачи комнаты</button></section><section class="panel"><b>Публичная биржа</b><p class="muted" style="margin:6px 0 12px">Сторонние задания от заказчиков вне команды.</p><button class="btn secondary block" id="room-exchange">Открыть общую биржу</button></section>';
  document.querySelector('#room-switch-mode').onclick=toggleWorkMode;document.querySelector('#room-manage').onclick=()=>isAdmin()?showAdminHub():toast('Управление доступно владельцу и администраторам комнаты');document.querySelector('#room-exchange').onclick=()=>setTab('exchange');document.querySelector('#room-tasks').onclick=showRoomTasks;
 }else if(tab==='exchange'){
  view.innerHTML='<section class="hello"><div class="eyebrow">ОБЩАЯ ПЛОЩАДКА TASKHUB</div><h1>Биржа</h1><p>Публичные задания. Комната для этого не нужна.</p></section><div class="panel muted">Загружаем открытые задания…</div>';
  let list,categoryTree,courierProfile=null;try{[list,categoryTree,courierProfile]=await Promise.all([api('/market/tasks?limit=50&offset=0'),api('/categories'),api('/market/courier-profile').catch(()=>null)])}catch(e){if(renderToken===renderSequence)view.innerHTML=empty('Не удалось загрузить биржу',e.message,'<button class="btn secondary" onclick="render()">Повторить</button>');return}
  if(renderToken!==renderSequence)return;
  const categories=categoryTree.map(c=>c.name);
  view.innerHTML='<section class="hello"><div class="eyebrow">ОБЩАЯ ПЛОЩАДКА</div><h1>Открытые задания</h1><p>Частные задания от заказчиков и компаний.</p></section><div class="filter-row"><input class="field" id="task-search" style="margin:0;min-width:140px;flex:1" placeholder="Поиск по задачам"><button class="filter-chip active" data-filter="all">Все</button>'+categories.map(c=>'<button class="filter-chip" data-filter="'+esc(c)+'">'+esc(c)+'</button>').join('')+'</div><div class="filter-row"><select class="field" id="deadline-filter" style="margin:0;min-width:150px;max-width:210px"><option value="all">Любой срок</option><option value="dated">Со сроком</option><option value="none">Без срока</option><option value="overdue">Просроченные</option></select><input class="field" id="city-filter" style="margin:0;min-width:140px;max-width:220px" placeholder="Город" value="'+esc(courierProfile?.city||'')+'"><label class="filter-chip" style="display:flex;align-items:center;gap:6px"><input id="courier-fit-filter" type="checkbox"> Подходящие мне</label></div><div id="task-list"></div><div id="market-pagination"></div>'+'<button class="btn block" id="new-task">Разместить задание</button>';
  let selected='all',offset=list.length,hasMore=list.length===50;
  const draw=()=>{const q=(document.querySelector('#task-search')?.value||'').toLocaleLowerCase('ru');const city=(document.querySelector('#city-filter')?.value||'').trim().toLocaleLowerCase('ru');const fitOnly=!!document.querySelector('#courier-fit-filter')?.checked;const deadlineMode=document.querySelector('#deadline-filter')?.value||'all';const today=new Date();today.setHours(0,0,0,0);const filtered=list.filter(t=>{const hasDeadline=!!t.deadline;const deadline=t.deadline?new Date(t.deadline):null;const overdue=deadline&&!Number.isNaN(deadline.getTime())&&deadline<today&&!['accepted','cancelled','rejected','not_completed'].includes(t.status);return(selected==='all'||(t.category||'Другое')===selected||(t.category||'').startsWith(selected+' / '))&&(!q||(t.title+' '+(t.description||'')).toLocaleLowerCase('ru').includes(q))&&(deadlineMode==='all'||(deadlineMode==='dated'&&hasDeadline)||(deadlineMode==='none'&&!hasDeadline)||(deadlineMode==='overdue'&&overdue))&&(!city||(t.task_mode==='irl'&&(t.city||'').toLocaleLowerCase('ru').includes(city)))&&(!fitOnly||(t.task_mode!=='irl'||(courierProfile&&courierProfile.city.toLocaleLowerCase('ru')===(t.city||'').toLocaleLowerCase('ru')&&(!t.parcel_weight_kg||t.parcel_weight_kg<=courierProfile.max_weight_kg)&&(!t.parcel_length_cm||t.parcel_length_cm<=courierProfile.max_length_cm)&&(!t.parcel_width_cm||t.parcel_width_cm<=courierProfile.max_width_cm)&&(!t.parcel_height_cm||t.parcel_height_cm<=courierProfile.max_height_cm))))});document.querySelector('#task-list').innerHTML=filtered.length?filtered.map(t=>taskCard(t)).join(''):empty('Задачи не найдены','Измени запрос или выбери другой срок.');const pager=document.querySelector('#market-pagination');pager.innerHTML=hasMore?'<button class="btn secondary block" id="load-market-more">Показать ещё</button>':'';document.querySelector('#load-market-more')?.addEventListener('click',async()=>{const b=document.querySelector('#load-market-more');b.disabled=true;b.textContent='Загружаем…';try{const page=await api('/market/tasks?limit=50&offset='+offset);list.push(...page);offset+=page.length;hasMore=page.length===50;draw()}catch(e){toast(e.message);b.disabled=false;b.textContent='Повторить'}});bindTaskActions()};
  view.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{selected=b.dataset.filter;view.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));draw()});
  document.querySelector('#task-search').oninput=draw;document.querySelector('#deadline-filter').onchange=draw;document.querySelector('#city-filter').oninput=draw;document.querySelector('#courier-fit-filter').onchange=draw;document.querySelector('#new-task')?.addEventListener('click',createForm);draw();bindTaskActions();

 }else if(tab==='profile'){
  view.innerHTML='<section class="hello"><h1>Профиль</h1><p>Твои задания, отклики, диалоги и репутация.</p></section><div class="panel muted">Загружаем профиль…</div>';
  let profile;try{profile=await api('/market/profile')}catch(e){if(renderToken===renderSequence)view.innerHTML=empty('Не удалось загрузить профиль',e.message,'<button class="btn secondary" onclick="render()">Повторить</button>');return}
  if(renderToken!==renderSequence)return;
  me=profile;const mine=(profile.tasks||[]).filter(t=>t.room_id===null||t.room_id===undefined);
  const counts={open:0,in_progress:0,done:0,disputed:0,...(profile.counts||{})};
  const customerTasks=mine.filter(t=>Number(t.created_by)===Number(me.user.id));
  view.innerHTML='<section class="profile-row" style="margin:8px 2px 18px"><div class="big-avatar">'+esc((me.user.first_name||'T').slice(0,1).toUpperCase())+'</div><div style="min-width:0;flex:1"><div class="profile-name">'+esc(me.user.first_name||'Участник')+'</div><div class="muted">Участник TaskHub</div><div class="muted" style="font-size:12px;margin-top:3px">★ '+Number(profile.rating_average||0).toFixed(1)+' · '+(profile.review_count||0)+' отзывов</div><div class="muted" style="font-size:12px;margin-top:5px">'+(profile.achievements||[]).map(a=>a.code==='first_quest'?'🏆 Первый квест':a.code==='first_task'?'✍️ Первое задание':a.code==='first_response'?'⚡ Первый отклик':a.code).join(' · ')+'</div></div></section><section class="panel"><b>Оплата напрямую</b><p class="muted" style="margin:6px 0 0">Заказчик и исполнитель рассчитываются самостоятельно. TaskHub фиксирует ход сделки, но не хранит деньги и не проводит выплаты.</p></section><div class="section-head"><h2>Мои сделки</h2><span>'+mine.length+' ВСЕГО</span></div><div class="stats"><div class="stat"><small>В работе</small><strong>'+counts.in_progress+'</strong></div><div class="stat"><small>Завершено</small><strong>'+counts.done+'</strong></div><div class="stat"><small>Открытые</small><strong>'+customerTasks.filter(t=>['open','available'].includes(t.status)).length+'</strong></div><div class="stat"><small>Споры</small><strong>'+counts.disputed+'</strong></div></div><div class="section-head"><h2>Мои задания как заказчика</h2><span>'+customerTasks.length+'</span></div><div id="my-market-tasks">'+(customerTasks.length?customerTasks.map(t=>taskCard(t)).join(''):empty('Ты ещё не размещал задания','Опубликуй первое задание на общей бирже.'))+'</div><div class="section-head"><h2>Мой аккаунт</h2></div><div id="profile-actions" class="panel" style="display:grid;gap:9px"><button class="btn secondary block" id="my-responses">Мои отклики</button><button class="btn secondary block" id="my-chats">Мои диалоги</button><button class="btn secondary block" id="my-subscriptions">Подписки на категории</button><button class="btn secondary block" id="courier-settings">Профиль курьера и города</button><button class="btn secondary block" id="profile-onboarding">Знакомство с TaskHub</button>'+(['admin','moderator'].includes(me.user.role)?'<button class="btn secondary block" id="market-moderation">Модерация биржи</button>':'')+'</div><div id="profile-theme"></div>';
  document.querySelector('#my-responses').onclick=showMyMarketResponses;
  document.querySelector('#my-chats').onclick=showMarketChats;
  document.querySelector('#my-subscriptions').onclick=showMarketNotificationSettings;
  document.querySelector('#courier-settings').onclick=showCourierProfile;
  document.querySelector('#profile-onboarding').onclick=()=>openOnboarding(true);
  document.querySelector('#market-moderation')?.addEventListener('click',showMarketplaceModeration);
  document.querySelector('#profile-theme').innerHTML=renderThemeSettings();
  document.querySelector('#profile-theme').querySelectorAll('[data-theme-choice]').forEach(b=>b.onclick=()=>applyTaskhubTheme(b.dataset.themeChoice));
  bindTaskActions();
}else if(tab==='chats'){
  await showMarketChats();
 }else if(tab==='admin'){
  showAdminHub();
 }
}
async function showRoomTasks(){
 if(!activeRoom?.id)return toast('Комната не выбрана');
 try{
  const rows=await api('/rooms/'+activeRoom.id+'/tasks');
  view.innerHTML='<div class="section-head"><button class="btn ghost" id="room-tasks-back">← Комната</button><h2>Задачи команды</h2><span>'+rows.length+'</span></div><p class="muted">Эти задания видны только участникам комнаты.</p>'+(rows.length?rows.map(t=>taskCard(t)).join(''):empty('Пока нет задач','Создай первую задачу для команды.'))+(workMode()==='customer'?'<button class="btn block" id="room-new-task">Создать задачу комнаты</button>':'');
  document.querySelector('#room-tasks-back').onclick=()=>setTab('room');
  document.querySelector('#room-new-task')?.addEventListener('click',()=>createForm('room'));
  bindTaskActions();
 }catch(e){view.innerHTML=empty('Не удалось загрузить задачи комнаты',e.message,'<button class="btn secondary" onclick="showRoomTasks()">Повторить</button>')}
}
async function showCourierProfile(){
 try{
  const current=await api('/market/courier-profile');
  const value=current||{city:'',transport:'walk',max_weight_kg:5,max_length_cm:40,max_width_cm:30,max_height_cm:30,max_items:1};
  view.innerHTML='<div class="section-head"><button class="btn ghost" id="courier-back">← Профиль</button><h2>Профиль курьера</h2></div><p class="muted">Укажи, в каких городах и с каким грузом ты готов работать. Эти параметры будут использоваться для подбора IRL-заказов.</p><section class="panel"><label>Город работы</label><input class="field" id="courier-city" maxlength="120" placeholder="Например, Екатеринбург"><label>Транспорт</label><select class="field" id="courier-transport"><option value="walk">Пешком</option><option value="bicycle">Велосипед</option><option value="car">Автомобиль</option><option value="motorcycle">Мотоцикл</option></select><label>Максимальный вес, кг</label><input class="field" id="courier-weight" type="number" min="0.1" max="200" step="0.1"><label>Максимальные габариты, см (Д × Ш × В)</label><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:7px"><input class="field" id="courier-length" type="number" min="1" max="500"><input class="field" id="courier-width" type="number" min="1" max="500"><input class="field" id="courier-height" type="number" min="1" max="500"></div><label>Количество предметов</label><input class="field" id="courier-items" type="number" min="1" max="50"><button class="btn block" id="courier-save">Сохранить профиль</button></section>';
  document.querySelector('#courier-city').value=value.city||'';document.querySelector('#courier-transport').value=value.transport||'walk';document.querySelector('#courier-weight').value=value.max_weight_kg??5;document.querySelector('#courier-length').value=value.max_length_cm??40;document.querySelector('#courier-width').value=value.max_width_cm??30;document.querySelector('#courier-height').value=value.max_height_cm??30;document.querySelector('#courier-items').value=value.max_items??1;
  document.querySelector('#courier-back').onclick=()=>setTab('profile');
  document.querySelector('#courier-save').onclick=async()=>{const payload={city:document.querySelector('#courier-city').value.trim(),transport:document.querySelector('#courier-transport').value,max_weight_kg:Number(document.querySelector('#courier-weight').value),max_length_cm:Number(document.querySelector('#courier-length').value),max_width_cm:Number(document.querySelector('#courier-width').value),max_height_cm:Number(document.querySelector('#courier-height').value),max_items:Number(document.querySelector('#courier-items').value)};if(payload.city.length<2)return toast('Укажи город работы');const b=document.querySelector('#courier-save');b.disabled=true;try{await api('/market/courier-profile',{method:'PUT',body:JSON.stringify(payload)});toast('Профиль курьера сохранён');setTab('profile')}catch(e){toast(e.message);b.disabled=false}};
 }catch(e){toast(e.message)}
}

async function showFinanceScreen(){
 try{
  const finance=await api('/market/ledger');
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
async function claim(id){try{await api('/tasks/'+id+'/claim',{method:'POST'});await refreshMe();toast('Задача закреплена за тобой');setTab('profile')}catch(e){toast(e.message)}}
async function claimMarketTask(id){try{await api('/market/tasks/'+id+'/claim',{method:'POST'});await refreshMe();toast('Задание закреплено за тобой');setTab('profile')}catch(e){toast(e.message)}}
async function reviewMarketTask(id,decision){let comment='';if(decision!=='accept'){comment=prompt('Укажи комментарий или причину')||'';if(!comment.trim())return}const quality=decision==='accept'?(prompt('Качество: acceptable, good, excellent или outstanding','good')||'good'):'good';try{await api('/market/tasks/'+id+'/review',{method:'POST',body:JSON.stringify({decision,comment,quality})});toast('Решение сохранено');await refreshMe();setTab('profile')}catch(e){toast(e.message)}}
function submitForm(id,market=false){
 document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="submitbox"><section class="modal-sheet"><div class="eyebrow">TASK DELIVERY</div><h2>Сдать работу</h2><p class="muted">Коротко опиши, что сделано. Можно добавить ссылку.</p><textarea class="field" id="submission" placeholder="Результат, ссылка или комментарий"></textarea><div class="actions"><button class="btn" id="send">Отправить на проверку</button><button class="btn secondary" id="cancel-submit">Отмена</button></div></section>');
 document.querySelector('#cancel-submit').onclick=()=>document.querySelector('#submitbox').remove();document.querySelector('#submitbox').onclick=e=>{if(e.target.id==='submitbox')e.target.remove()};
 document.querySelector('#send').onclick=async()=>{const body=document.querySelector('#submission').value.trim();if(!body)return toast('Добавь описание результата');try{await api((market?'/market/tasks/':'/tasks/')+id+'/submit',{method:'POST',body:JSON.stringify({body})});toast('Работа отправлена на проверку');await refreshMe();setTab('profile')}catch(e){toast(e.message)}};
}
const MARKET_CATEGORY_OPTIONS = [
 'Маркетинг / SMM','Маркетинг / Контент','Маркетинг / Таргетированная реклама','Маркетинг / SEO','Маркетинг / Аналитика',
 'Discord / Создание сервера','Discord / Настройка и оформление','Discord / Боты и автоматизация','Discord / Модерация','Discord / Интеграции',
 'Игры / Пополнение / Steam','Игры / Пополнение / Roblox','Игры / Пополнение / Mobile Legends','Игры / Пополнение / Другие игры',
 'Игры / Валюта и предметы','Игры / Прокачка аккаунта','Игры / Помощь в прохождении','Игры / Буст и рейтинг','Игры / Аккаунты и услуги',
 'Дизайн / Баннеры и обложки','Дизайн / Логотипы','Дизайн / UI и UX',
 'Разработка / Сайты','Разработка / Боты','Разработка / Скрипты и автоматизация','Другое'
];
async function createForm(scope='market', existing=null){
 const editing=!!existing,market=scope==='market',locked=editing&&existing.status==='in_progress';let tree=[];
 if(market){try{tree=await api('/categories')}catch(e){toast('Не удалось загрузить категории: '+e.message);return}}
 const currentCategoryId=existing?.category_id??tree.flatMap(p=>p.children.map(c=>({id:c.id,label:p.name+' / '+c.name}))).find(c=>c.label===existing?.category)?.id;
 const opts=market?tree.map(p=>'<optgroup label="'+esc(p.name)+'">'+p.children.map(c=>'<option value="'+c.id+'" '+(Number(currentCategoryId)===Number(c.id)?'selected':'')+'>'+esc(c.name)+'</option>').join('')+'</optgroup>').join(''):[...MARKET_CATEGORY_OPTIONS].map(c=>'<option value="'+esc(c)+'">'+esc(c)+'</option>').join('');
 document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="createbox"><section class="modal-sheet"><div class="eyebrow">НОВОЕ ЗАДАНИЕ</div><h2>'+(editing?'Редактировать задание':'Публикация задания')+'</h2><label>Название</label><input class="field" id="title" maxlength="120" placeholder="Что нужно сделать?" '+(locked?'disabled':'')+' ><label>Описание</label><textarea class="field" id="description" placeholder="Опиши задачу и ожидаемый результат"></textarea>'+(market?'<label>Формат задания</label><select class="field" id="task-mode"><option value="online">Онлайн — удалённо</option><option value="irl">IRL — в городе</option></select><div id="irl-fields" hidden><label>Город выполнения</label><input class="field" id="task-city" maxlength="120" placeholder="Например, Екатеринбург"><label>Откуда забрать (если нужна доставка)</label><input class="field" id="pickup-address" maxlength="500" placeholder="Адрес или место встречи"><label>Куда доставить</label><input class="field" id="delivery-address" maxlength="500" placeholder="Адрес получателя"><label>Вес посылки, кг</label><input class="field" id="parcel-weight" type="number" min="0.1" max="200" step="0.1" placeholder="Например, 3"><label>Размеры посылки, см (Д × Ш × В)</label><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:7px"><input class="field" id="parcel-length" type="number" min="1" max="500" placeholder="Длина"><input class="field" id="parcel-width" type="number" min="1" max="500" placeholder="Ширина"><input class="field" id="parcel-height" type="number" min="1" max="500" placeholder="Высота"></div><label>Фотографии посылки (до 5, JPG/PNG/WEBP, до 10 МБ каждая)</label><input class="field" id="task-photos" type="file" accept="image/jpeg,image/png,image/webp" multiple><div class="muted" style="font-size:12px">Фото будут доступны заказчику и выбранному исполнителю.</div></div>':'')+'<label>Категория и направление</label>'+(market?'<input class="field" id="category-search" type="search" autocomplete="off" placeholder="Начни вводить: достав, ремонт, уборка…" '+(locked?'disabled':'')+'><div id="category-suggestions" class="panel" style="display:none;padding:6px;margin-top:5px;max-height:220px;overflow:auto"></div><div class="muted" id="category-selected" style="font-size:12px;margin:5px 0"></div><select class="field" id="category" hidden>'+opts+'</select>':'<select class="field" id="category">'+opts+'</select>')+'<div id="other-category-wrap" hidden><label>Что за услуга? (до 200 символов)</label><input class="field" id="other-category-text" maxlength="200" placeholder="Опиши свою нишу"></div><label>Оплата, ₽</label><input class="field" id="reward" type="number" min="1" step="1" placeholder="Сумма, о которой договоритесь напрямую" '+(locked?'disabled':'')+' ><label>Срок выполнения</label><input class="field" id="deadline" type="date">'+(locked?'<p class="muted">После выбора исполнителя можно изменить только описание и продлить срок.</p>':'')+'<p class="muted">TaskHub не принимает оплату. Заказчик и исполнитель рассчитываются напрямую.</p>'+'<div class="actions"><button class="btn" id="create">'+(editing?'Сохранить':'Опубликовать')+'</button><button class="btn secondary" id="cancel-create">Отмена</button></div></section></div>');
 const set=(id,value)=>{const el=document.querySelector('#'+id);if(el&&value!==undefined&&value!==null)el.value=value};
 set('title',existing?.title||'');set('description',existing?.description||'');set('reward',Math.round((existing?.reward_kop||0)/100));set('deadline',existing?.deadline||'');set('task-mode',existing?.task_mode||'online');set('task-city',existing?.city||'');set('pickup-address',existing?.pickup_address||'');set('delivery-address',existing?.delivery_address||'');set('parcel-weight',existing?.parcel_weight_kg??'');set('parcel-length',existing?.parcel_length_cm??'');set('parcel-width',existing?.parcel_width_cm??'');set('parcel-height',existing?.parcel_height_cm??'');const modeSelect=document.querySelector('#task-mode'),irlFields=document.querySelector('#irl-fields');const syncTaskMode=()=>{if(irlFields)irlFields.hidden=modeSelect?.value!=='irl'};modeSelect?.addEventListener('change',syncTaskMode);syncTaskMode();
 const category=document.querySelector('#category'),otherWrap=document.querySelector('#other-category-wrap');
 const flatCategories=tree.flatMap(p=>p.children.map(c=>({id:c.id,label:p.name+' / '+c.name,parent:p.name,name:c.name})));
 const searchInput=document.querySelector('#category-search'),suggestions=document.querySelector('#category-suggestions'),selectedLabel=document.querySelector('#category-selected');
 const syncOther=()=>{const selected=flatCategories.find(c=>String(c.id)===category.value);otherWrap.hidden=!(selected?.label==='Другое / Прочее');if(selectedLabel)selectedLabel.textContent=selected?'Выбрано: '+selected.label:''};
 const renderCategorySuggestions=()=>{
  if(!searchInput||!suggestions)return;
  const q=searchInput.value.trim().toLocaleLowerCase('ru').replace(/ё/g,'е');
  if(!q){suggestions.style.display='none';suggestions.innerHTML='';return}
  const matches=flatCategories.map(item=>({...item,hay:(item.label+' '+item.name+' '+item.parent).toLocaleLowerCase('ru').replace(/ё/g,'е')}))
   .filter(item=>item.hay.includes(q))
   .sort((a,b)=>Number(b.name.toLocaleLowerCase('ru').startsWith(q))-Number(a.name.toLocaleLowerCase('ru').startsWith(q))||a.label.localeCompare(b.label,'ru'))
   .slice(0,12);
  suggestions.innerHTML=matches.length?matches.map(item=>'<button type="button" class="block" data-category-pick="'+item.id+'" style="width:100%;padding:10px;border:0;border-radius:9px;background:transparent;color:var(--text);text-align:left"><b>'+esc(item.name)+'</b><small class="muted" style="display:block">'+esc(item.parent)+'</small></button>').join(''):'<div class="muted" style="padding:10px">Ничего не найдено. Попробуй другое слово.</div>';
  suggestions.style.display='block';
  suggestions.querySelectorAll('[data-category-pick]').forEach(button=>button.onclick=()=>{
   category.value=button.dataset.categoryPick;
   const picked=flatCategories.find(item=>String(item.id)===category.value);
   searchInput.value=picked?.name||'';
   suggestions.style.display='none';syncOther();
  });
 };
 if(market&&currentCategoryId)set('category',String(currentCategoryId));
 if(searchInput){const current=flatCategories.find(item=>String(item.id)===category.value);searchInput.value=current?.name||'';searchInput.addEventListener('input',renderCategorySuggestions);searchInput.addEventListener('focus',renderCategorySuggestions);}
 syncOther();if(category)category.onchange=syncOther;
 document.querySelector('#cancel-create').onclick=()=>document.querySelector('#createbox').remove();document.querySelector('#createbox').onclick=e=>{if(e.target.id==='createbox')e.target.remove()};
 document.querySelector('#create').onclick=async()=>{const title=document.querySelector('#title').value.trim(),reward=Number(document.querySelector('#reward').value||0);if(title.length<2)return toast('Добавь название задания');if(reward<=0)return toast('Укажи сумму вознаграждения');const selected=tree.flatMap(p=>p.children.map(c=>({id:c.id,label:p.name+' / '+c.name}))).find(c=>String(c.id)===category.value);const taskMode=market?(document.querySelector('#task-mode')?.value||'online'):'online';const payload={title,description:document.querySelector('#description').value,category_id:market?Number(category.value):null,category:market?(selected?.label||'Другое'):category.value,other_category_text:document.querySelector('#other-category-text')?.value||'',reward_rub:reward,pricing_model:'fixed',deadline:document.querySelector('#deadline').value||null,task_mode:taskMode,city:taskMode==='irl'?(document.querySelector('#task-city')?.value||'').trim():'',pickup_address:taskMode==='irl'?(document.querySelector('#pickup-address')?.value||'').trim():'',delivery_address:taskMode==='irl'?(document.querySelector('#delivery-address')?.value||'').trim():'',parcel_weight_kg:taskMode==='irl'&&document.querySelector('#parcel-weight')?.value?Number(document.querySelector('#parcel-weight').value):null,parcel_length_cm:taskMode==='irl'&&document.querySelector('#parcel-length')?.value?Number(document.querySelector('#parcel-length').value):null,parcel_width_cm:taskMode==='irl'&&document.querySelector('#parcel-width')?.value?Number(document.querySelector('#parcel-width').value):null,parcel_height_cm:taskMode==='irl'&&document.querySelector('#parcel-height')?.value?Number(document.querySelector('#parcel-height').value):null};if(taskMode==='irl'&&!payload.city)return toast('Укажи город выполнения');if(market&&selected?.label==='Другое / Прочее'&&!payload.other_category_text.trim())return toast('Опиши, какая услуга нужна');const button=document.querySelector('#create');button.disabled=true;button.textContent=editing?'Сохраняем…':'Публикуем…';try{const url=market?'/market/tasks'+(editing?'/'+existing.id:''):'/rooms/'+activeRoom.id+'/tasks';const saved=await api(url,{method:editing?'PUT':'POST',body:JSON.stringify(payload)});if(market&&taskMode==='irl'){const attachmentTaskId=editing?existing.id:saved.id;const photoFiles=[...(document.querySelector('#task-photos')?.files||[])].slice(0,5);for(const file of photoFiles){if(file.size>10*1024*1024){toast('Фото '+file.name+' больше 10 МБ');continue}try{const dataUrl=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result||''));reader.onerror=()=>reject(Error('Не удалось прочитать фото'));reader.readAsDataURL(file)});await api('/market/tasks/'+attachmentTaskId+'/attachments',{method:'POST',body:JSON.stringify({filename:file.name,mime_type:file.type,data_base64:dataUrl.split(',')[1]})})}catch(photoError){toast('Фото не загрузилось: '+photoError.message)}}}document.querySelector('#createbox')?.remove();toast(editing?'Изменения сохранены':scope==='room'?'Задача добавлена в комнату':'Задание опубликовано');await refreshMe();if(scope==='room')showRoomTasks();else setTab('exchange')}catch(e){toast(e.message);button.disabled=false;button.textContent=editing?'Сохранить':'Опубликовать'}};
}
async function deleteMarketTask(id){if(!confirm('Снять своё задание с биржи? Оно исчезнет из открытых, но история останется в профиле.'))return;try{await api('/market/tasks/'+id,{method:'DELETE'});toast('Задание снято с публикации');await refreshMe();setTab('profile')}catch(e){toast(e.message)}}
async function saveMarketNotificationPrefs(){const selected=[...document.querySelectorAll('[data-market-category]:checked')].map(x=>x.value);const enabled=!!document.querySelector('#market-notify-enabled')?.checked;try{await api('/market/notification-preferences',{method:'PUT',body:JSON.stringify({categories:selected,enabled})});toast('Настройки уведомлений сохранены')}catch(e){toast(e.message)}}
async function showMarketNotificationSettings(){
 let [tree,selected]=[[],[]];try{[tree,selected]=await Promise.all([api('/categories'),api('/market/subscriptions')])}catch(e){toast(e.message);return}
 const ids=selected.map(x=>Number(x.id));
 view.innerHTML='<div class="section-head"><button class="btn ghost" id="notify-back">← Профиль</button><h2>Подписки</h2></div><section class="panel"><b>Новые задания в Telegram</b><p class="muted">Выбери до 20 категорий. Подписка на верхнюю категорию включает все её направления. Дайджест приходит раз в час, только если появились новые задания.</p><div class="muted" id="subscription-count">'+ids.length+' / 20 выбрано</div></section>'+tree.map(p=>'<section class="panel"><label style="display:flex;gap:10px;align-items:center;font-weight:800"><input type="checkbox" data-sub-category value="'+p.id+'" '+(ids.includes(Number(p.id))?'checked':'')+'> '+esc(p.name)+'</label><div style="padding-left:22px">'+p.children.map(c=>'<label style="display:flex;gap:9px;align-items:center;padding:6px 0"><input type="checkbox" data-sub-category value="'+c.id+'" '+(ids.includes(Number(c.id))?'checked':'')+'> '+esc(c.name)+'</label>').join('')+'</div></section>').join('')+'<button class="btn block" id="save-subscriptions">Сохранить подписки</button>';
 document.querySelector('#notify-back').onclick=()=>setTab('profile');
 view.querySelectorAll('[data-sub-category]').forEach(b=>b.onchange=()=>{const n=view.querySelectorAll('[data-sub-category]:checked').length;if(n>20){b.checked=false;toast('Максимум 20 подписок')}document.querySelector('#subscription-count').textContent=view.querySelectorAll('[data-sub-category]:checked').length+' / 20 выбрано'});
 document.querySelector('#save-subscriptions').onclick=async()=>{const category_ids=[...view.querySelectorAll('[data-sub-category]:checked')].map(x=>Number(x.value));try{await api('/market/subscriptions',{method:'PUT',body:JSON.stringify({category_ids})});toast('Подписки сохранены');setTab('profile')}catch(e){toast(e.message)}};
}
async function showMarketplaceModeration(){
 try{
  const [tree,requests,disputes]=await Promise.all([api('/categories?include_hidden=true'),api('/category-requests?status=pending'),api('/moderation/disputes')]);
  view.innerHTML='<div class="section-head"><button class="btn ghost" id="market-admin-back">← Профиль</button><h2>Модерация биржи</h2></div><div class="actions"><button class="btn" id="add-root-category">Добавить категорию</button><button class="btn secondary" id="add-child-category">Добавить подкатегорию</button></div><div class="section-head"><h2>Дерево категорий</h2></div>'+tree.map(p=>'<section class="panel"><div class="task-top"><div><b>'+esc(p.name)+'</b><div class="muted" style="font-size:11px">ID: '+p.id+'</div></div><span class="pill">'+(p.is_hidden?'Скрыта':'Активна')+'</span></div><div class="actions"><button class="btn secondary" data-category-edit="'+p.id+'">Переименовать</button><button class="btn secondary" data-category-hide="'+p.id+'" data-hidden="'+(p.is_hidden?'1':'0')+'">'+(p.is_hidden?'Показать':'Скрыть')+'</button></div>'+p.children.map(c=>'<div class="task-bottom" style="padding:8px 0;border-top:1px solid var(--line)"><span>'+esc(c.name)+' <small class="muted">ID: '+c.id+'</small>'+(c.is_hidden?' · скрыта':'')+'</span><span><button class="btn ghost" data-category-edit="'+c.id+'">✎</button><button class="btn ghost" data-category-move="'+c.id+'">↗</button><button class="btn ghost" data-category-hide="'+c.id+'" data-hidden="'+(c.is_hidden?'1':'0')+'">'+(c.is_hidden?'Показать':'Скрыть')+'</button></span></div>').join('')+'</section>').join('')+'<div class="section-head"><h2>Заявки на категории</h2><span>'+requests.length+'</span></div>'+(requests.length?requests.map(x=>'<section class="panel"><b>'+esc(x.text)+'</b><p class="muted">'+esc(x.task_title)+' · '+esc(x.requester_name)+'</p><div class="actions"><button class="btn" data-category-request="added" data-id="'+x.id+'">Добавить нишу</button><button class="btn secondary" data-category-request="moved" data-id="'+x.id+'">Перенести в категорию</button><button class="btn danger" data-category-request="rejected" data-id="'+x.id+'">Отклонить</button></div></section>').join(''):empty('Заявок нет','Новых запросов на категории пока нет.'))+'<div class="section-head"><h2>Жалобы по сделкам</h2><span>'+disputes.length+'</span></div>'+(disputes.length?disputes.map(x=>'<section class="panel"><b>'+esc(x.title)+'</b><p>'+esc(x.reason)+'</p><div class="muted">'+esc(x.opened_by_name)+' · '+esc(x.created_at)+'</div><div class="actions"><button class="btn secondary" data-dispute-chat="'+x.id+'">Открыть переписку</button><button class="btn" data-dispute-resolve="done" data-id="'+x.id+'">Завершить сделку</button><button class="btn secondary" data-dispute-resolve="reopen" data-id="'+x.id+'">Вернуть в работу</button><button class="btn danger" data-dispute-resolve="cancelled" data-id="'+x.id+'">Отменить сделку</button></div></section>').join(''):empty('Жалоб нет','Открытых споров пока нет.'));
  document.querySelector('#market-admin-back').onclick=()=>setTab('profile');
  document.querySelector('#add-root-category').onclick=async()=>{const name=prompt('Название новой верхней категории');if(!name)return;try{await api('/categories',{method:'POST',body:JSON.stringify({name,parent_id:null})});toast('Категория добавлена');showMarketplaceModeration()}catch(e){toast(e.message)}};
  document.querySelector('#add-child-category').onclick=async()=>{const parent=prompt('ID родительской категории (посмотри в дереве через редактирование)');const name=prompt('Название подкатегории');if(!parent||!name)return;try{await api('/categories',{method:'POST',body:JSON.stringify({name,parent_id:Number(parent)})});toast('Подкатегория добавлена');showMarketplaceModeration()}catch(e){toast(e.message)}};
  view.querySelectorAll('[data-category-edit]').forEach(b=>b.onclick=async()=>{const item=tree.flatMap(p=>[{id:p.id,name:p.name},...p.children]).find(x=>String(x.id)===b.dataset.categoryEdit);const name=prompt('Новое название',item?.name||'');if(!name)return;try{await api('/categories/'+b.dataset.categoryEdit,{method:'PATCH',body:JSON.stringify({name})});showMarketplaceModeration()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-category-hide]').forEach(b=>b.onclick=async()=>{try{await api('/categories/'+b.dataset.categoryHide,{method:'PATCH',body:JSON.stringify({is_hidden:b.dataset.hidden!=='1'})});showMarketplaceModeration()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-category-move]').forEach(b=>b.onclick=async()=>{const roots=tree.map(x=>x.id+' — '+x.name).join('\n');const parent_id=Number(prompt('ID новой родительской категории:\n'+roots));if(!parent_id)return;try{await api('/categories/'+b.dataset.categoryMove,{method:'PATCH',body:JSON.stringify({parent_id})});toast('Подкатегория перенесена');showMarketplaceModeration()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-category-request]').forEach(b=>b.onclick=async()=>{let category_id=null;if(b.dataset.categoryRequest!=='rejected'){const flat=tree.flatMap(p=>p.children.map(c=>({id:c.id,label:p.name+' / '+c.name})));const choices=flat.map(x=>x.id+' — '+x.label).join('\n');const chosen=prompt('ID целевой подкатегории:\n'+choices);if(!chosen)return;category_id=Number(chosen)}const note=prompt('Комментарий заявителю (необязательно)','')||'';try{await api('/category-requests/'+b.dataset.id+'/resolve',{method:'POST',body:JSON.stringify({status:b.dataset.categoryRequest,category_id,note})});toast('Заявка обработана');showMarketplaceModeration()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-dispute-chat]').forEach(b=>b.onclick=()=>showModeratorDisputeChat(b.dataset.disputeChat));
  view.querySelectorAll('[data-dispute-resolve]').forEach(b=>b.onclick=async()=>{const comment=prompt('Комментарий модератора (необязательно)','')||'';if(!confirm('Применить решение к сделке?'))return;try{await api('/moderation/disputes/'+b.dataset.id+'/resolve',{method:'POST',body:JSON.stringify({decision:b.dataset.disputeResolve,comment})});toast('Решение сохранено');showMarketplaceModeration()}catch(e){toast(e.message)}});
 }catch(e){toast(e.message)}
}
async function showModeratorDisputeChat(id){
 try{const rows=await api('/moderation/disputes/'+id+'/chat');view.innerHTML='<div class="section-head"><button class="btn ghost" id="dispute-back">← Модерация</button><h2>Переписка по жалобе</h2></div>'+(rows.length?rows.map(x=>'<section class="panel"><b>'+esc(x.sender_name)+'</b><p>'+esc(x.text)+'</p><small class="muted">'+esc(x.created_at)+'</small></section>').join(''):empty('Сообщений нет','Переписка отсутствует.'));document.querySelector('#dispute-back').onclick=showMarketplaceModeration}catch(e){toast(e.message)}
}

async function showMyMarketResponses(){
 try{const rows=await api('/market/responses/mine');view.innerHTML='<div class="section-head"><button class="btn ghost" id="responses-back">← Профиль</button><h2>Мои отклики</h2><span>'+rows.length+'</span></div>'+(rows.length?rows.map(r=>{let action='';if(r.status==='sent')action='<button class="btn secondary" data-withdraw-response="'+r.id+'">Отозвать отклик</button>';if(r.status==='accepted'&&r.task_status==='in_progress')action='<button class="btn danger" data-release-response="'+r.id+'">Отказаться от сделки</button>';return '<article class="panel"><div class="task-top"><b>'+esc(r.title)+'</b><span class="pill">'+esc(r.status)+'</span></div><p>'+esc(r.message||'Без сопроводительного сообщения')+'</p><div class="muted">'+esc(r.category)+' · '+rub(r.reward_kop)+'</div>'+(action?'<div class="actions">'+action+'</div>':'')+'</article>'}).join(''):empty('Откликов пока нет','Твои отклики появятся здесь.'));document.querySelector('#responses-back').onclick=()=>setTab('profile');view.querySelectorAll('[data-withdraw-response]').forEach(b=>b.onclick=async()=>{try{await api('/market/responses/'+b.dataset.withdrawResponse+'/withdraw',{method:'POST'});toast('Отклик отозван');showMyMarketResponses()}catch(e){toast(e.message)}});view.querySelectorAll('[data-release-response]').forEach(b=>b.onclick=async()=>{if(!confirm('Отказаться от текущей сделки? Задание снова откроется для откликов.'))return;try{await api('/market/responses/'+b.dataset.releaseResponse+'/release',{method:'POST'});toast('Ты отказался без штрафа');showMyMarketResponses()}catch(e){toast(e.message)}})}catch(e){toast(e.message)}
}

async function showMarketChats(){
 try{const rows=await api('/market/chats');view.innerHTML='<div class="section-head"><button class="btn ghost" id="chats-back">← Профиль</button><h2>Мои диалоги</h2></div>'+(rows.length?rows.map(c=>'<button class="panel block" style="text-align:left;margin-bottom:10px" data-chat-id="'+c.chat_id+'" data-chat-title="'+esc(c.title)+'"><b>'+esc(c.title)+'</b><div class="muted">'+(Number(c.created_by)===Number(me.user.id)?'Ты заказчик':'Ты исполнитель')+' · '+esc(c.created_at)+'</div></button>').join(''):empty('Диалогов пока нет','Чат появится после того, как заказчик примет отклик.'));document.querySelector('#chats-back').onclick=()=>setTab('profile');view.querySelectorAll('[data-chat-id]').forEach(b=>b.onclick=()=>showDealChat(b.dataset.chatId,b.dataset.chatTitle))}catch(e){toast(e.message)}
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
document.querySelector('#theme-toggle')?.addEventListener('click',openThemePicker);
boot();
