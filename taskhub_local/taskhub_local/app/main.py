from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from os import getenv
from dotenv import load_dotenv
from .db import init_db, connect
from .auth import verify_telegram_init_data
import asyncio
load_dotenv(); ROOT=Path(__file__).resolve().parent
app=FastAPI(title="TaskHub"); app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")
_bot_task=None
class TaskIn(BaseModel): title:str=Field(min_length=2,max_length=120); description:str=""; category:str="Другое"; reward_rub:int=Field(ge=0,le=1000000); deadline:str|None=None
class SubmitIn(BaseModel): body:str=Field(min_length=1,max_length=10000)
class ReviewIn(BaseModel): decision:str; comment:str=""
def current_user(init_data):
 token=getenv("BOT_TOKEN",""); u=verify_telegram_init_data(init_data,token)
 if not u and getenv("DEV_MODE","false").lower()=="true": u={"id":999000,"first_name":"Разработчик","username":"dev"}
 if not u: raise HTTPException(401,"Откройте TaskHub через Telegram")
 admins={int(x) for x in getenv("ADMIN_TELEGRAM_IDS","").split(",") if x.strip().isdigit()}
 role="admin" if int(u["id"]) in admins or int(u["id"])==999000 else "helper"
 with connect() as c:
  c.execute("INSERT INTO users(telegram_id,username,first_name,role) VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name",(int(u["id"]),u.get("username"),u.get("first_name", ""),role))
  return dict(c.execute("SELECT * FROM users WHERE telegram_id=?",(int(u["id"]),)).fetchone())
def user(x): return current_user(x)
def admin(u):
 if u["role"]!="admin": raise HTTPException(403,"Только для администратора")
@app.on_event("startup")
async def startup():
 global _bot_task; init_db()
 if getenv("BOT_TOKEN") and getenv("PUBLIC_URL","").startswith("https://"):
  from .bot import run_bot
  _bot_task=asyncio.create_task(run_bot())
@app.on_event("shutdown")
async def shutdown():
 if _bot_task: _bot_task.cancel()
@app.get("/")
def home(): return FileResponse(ROOT/"static"/"index.html")
@app.get("/api/health")
def health(): return {"ok":True,"service":"TaskHub"}
@app.get("/api/me")
def me(x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data)
 with connect() as c:
  bal=c.execute("SELECT COALESCE(SUM(amount_kop),0) n FROM ledger WHERE user_id=?",(u["id"],)).fetchone()["n"]
  counts={r["status"]:r["n"] for r in c.execute("SELECT status,COUNT(*) n FROM tasks WHERE assignee_id=? GROUP BY status",(u["id"],))}
 return {"user":u,"balance_kop":bal,"counts":counts}
@app.get("/api/tasks")
def tasks(status:str="available",x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data)
 with connect() as c:
  if status=="all" and u["role"]=="admin": rows=c.execute("SELECT t.*,u.first_name assignee_name FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id ORDER BY t.id DESC").fetchall()
  elif status=="mine": rows=c.execute("SELECT * FROM tasks WHERE assignee_id=? ORDER BY id DESC",(u["id"],)).fetchall()
  else: rows=c.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC",(status,)).fetchall()
 return [dict(r) for r in rows]
@app.post("/api/tasks")
def create_task(data:TaskIn,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data); admin(u)
 with connect() as c:
  cur=c.execute("INSERT INTO tasks(title,description,category,reward_kop,created_by,deadline) VALUES(?,?,?,?,?,?)",(data.title,data.description,data.category,data.reward_rub*100,u["id"],data.deadline))
  c.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",(u["id"],"create","task",cur.lastrowid))
  return dict(c.execute("SELECT * FROM tasks WHERE id=?",(cur.lastrowid,)).fetchone())
@app.post("/api/tasks/{task_id}/claim")
def claim(task_id:int,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data)
 with connect() as c:
  cur=c.execute("UPDATE tasks SET status='in_progress',assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='available'",(u["id"],task_id))
  if cur.rowcount!=1: raise HTTPException(409,"Задача уже занята или недоступна")
  c.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",(u["id"],"claim","task",task_id))
 return {"ok":True}
@app.post("/api/tasks/{task_id}/submit")
def submit(task_id:int,data:SubmitIn,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data)
 with connect() as c:
  t=c.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
  if not t or t["assignee_id"]!=u["id"] or t["status"] not in ("in_progress","revision"): raise HTTPException(409,"Нельзя сдать эту задачу")
  c.execute("INSERT INTO submissions(task_id,user_id,body) VALUES(?,?,?)",(task_id,u["id"],data.body)); c.execute("UPDATE tasks SET status='submitted',updated_at=CURRENT_TIMESTAMP WHERE id=?",(task_id,))
 return {"ok":True}
@app.get("/api/review")
def review(x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data); admin(u)
 with connect() as c:
  return [dict(r) for r in c.execute("SELECT t.*,u.first_name assignee_name,s.body submission,s.created_at submitted_at FROM tasks t JOIN users u ON u.id=t.assignee_id LEFT JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE task_id=t.id) WHERE t.status='submitted' ORDER BY t.id") ]
@app.post("/api/tasks/{task_id}/review")
def decide(task_id:int,data:ReviewIn,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data); admin(u)
 if data.decision not in ("accept","revision","reject"): raise HTTPException(400,"Неизвестное решение")
 with connect() as c:
  t=c.execute("SELECT * FROM tasks WHERE id=? AND status='submitted'",(task_id,)).fetchone()
  if not t: raise HTTPException(404,"Сдача не найдена")
  status={"accept":"accepted","revision":"revision","reject":"rejected"}[data.decision]
  c.execute("UPDATE tasks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(status,task_id))
  if data.decision=="accept": c.execute("INSERT INTO ledger(user_id,task_id,amount_kop,kind,note) VALUES(?,?,?,?,?)",(t["assignee_id"],task_id,t["reward_kop"],"task_earning",data.comment or "Задача принята"))
  c.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",(u["id"],data.decision,"task",task_id,data.comment))
 return {"ok":True,"status":status}
@app.get("/api/ledger")
def ledger(x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
 u=user(x_telegram_init_data)
 with connect() as c: return [dict(r) for r in c.execute("SELECT l.*,t.title FROM ledger l LEFT JOIN tasks t ON t.id=l.task_id WHERE l.user_id=? ORDER BY l.id DESC",(u["id"],))]
