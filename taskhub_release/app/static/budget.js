async function showBudgetSettings(){if(!isAdmin())return toast('Нет прав');try{roomBudget=await api('/rooms/'+activeRoom.id+'/budget');const v=roomBudget.limit_kop===null?'':roomBudget.limit_kop/100;view.innerHTML='<div class="section-head"><h2>Бюджет комнаты</h2></div><section class="hero"><div class="eyebrow">ДОСТУПНО</div><div class="amount">'+(roomBudget.available_kop===null?'∞':rub(roomBudget.available_kop))+'</div><div class="hero-foot">Резерв: '+rub(roomBudget.reserved_kop)+'</div></section><div class="panel"><label>Лимит, ₽</label><input class="field" id="budget-limit" type="number" min="0" step="1" value="'+v+'" placeholder="Пусто — без лимита"><div class="actions"><button class="btn" id="budget-save">Сохранить</button><button class="btn secondary" id="budget-none">Без лимита</button></div></div><button class="btn secondary" id="budget-back">Назад</button>';document.querySelector('#budget-save').onclick=()=>saveRoomBudget(false);document.querySelector('#budget-none').onclick=()=>saveRoomBudget(true);document.querySelector('#budget-back').onclick=()=>setTab('home')}catch(e){toast(e.message)}}
async function saveRoomBudget(clear){const s=document.querySelector('#budget-limit')?.value.trim()||'';if(!clear&&s!==''&&(!Number.isSafeInteger(Number(s))||Number(s)<0))return toast('Нужна целая сумма в рублях');try{roomBudget=await api('/rooms/'+activeRoom.id+'/budget',{method:'PUT',body:JSON.stringify({limit_rub:clear||s===''?null:Number(s)})});toast('Бюджет обновлён');showBudgetSettings()}catch(e){toast(e.message)}}

function installBudgetShortcut(){
 const host=document.querySelector('#room-switcher');
 if(!host||!activeRoom)return;
 host.querySelector('#budget-shortcut')?.remove();
 if(!isAdmin())return;
 const b=document.createElement('button');b.id='budget-shortcut';b.type='button';b.textContent='₽';b.title='Бюджет комнаты';
 b.style.cssText='margin-left:6px;border:1px solid var(--line);background:var(--panel);color:var(--lime);border-radius:11px;padding:8px 11px;font-weight:800';
 b.onclick=showBudgetSettings;host.appendChild(b);
}
new MutationObserver(installBudgetShortcut).observe(document.querySelector('#room-switcher'),{childList:true});
installBudgetShortcut();
