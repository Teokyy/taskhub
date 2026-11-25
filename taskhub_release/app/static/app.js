const tg=window.Telegram?.WebApp;
tg?.ready();tg?.expand();
const initData=tg?.initData||'';
const headers={'Content-Type':'application/json','X-Telegram-Init-Data':initData};
const view=document.querySelector('#view');
let me=null,tab='exchange',toastTimer,rooms=[],activeRoom=null,roomBudget=null,renderSequence=0;
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
async function boot(){
 try{
  me=await api('/me');
  document.querySelector('#avatar').innerHTML=me.user.photo_url?'<img src="'+esc(me.user.photo_url)+'" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:14px">':esc((me.user.first_name||'T').slice(0,1).toUpperCase());
  await loadRooms();
  renderRoomSelector();
  await refreshMe();
  startPresenceHeartbeat();
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
 const marketTask=t.room_id===null||t.room_id===undefined,own=Number(t.created_by)===Number(me.user.id),executor=Number(t.assignee_id)===Number(me.user.id);
 let action='';
 if(marketTask&&['open','available'].includes(t.status)){
  if(!own)action='<button class="btn" data-market-response="'+t.id+'">Откликнуться</button>';
  else action='<button class="btn secondary" data-view-responses="'+t.id+'">Отклики</button><button class="btn secondary" data-edit-market="'+t.id+'">Редактировать</button><button class="btn danger" data-delete-market="'+t.id+'">Снять с публикации</button>';
 }
 if(marketTask&&t.status==='in_progress'){
  if(own)action='<button class="btn" data-complete-market="'+t.id+'">Подтвердить выполнение</button>';
  if(executor)action='<button class="btn secondary" data-open-deal-chat="'+t.id+'">Открыть чат</button>';
  if(own||executor)action+='<button class="btn danger" data-dispute-market="'+t.id+'">Пожаловаться</button>';
 }
 if(marketTask&&t.status==='done'&&(own||executor))action='<button class="btn secondary" data-review-task="'+t.id+'">Оставить отзыв</button>';
 if(!marketTask&&t.status==='available'&&workMode()==='executor'&&!own)action='<button class="btn" data-claim="'+t.id+'">Взять задачу</button>';
 if(['in_progress','revision'].includes(t.status)&&executor&&!marketTask)action='<button class="btn" data-submit="'+t.id+'" data-market="0">Сдать работу</button>';
 const statuses={open:'Открыта',available:'Открыта',in_progress:'В работе',submitted:'На проверке',revision:'Нужна доработка',accepted:'Принята',done:'Завершена',disputed:'Спор',rejected:'Отклонена',cancelled:'Отменена',not_completed:'Не выполнена'};
 return '<article class="panel task"><div class="task-top"><span class="pill">'+esc(t.category||'Другое')+'</span><span class="price">'+rub(t.reward_kop)+'</span></div><h3>'+esc(t.title)+'</h3><p>'+esc(t.description||'Без дополнительного описания')+'</p><div class="task-bottom"><span class="status">'+(statuses[t.status]||esc(t.status))+'</span><span class="deadline">'+(t.deadline?'До '+esc(t.deadline):'Без срока')+'</span></div>'+(adminView&&t.assignee_name?'<div class="divider"></div><span class="muted">Исполнитель: '+esc(t.assignee_name)+'</span>':'')+(action?'<div class="actions">'+action+'</div>':'')+'</article>'
}
function bindTaskActions(){
 view.querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>claim(b.dataset.claim));
 view.querySelectorAll('[data-market-claim]').forEach(b=>b.onclick=()=>claimMarketTask(b.dataset.marketClaim));
 view.querySelectorAll('[data-submit]').forEach(b=>b.onclick=()=>submitForm(b.dataset.submit,b.dataset.market==='1'));
 view.querySelectorAll('[data-market-review]').forEach(b=>b.onclick=()=>reviewMarketTask(b.dataset.id,b.dataset.marketReview));
 view.querySelectorAll('[data-edit-market]').forEach(b=>b.onclick=()=>{const task=(me.tasks||[]).find(t=>String(t.id)===b.dataset.editMarket)||(window.marketTaskCache||[]).find(t=>String(t.id)===b.dataset.editMarket);if(task)createForm('market',task);else api('/market/tasks?status=mine').then(rows=>{const t=rows.find(x=>String(x.id)===b.dataset.editMarket);if(t)createForm('market',t);else toast('Задание не найдено')}).catch(e=>toast(e.message))});
 view.querySelectorAll('[data-delete-market]').forEach(b=>b.onclick=()=>deleteMarketTask(b.dataset.deleteMarket));
 view.querySelectorAll('[data-market-response]').forEach(b=>b.onclick=()=>createMarketResponse(b.dataset.marketResponse));
 view.querySelectorAll('[data-view-responses]').forEach(b=>b.onclick=()=>showMarketResponses(b.dataset.viewResponses));
 view.querySelectorAll('[data-complete-market]').forEach(b=>b.onclick=()=>completeMarketTask(b.dataset.completeMarket));
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
async function completeMarketTask(id){if(!confirm('Подтвердить выполнение задания?'))return;try{await api('/market/tasks/'+id+'/complete',{method:'POST'});toast('Сделка завершена');await render()}catch(e){toast(e.message)}}
async function createMarketDispute(id){const reason=prompt('Опиши проблему по сделке');if(!reason)return;try{await api('/market/tasks/'+id+'/disputes',{method:'POST',body:JSON.stringify({reason})});toast('Жалоба отправлена модератору');await render()}catch(e){toast(e.message)}}
async function createMarketReview(id){const rating=Number(prompt('Оценка от 1 до 5','5'));if(!Number.isInteger(rating)||rating<1||rating>5)return toast('Оценка должна быть от 1 до 5');const text=prompt('Короткий отзыв (необязательно)','')||'';try{await api('/market/tasks/'+id+'/reviews',{method:'POST',body:JSON.stringify({rating,text})});toast('Отзыв сохранён')}catch(e){toast(e.message)}}
async function showDealChatByTask(taskId){const rows=await api('/market/chats');const chat=rows.find(x=>String(x.task_id)===String(taskId));if(chat)showDealChat(chat.chat_id,chat.title);else toast('Чат сделки не найден')}
async function showDealChat(chatId,title){
 let lastId=0;view.innerHTML='<div class="section-head"><button class="btn ghost" id="chat-back">← Профиль</button><h2>'+esc(title||'Чат сделки')+'</h2></div><div id="deal-messages" class="panel" style="min-height:220px"></div><textarea class="field" id="deal-message" maxlength="2000" placeholder="Напиши сообщение…"></textarea><button class="btn block" id="send-deal-message">Отправить</button>';
 document.querySelector('#chat-back').onclick=()=>setTab('profile');
 const load=async()=>{try{const rows=await api('/market/chats/'+chatId+'/messages?after_id='+lastId);const box=document.querySelector('#deal-messages');if(!box)return;rows.forEach(x=>{lastId=Math.max(lastId,x.id);box.insertAdjacentHTML('beforeend','<div style="padding:8px 0;border-bottom:1px solid var(--line)"><b>'+esc(x.sender_name)+'</b><p style="margin:4px 0">'+esc(x.text)+'</p><small class="muted">'+esc(x.created_at)+'</small></div>')})}catch(e){toast(e.message)}};
 await load();const timer=setInterval(()=>{if(document.querySelector('#deal-messages'))load();else clearInterval(timer)},4000);
 document.querySelector('#send-deal-message').onclick=async()=>{const el=document.querySelector('#deal-message'),text=el.value.trim();if(!text)return;try{const r=await api('/market/chats/'+chatId+'/messages',{method:'POST',body:JSON.stringify({body:text})});el.value='';if(r.warning_external_contact)toast('Напоминание: не передавай личные контакты незнакомым пользователям');await load()}catch(e){toast(e.message)}};
}

async function showMarketServices(){
 let services=[];try{services=await api('/market/services')}catch(e){toast(e.message);return}
 view.innerHTML='<section class="hello"><div class="eyebrow">ПОСТОЯННЫЕ ПРЕДЛОЖЕНИЯ</div><h1>Услуги</h1><p>Многоразовые предложения: пополнение, игровые услуги, настройка Discord и другие работы. Заказы создаются отдельно каждый раз.</p></section><div class="actions"><button class="btn" id="new-market-service">Разместить услугу</button><button class="btn secondary" id="my-service-orders">Мои заказы</button></div><div id="market-service-list">'+(services.length?services.map(x=>'<article class="panel task"><div class="task-top"><span class="pill">'+esc(x.category)+'</span><span class="price">'+rub(x.price_kop)+'</span></div><h3>'+esc(x.title)+'</h3><p>'+esc(x.description||'Без описания')+'</p><div class="task-bottom"><span class="muted">Продавец: '+esc(x.seller_name||'Участник')+'</span><span class="muted">Срок: '+Math.ceil(x.delivery_minutes/60)+' ч.</span></div>'+(Number(x.seller_id)===Number(me.user.id)?'<span class="muted">Твоя услуга</span>':'<button class="btn block" data-order-service="'+x.id+'">Заказать</button>')+'</article>').join(''):empty('Услуг пока нет','Размести первое постоянное предложение.'))+'</div>';
 document.querySelector('#new-market-service').onclick=createMarketServiceForm;document.querySelector('#my-service-orders').onclick=showMarketServiceOrders;
 view.querySelectorAll('[data-order-service]').forEach(b=>b.onclick=async()=>{const details=prompt('Уточни детали заказа: игровой ID, сервер, нужная сумма пополнения и т.п.','');if(details===null)return;try{await api('/market/services/'+b.dataset.orderService+'/orders',{method:'POST',body:JSON.stringify({details})});toast('Заказ отправлен продавцу');showMarketServices()}catch(e){toast(e.message)}});
}
function createMarketServiceForm(){
 document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="servicebox"><section class="modal-sheet"><div class="eyebrow">REPEATABLE SERVICE</div><h2>Новая услуга</h2><label>Название</label><input class="field" id="service-title" maxlength="120" placeholder="Например, пополнение Steam"><label>Описание и условия</label><textarea class="field" id="service-description" placeholder="Что входит в услугу? Какие данные нужны от заказчика?"></textarea><label>Категория</label><select class="field" id="service-category">'+MARKET_CATEGORY_OPTIONS.map(c=>'<option>'+esc(c)+'</option>').join('')+'</select><label>Цена за один заказ, ₽</label><input class="field" id="service-price" type="number" min="1" step="1" placeholder="500"><label>Срок выполнения, минут</label><input class="field" id="service-delivery" type="number" min="1" value="60"><p class="muted">Оплата пока проходит напрямую между заказчиком и исполнителем. TaskHub фиксирует заказ и его статус, но не принимает деньги и не удерживает средства.</p><div class="actions"><button class="btn" id="service-publish">Опубликовать услугу</button><button class="btn secondary" id="service-cancel">Отмена</button></div></section></div>');
 document.querySelector('#service-cancel').onclick=()=>document.querySelector('#servicebox').remove();document.querySelector('#servicebox').onclick=e=>{if(e.target.id==='servicebox')e.target.remove()};
 document.querySelector('#service-publish').onclick=async()=>{const title=document.querySelector('#service-title').value.trim(),price=Number(document.querySelector('#service-price').value);if(title.length<2||price<1)return toast('Заполни название и цену');const b=document.querySelector('#service-publish');b.disabled=true;try{await api('/market/services',{method:'POST',body:JSON.stringify({title,description:document.querySelector('#service-description').value,category:document.querySelector('#service-category').value,price_rub:price,delivery_minutes:Number(document.querySelector('#service-delivery').value||60)})});document.querySelector('#servicebox').remove();toast('Услуга опубликована');showMarketServices()}catch(e){toast(e.message);b.disabled=false}};
}
async function showMarketServiceOrders(){
 try{const rows=await api('/market/service-orders');const statuses={created:'Новый заказ',accepted:'Принят продавцом',delivered:'Ожидает подтверждения',completed:'Завершён',cancelled:'Отменён'};view.innerHTML='<div class="section-head"><button class="btn ghost" id="orders-back">← Услуги</button><h2>Заказы услуг</h2><span>'+rows.length+'</span></div>'+(rows.length?rows.map(o=>{const seller=Number(o.seller_id)===Number(me.user.id),buyer=Number(o.buyer_id)===Number(me.user.id);let actions='';if(seller&&o.status==='created')actions='<button class="btn" data-order-status="accepted" data-id="'+o.id+'">Принять заказ</button>';if(seller&&o.status==='accepted')actions='<button class="btn" data-order-status="delivered" data-id="'+o.id+'">Отметить выполненным</button>';if(buyer&&o.status==='delivered')actions='<button class="btn" data-order-status="completed" data-id="'+o.id+'">Подтвердить выполнение</button>';if(buyer&&o.status==='created')actions='<button class="btn danger" data-order-status="cancelled" data-id="'+o.id+'">Отменить</button>';return '<article class="panel"><div class="task-top"><b>'+esc(o.service_title)+'</b><span class="pill">'+(statuses[o.status]||esc(o.status))+'</span></div><p>'+esc(o.details||'Без дополнительных деталей')+'</p><div class="task-bottom"><span class="muted">'+(seller?'Покупатель: '+esc(o.buyer_name):'Продавец: '+esc(o.seller_name))+'</span><b>'+rub(o.price_kop)+'</b></div>'+(actions?'<div class="actions">'+actions+'</div>':'')+'</article>'}).join(''):empty('Заказов пока нет','Здесь появятся твои покупки и заказы от клиентов.'));document.querySelector('#orders-back').onclick=showMarketServices;view.querySelectorAll('[data-order-status]').forEach(b=>b.onclick=async()=>{try{await api('/market/service-orders/'+b.dataset.id,{method:'PATCH',body:JSON.stringify({status:b.dataset.orderStatus})});toast('Статус заказа обновлён');showMarketServiceOrders()}catch(e){toast(e.message)}})}catch(e){toast(e.message)}
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
  let list,categoryTree;try{[list,categoryTree]=await Promise.all([api('/market/tasks'),api('/categories')])}catch(e){if(renderToken===renderSequence)view.innerHTML=empty('Не удалось загрузить биржу',e.message,'<button class="btn secondary" onclick="render()">Повторить</button>');return}
  if(renderToken!==renderSequence)return;
  const categories=categoryTree.map(c=>c.name);
  view.innerHTML='<section class="hello"><div class="eyebrow">ОБЩАЯ ПЛОЩАДКА</div><h1>Открытые задания</h1><p>Частные задания от заказчиков и компаний.</p></section><div class="filter-row"><input class="field" id="task-search" style="margin:0;min-width:140px;flex:1" placeholder="Поиск по задачам"><button class="filter-chip active" data-filter="all">Все</button>'+categories.map(c=>'<button class="filter-chip" data-filter="'+esc(c)+'">'+esc(c)+'</button>').join('')+'</div><div class="filter-row"><select class="field" id="deadline-filter" style="margin:0;min-width:150px;max-width:210px"><option value="all">Любой срок</option><option value="dated">Со сроком</option><option value="none">Без срока</option><option value="overdue">Просроченные</option></select></div><div id="task-list"></div>'+'<button class="btn block" id="new-task">Разместить задание</button>';
  let selected='all';
  const draw=()=>{const q=(document.querySelector('#task-search')?.value||'').toLocaleLowerCase('ru');const deadlineMode=document.querySelector('#deadline-filter')?.value||'all';const today=new Date();today.setHours(0,0,0,0);const filtered=list.filter(t=>{const hasDeadline=!!t.deadline;const deadline=t.deadline?new Date(t.deadline):null;const overdue=deadline&&!Number.isNaN(deadline.getTime())&&deadline<today&&!['accepted','cancelled','rejected','not_completed'].includes(t.status);return(selected==='all'||(t.category||'Другое')===selected||(t.category||'').startsWith(selected+' / '))&&(!q||(t.title+' '+(t.description||'')).toLocaleLowerCase('ru').includes(q))&&(deadlineMode==='all'||(deadlineMode==='dated'&&hasDeadline)||(deadlineMode==='none'&&!hasDeadline)||(deadlineMode==='overdue'&&overdue))});document.querySelector('#task-list').innerHTML=filtered.length?filtered.map(t=>taskCard(t)).join(''):empty('Задачи не найдены','Измени запрос или выбери другой срок.');bindTaskActions()};
  view.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{selected=b.dataset.filter;view.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));draw()});
  document.querySelector('#task-search').oninput=draw;document.querySelector('#deadline-filter').onchange=draw;document.querySelector('#new-task')?.addEventListener('click',createForm);draw();bindTaskActions();

 }else if(tab==='profile'){
  view.innerHTML='<section class="hello"><h1>Профиль</h1><p>Единый аккаунт TaskHub — не зависит от выбранной комнаты.</p></section><div class="panel muted">Загружаем профиль…</div>';
  let profile;try{profile=await api('/market/profile')}catch(e){if(renderToken===renderSequence)view.innerHTML=empty('Не удалось загрузить профиль',e.message,'<button class="btn secondary" onclick="render()">Повторить</button>');return}
  if(renderToken!==renderSequence)return;
  me=profile;const mine=(profile.tasks||[]).filter(t=>t.room_id===null||t.room_id===undefined);
  const counts={in_progress:0,submitted:0,revision:0,accepted:0,rejected:0,not_completed:0,...(profile.counts||{})};
  view.innerHTML='<section class="profile-row" style="margin:8px 2px 18px"><div class="big-avatar">'+esc((me.user.first_name||'T').slice(0,1).toUpperCase())+'</div><div style="min-width:0;flex:1"><div class="profile-name">'+esc(me.user.first_name||'Участник')+'</div><div class="muted">Пользователь TaskHub</div><div class="muted" style="font-size:12px;margin-top:3px">★ '+Number(profile.rating_average||0).toFixed(1)+' · '+(profile.review_count||0)+' отзывов</div></div></section><section class="panel balance-card"><div class="task-top"><div><div class="eyebrow">МОЙ БАЛАНС</div><div style="font-size:25px;font-weight:850;margin-top:4px">'+rub(me.balance_kop||0)+'</div></div><button class="btn secondary" id="profile-finance">История</button></div></section><div class="section-head"><h2>Мои задачи</h2><span>'+mine.filter(t=>t.assignee_id===me.user.id).length+' ВСЕГО</span></div><div class="stats"><div class="stat"><small>В работе</small><strong>'+counts.in_progress+'</strong></div><div class="stat"><small>На проверке</small><strong>'+counts.submitted+'</strong></div><div class="stat"><small>На доработке</small><strong>'+counts.revision+'</strong></div><div class="stat"><small>Принято</small><strong>'+counts.accepted+'</strong></div></div><div class="section-head"><h2>Мои задания как заказчика</h2><span>'+mine.filter(t=>t.created_by===me.user.id).length+'</span></div><div id="my-market-tasks">'+(mine.filter(t=>t.created_by===me.user.id).length?mine.filter(t=>t.created_by===me.user.id).map(t=>taskCard(t)).join(''):empty('Ты ещё не размещал задания','Опубликуй первое задание на общей бирже.'))+'</div><div class="section-head"><h2>Команды</h2></div><section class="panel"><p class="muted">'+(rooms.length?'Состоишь в '+rooms.length+' комнатах. Командные настройки доступны отдельно.':'Ты не состоишь в командах. Это никак не ограничивает биржу.')+'</p><button class="btn secondary block" id="profile-rooms">Открыть комнаты</button></section>';
  document.querySelector('#profile-finance').onclick=showFinanceScreen;
  const responseButton=document.createElement('button');responseButton.className='btn secondary block';responseButton.textContent='Мои отклики';responseButton.onclick=showMyMarketResponses;document.querySelector('#profile-finance')?.parentElement?.after(responseButton);
  const chatButton=document.createElement('button');chatButton.className='btn secondary block';chatButton.textContent='Мои диалоги';chatButton.onclick=showMarketChats;document.querySelector('#profile-finance')?.parentElement?.after(chatButton);
  document.querySelector('#profile-rooms').onclick=()=>setTab('room');
  const profileSettings=document.createElement('div');profileSettings.innerHTML=renderThemeSettings();document.querySelector('#profile-rooms')?.parentElement?.after(profileSettings);
  profileSettings.querySelectorAll('[data-theme-choice]').forEach(b=>b.onclick=()=>applyTaskhubTheme(b.dataset.themeChoice));
  const notifyButton=document.createElement('button');notifyButton.className='btn secondary block';notifyButton.textContent='Уведомления о новых заданиях';notifyButton.onclick=showMarketNotificationSettings;document.querySelector('#profile-rooms')?.parentElement?.appendChild(notifyButton);
  bindTaskActions();
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
 const editing=!!existing,market=scope==='market';let tree=[];
 if(market){try{tree=await api('/categories')}catch(e){toast('Не удалось загрузить категории: '+e.message);return}}
 const opts=market?tree.map(p=>'<optgroup label="'+esc(p.name)+'">'+p.children.map(c=>'<option value="'+c.id+'" '+(Number(existing?.category_id)===Number(c.id)?'selected':'')+'>'+esc(c.name)+'</option>').join('')+'</optgroup>').join(''):[...MARKET_CATEGORY_OPTIONS].map(c=>'<option value="'+esc(c)+'">'+esc(c)+'</option>').join('');
 document.body.insertAdjacentHTML('beforeend','<div class="modal-backdrop" id="createbox"><section class="modal-sheet"><div class="eyebrow">НОВОЕ ЗАДАНИЕ</div><h2>'+(editing?'Редактировать задание':'Публикация задания')+'</h2><label>Название</label><input class="field" id="title" maxlength="120" placeholder="Что нужно сделать?"><label>Описание</label><textarea class="field" id="description" placeholder="Опиши задачу и ожидаемый результат"></textarea><label>Категория и направление</label><select class="field" id="category">'+opts+'</select><div id="other-category-wrap" hidden><label>Что за услуга? (до 200 символов)</label><input class="field" id="other-category-text" maxlength="200" placeholder="Опиши свою нишу"></div><label>Оплата, ₽</label><input class="field" id="reward" type="number" min="1" step="1" placeholder="Сумма, о которой договоритесь напрямую"><label>Срок выполнения</label><input class="field" id="deadline" type="date"><p class="muted">TaskHub не принимает оплату. Заказчик и исполнитель рассчитываются напрямую.</p><div class="actions"><button class="btn" id="create">'+(editing?'Сохранить':'Опубликовать')+'</button><button class="btn secondary" id="cancel-create">Отмена</button></div></section></div>');
 const set=(id,value)=>{const el=document.querySelector('#'+id);if(el&&value!==undefined&&value!==null)el.value=value};
 set('title',existing?.title||'');set('description',existing?.description||'');set('reward',Math.round((existing?.reward_kop||0)/100));set('deadline',existing?.deadline||'');
 const category=document.querySelector('#category'),otherWrap=document.querySelector('#other-category-wrap');
 const syncOther=()=>{const selected=tree.flatMap(p=>p.children.map(c=>({id:c.id,label:p.name+' / '+c.name}))).find(c=>String(c.id)===category.value);otherWrap.hidden=!(selected?.label==='Другое / Прочее')};
 if(market&&existing?.category_id)set('category',String(existing.category_id));syncOther();category.onchange=syncOther;
 document.querySelector('#cancel-create').onclick=()=>document.querySelector('#createbox').remove();document.querySelector('#createbox').onclick=e=>{if(e.target.id==='createbox')e.target.remove()};
 document.querySelector('#create').onclick=async()=>{const title=document.querySelector('#title').value.trim(),reward=Number(document.querySelector('#reward').value||0);if(title.length<2)return toast('Добавь название задания');if(reward<=0)return toast('Укажи сумму вознаграждения');const selected=tree.flatMap(p=>p.children.map(c=>({id:c.id,label:p.name+' / '+c.name}))).find(c=>String(c.id)===category.value);const payload={title,description:document.querySelector('#description').value,category_id:market?Number(category.value):null,category:market?(selected?.label||'Другое'):category.value,other_category_text:document.querySelector('#other-category-text')?.value||'',reward_rub:reward,pricing_model:'fixed',deadline:document.querySelector('#deadline').value||null};if(market&&selected?.label==='Другое / Прочее'&&!payload.other_category_text.trim())return toast('Опиши, какая услуга нужна');const button=document.querySelector('#create');button.disabled=true;button.textContent=editing?'Сохраняем…':'Публикуем…';try{const url=market?'/market/tasks'+(editing?'/'+existing.id:''):'/rooms/'+activeRoom.id+'/tasks';await api(url,{method:editing?'PUT':'POST',body:JSON.stringify(payload)});document.querySelector('#createbox')?.remove();toast(editing?'Изменения сохранены':scope==='room'?'Задача добавлена в комнату':'Задание опубликовано');await refreshMe();if(scope==='room')showRoomTasks();else setTab('exchange')}catch(e){toast(e.message);button.disabled=false;button.textContent=editing?'Сохранить':'Опубликовать'}};
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
  view.innerHTML='<div class="section-head"><button class="btn ghost" id="market-admin-back">← Профиль</button><h2>Модерация биржи</h2></div><div class="actions"><button class="btn" id="add-root-category">Добавить категорию</button><button class="btn secondary" id="add-child-category">Добавить подкатегорию</button></div><div class="section-head"><h2>Дерево категорий</h2></div>'+tree.map(p=>'<section class="panel"><div class="task-top"><b>'+esc(p.name)+'</b><span class="pill">'+(p.is_hidden?'Скрыта':'Активна')+'</span></div><div class="actions"><button class="btn secondary" data-category-edit="'+p.id+'">Переименовать</button><button class="btn secondary" data-category-hide="'+p.id+'" data-hidden="'+(p.is_hidden?'1':'0')+'">'+(p.is_hidden?'Показать':'Скрыть')+'</button></div>'+p.children.map(c=>'<div class="task-bottom" style="padding:8px 0;border-top:1px solid var(--line)"><span>'+esc(c.name)+(c.is_hidden?' · скрыта':'')+'</span><span><button class="btn ghost" data-category-edit="'+c.id+'">✎</button><button class="btn ghost" data-category-hide="'+c.id+'" data-hidden="'+(c.is_hidden?'1':'0')+'">'+(c.is_hidden?'Показать':'Скрыть')+'</button></span></div>').join('')+'</section>').join('')+'<div class="section-head"><h2>Заявки на категории</h2><span>'+requests.length+'</span></div>'+(requests.length?requests.map(x=>'<section class="panel"><b>'+esc(x.text)+'</b><p class="muted">'+esc(x.task_title)+' · '+esc(x.requester_name)+'</p><div class="actions"><button class="btn" data-category-request="added" data-id="'+x.id+'">Добавить нишу</button><button class="btn secondary" data-category-request="moved" data-id="'+x.id+'">Перенести в категорию</button><button class="btn danger" data-category-request="rejected" data-id="'+x.id+'">Отклонить</button></div></section>').join(''):empty('Заявок нет','Новых запросов на категории пока нет.'))+'<div class="section-head"><h2>Жалобы по сделкам</h2><span>'+disputes.length+'</span></div>'+(disputes.length?disputes.map(x=>'<section class="panel"><b>'+esc(x.title)+'</b><p>'+esc(x.reason)+'</p><div class="muted">'+esc(x.opened_by_name)+' · '+esc(x.created_at)+'</div><button class="btn secondary" data-dispute-chat="'+x.id+'">Открыть переписку</button></section>').join(''):empty('Жалоб нет','Открытых споров пока нет.'));
  document.querySelector('#market-admin-back').onclick=()=>setTab('profile');
  document.querySelector('#add-root-category').onclick=async()=>{const name=prompt('Название новой верхней категории');if(!name)return;try{await api('/categories',{method:'POST',body:JSON.stringify({name,parent_id:null})});toast('Категория добавлена');showMarketplaceModeration()}catch(e){toast(e.message)}};
  document.querySelector('#add-child-category').onclick=async()=>{const parent=prompt('ID родительской категории (посмотри в дереве через редактирование)');const name=prompt('Название подкатегории');if(!parent||!name)return;try{await api('/categories',{method:'POST',body:JSON.stringify({name,parent_id:Number(parent)})});toast('Подкатегория добавлена');showMarketplaceModeration()}catch(e){toast(e.message)}};
  view.querySelectorAll('[data-category-edit]').forEach(b=>b.onclick=async()=>{const item=tree.flatMap(p=>[{id:p.id,name:p.name},...p.children]).find(x=>String(x.id)===b.dataset.categoryEdit);const name=prompt('Новое название',item?.name||'');if(!name)return;try{await api('/categories/'+b.dataset.categoryEdit,{method:'PATCH',body:JSON.stringify({name})});showMarketplaceModeration()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-category-hide]').forEach(b=>b.onclick=async()=>{try{await api('/categories/'+b.dataset.categoryHide,{method:'PATCH',body:JSON.stringify({is_hidden:b.dataset.hidden!=='1'})});showMarketplaceModeration()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-category-request]').forEach(b=>b.onclick=async()=>{let category_id=null;if(b.dataset.categoryRequest!=='rejected'){const flat=tree.flatMap(p=>p.children.map(c=>({id:c.id,label:p.name+' / '+c.name})));const choices=flat.map(x=>x.id+' — '+x.label).join('\n');const chosen=prompt('ID целевой подкатегории:\n'+choices);if(!chosen)return;category_id=Number(chosen)}const note=prompt('Комментарий заявителю (необязательно)','')||'';try{await api('/category-requests/'+b.dataset.id+'/resolve',{method:'POST',body:JSON.stringify({status:b.dataset.categoryRequest,category_id,note})});toast('Заявка обработана');showMarketplaceModeration()}catch(e){toast(e.message)}});
  view.querySelectorAll('[data-dispute-chat]').forEach(b=>b.onclick=()=>showModeratorDisputeChat(b.dataset.disputeChat));
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
