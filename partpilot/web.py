"""標準庫 HTTP 入口；所有資料操作交由 Service 授權。"""
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit


def _e(value):
    return escape(str(value), quote=True)


def _table(headers, rows):
    return "<table><thead><tr>" + "".join("<th>" + _e(h) + "</th>" for h in headers) + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join("<td>" + _e(cell) + "</td>" for cell in row) + "</tr>" for row in rows) + "</tbody></table>"


SCRIPT = """
async function post(path, data) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  if(r.redirected){location.href='/login'; throw new Error('請重新登入');}
  const body = await r.json();
  if(!r.ok) throw new Error(body.error || '操作失敗');
  return body;
}
"""


def _page(title, body, user=None, script=""):
    nav = ""
    if user:
        links = '<a href="/admin">管理</a>' if user["role"] == "admin" else '<a href="/">問答</a>'
        if user["role"] == "component":
            links += ' <a href="/review">待確認資料</a>'
        nav = '<nav>' + _e(user["username"]) + '（' + _e(user["role"]) + '） ' + links + ' <a href="/logout">登出</a></nav>'
    return ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + _e(title) + '</title><style>'
            'body{font:16px system-ui,sans-serif;max-width:1200px;margin:32px auto;padding:0 20px;color:#172b40}'
            'nav{padding:12px;background:#eef3f8}a{margin:0 8px}button,input,select,textarea{font:inherit;padding:8px;margin:4px}'
            'textarea{width:95%;min-height:90px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:16px}'
            'table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border:1px solid #ccd6df;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}'
            '.error{color:#ac2430}button{cursor:pointer}button:disabled{cursor:default}small{display:block}</style>'
            + nav + '<h1>' + _e(title) + '</h1>' + body
            + ('<script>' + SCRIPT + script + '</script>' if script else '') + '</html>')


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        self.sessions = {}
        self.session_lock = threading.RLock()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, content, content_type="application/json; charset=utf-8", headers=None):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, value):
        self._reply(status, json.dumps(value, ensure_ascii=False))

    def _html(self, content):
        self._reply(200, content, "text/html; charset=utf-8")

    def _redirect(self, destination, cookie=None):
        headers = {"Location": destination}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._reply(303, "", headers=headers)

    def _token(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return cookie["partpilot_session"].value if "partpilot_session" in cookie else None

    def _user(self):
        token = self._token()
        with self.server.session_lock:
            now = time.monotonic()
            for key in [key for key, row in self.server.sessions.items() if row[1] < now]:
                del self.server.sessions[key]
            session = self.server.sessions.get(token)
            return session[0] if session else None

    def _body(self, form=False):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise ValueError("請求內容大小無效（上限 65536 bytes）")
        body = self.rfile.read(length).decode("utf-8")
        if form:
            return {key: values[0] for key, values in parse_qs(body).items()}
        if self.headers.get_content_type() != "application/json":
            raise ValueError("請使用 application/json")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("請求必須為 JSON 物件")
        return result

    def _guard(self, user, roles, action):
        self.server.service._require(user, roles, action)

    def do_GET(self):
        self._handle(False)

    def do_POST(self):
        self._handle(True)

    def _handle(self, post):
        try:
            path = urlsplit(self.path).path
            svc = self.server.service
            if post:
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    raise PermissionError("不接受外部來源的操作")
            if path == "/login":
                if not post:
                    self._html(_page("PartPilot 登入", '<form method="post" action="/login">'
                        '<label>帳號 <input name="username" autocomplete="username" required></label><br>'
                        '<label>密碼 <input name="password" type="password" autocomplete="current-password" required></label>'
                        '<button>登入</button></form>'))
                else:
                    data = self._body(form=True)
                    user = svc.login(data.get("username", ""), data.get("password", ""))
                    if user is None:
                        raise PermissionError("登入失敗：帳號或密碼錯誤")
                    token = secrets.token_urlsafe(32)
                    with self.server.session_lock:
                        self.server.sessions[token] = (user, time.monotonic() + 12 * 3600)
                    self._redirect("/admin" if user["role"] == "admin" else "/",
                                   "partpilot_session=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200")
                return
            user = self._user()
            if not user:
                self._redirect("/login")
                return
            if path == "/logout":
                with self.server.session_lock:
                    self.server.sessions.pop(self._token(), None)
                self._redirect("/login", "partpilot_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            elif not post and path == "/":
                self._guard(user, {"supply_chain", "product_line", "plant_control", "component"}, "ask")
                self._html(_page("物料問答", '<p>內部系統，不提供外寄服務。資料基準日：2026-11-06。</p>'
                    '<p>目前 conv_id：<code id="conv">（新對話）</code> <button id="new">新對話</button></p>'
                    '<div id="history"></div><form id="ask"><textarea id="question" required placeholder="輸入問題"></textarea>'
                    '<button id="send">送出</button></form><p id="error" class="error"></p>', user, """
let conv=null;
document.getElementById('new').onclick=()=>{conv=null;document.getElementById('conv').textContent='（新對話）';document.getElementById('history').replaceChildren();};
document.getElementById('ask').onsubmit=async(e)=>{e.preventDefault(); const q=document.getElementById('question');const send=document.getElementById('send');
send.disabled=true;document.getElementById('error').textContent='';
try{const r=await post('/api/ask',{question:q.value,conv_id:conv});conv=r.conv_id;document.getElementById('conv').textContent=conv;
const box=document.createElement('section');const question=document.createElement('p');question.textContent=q.value;const answer=document.createElement('pre');answer.textContent=r.summary;
const details=document.createElement('details');const label=document.createElement('summary');label.textContent='原始 JSON';const raw=document.createElement('pre');raw.textContent=JSON.stringify(r.raw_results,null,2);details.append(label,raw);
const usage=document.createElement('small');usage.textContent='本題 tokens：'+JSON.stringify(r.usage)+'；工具次數：'+r.tool_calls;
box.append(question,answer,details,usage);document.getElementById('history').append(box);q.value='';}
catch(err){document.getElementById('error').textContent=err.message;}finally{send.disabled=false;}};
"""))
            elif not post and path == "/review":
                self._guard(user, {"component"}, "review")
                rows = []
                for item in svc.review_items(user):
                    rid = _e(item["ReviewID"])
                    options = item.get("Options", [])
                    options = ''.join('<option value="' + str(i) + '">' + _e(str(i) + ': ' + json.dumps(option, ensure_ascii=False)[:500]) + '</option>' for i, option in enumerate(options))
                    action = '<select class="choice"><option value="">選擇候選索引</option>' + options + '</select>' if item.get("Kind", "").endswith("conflict") else ''
                    if item.get("can_approve"):
                        action += '<button type="button" data-decision="approve">核准</button><button type="button" data-decision="reject">駁回</button>'
                    else:
                        action = _e(item.get("approval_note", "不可核准"))
                    cells = [item["ReviewID"], item["Kind"], item["Key"], item["Reason"],
                             '\n'.join(str(i) + ': ' + json.dumps(c, ensure_ascii=False)[:800] for i, c in enumerate(options))]
                    rows.append('<tr data-id="' + rid + '">' + ''.join('<td>' + _e(c) + '</td>' for c in cells) + '<td>' + action + '</td></tr>')
                table = '<table><tr>' + ''.join('<th>' + h + '</th>' for h in ["ReviewID", "Kind", "Key", "Reason", "Options 摘要（索引從 0 開始）", "決定"]) + '</tr>' + ''.join(rows) + '</table>'
                records = svc.own_approvals(user)
                body = '<p id="error" class="error"></p>' + table + '<h2>我的核准紀錄</h2>' + _table(
                    ["ReviewID", "決定", "候選", "核准者", "時間"],
                    [[r["ReviewID"], r["decision"], r.get("choice"), r["by"], r["at"]] for r in records])
                self._html(_page("待人工確認", body, user, """
document.querySelectorAll('button[data-decision]').forEach(b=>b.onclick=async()=>{const row=b.closest('tr');const select=row.querySelector('.choice');
row.querySelectorAll('button').forEach(x=>x.disabled=true);
try{await post('/api/review',{review_id:row.dataset.id,decision:b.dataset.decision,choice:select&&select.value!==''?Number(select.value):null});location.reload();}
catch(err){document.getElementById('error').textContent=err.message;row.querySelectorAll('button').forEach(x=>x.disabled=false);}});
"""))
            elif not post and path == "/admin":
                self._guard(user, {"admin"}, "admin")
                audit = svc.audit_view(user)
                usage = svc.usage_summary(user)
                body = '<button id="clean">執行資料清理</button><pre id="status"></pre><h2>Token 用量彙總</h2><pre>' + _e(json.dumps(usage, ensure_ascii=False, indent=2)) + '</pre><h2>稽核紀錄</h2>' + _table(
                    ["時間", "帳號", "角色", "事件", "細節"],
                    [[r["ts"], r["username"], r["role"], r["event"], json.dumps(r["detail"], ensure_ascii=False)] for r in audit])
                self._html(_page("系統管理", body, user, """
document.getElementById('clean').onclick=async()=>{const b=document.getElementById('clean');b.disabled=true;
try{const r=await post('/api/clean',{});document.getElementById('status').textContent=JSON.stringify(r,null,2);}
catch(err){document.getElementById('status').textContent=err.message;}finally{b.disabled=false;}};
"""))
            elif post and path == "/api/ask":
                self._guard(user, {"supply_chain", "product_line", "plant_control", "component"}, "ask")
                data = self._body()
                self._json(200, svc.agent(user).ask(data.get("question"), data.get("conv_id")))
            elif post and path == "/api/review":
                self._guard(user, {"component"}, "review")
                data = self._body()
                self._json(200, svc.decide(user, data.get("review_id"), data.get("decision"), data.get("choice")))
            elif post and path == "/api/clean":
                self._guard(user, {"admin"}, "clean")
                self._body()
                self._json(200, svc.run_clean(user))
            else:
                self._json(404, {"error": "找不到此頁面或操作"})
        except Exception as exc:
            traceback.print_exc()
            message = str(exc) if isinstance(exc, (ValueError, PermissionError)) else "操作失敗，請洽系統管理員查看錯誤紀錄"
            self._json(403 if isinstance(exc, PermissionError) else 400, {"error": message})


def serve(service, host="127.0.0.1", port=18765):
    with Server((host, port), service) as server:
        print("PartPilot 網頁：http://{}:{}".format(host, port), flush=True)
        server.serve_forever()
