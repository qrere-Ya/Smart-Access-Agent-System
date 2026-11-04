"use strict";
// 遠端測試入口的前端：只用 textContent 顯示內容（不用 innerHTML），畫面完全由伺服器回傳的狀態重繪。
const $ = (id) => document.getElementById(id);
const POLL_BUSY_MS = 800;
let timer = null;

async function api(path, body) {
  const options = body === undefined
    ? { credentials: "same-origin" }
    : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, options);
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  return { status: res.status, data };
}

function bubble(kind, text) {
  const div = document.createElement("div");
  div.className = "bubble " + kind;
  div.textContent = text;
  return div;
}

function render(state) {
  const box = $("messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.replaceChildren();
  for (const turn of state.turns) {
    box.append(bubble("me", turn.q), bubble("bot", turn.a));
  }
  if (state.current) {
    box.append(bubble("me", state.current.q), bubble("bot", state.current.a || "思考中…"));
  }
  if (nearBottom) box.scrollTop = box.scrollHeight;
  $("send").disabled = state.busy;
  let status = "";
  if (state.current && state.current.ahead !== null && state.current.ahead !== undefined && !state.current.a) {
    status = state.current.ahead > 0 ? "排隊中，前面還有 " + state.current.ahead + " 人" : "輪到你了，處理中…";
  } else if (state.busy) {
    status = "處理中…";
  }
  $("status").textContent = status;
}

function showLogin(message) {
  clearTimeout(timer);
  $("chat").hidden = true;
  $("login").hidden = false;
  const err = $("login-error");
  err.hidden = !message;
  err.textContent = message || "";
}

async function refresh() {
  clearTimeout(timer);
  const { status, data } = await api("/api/state");
  if (status === 401) return showLogin("");
  $("login").hidden = true;
  $("chat").hidden = false;
  $("who").textContent = data.name;
  render(data);
  if (data.busy || data.current) timer = setTimeout(refresh, POLL_BUSY_MS);
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const { status, data } = await api("/api/login", { key: $("key").value });
  $("key").value = "";
  if (status === 200) return refresh();
  showLogin(data.error || "登入失敗");
});

$("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("question").value.trim();
  if (!text) return;
  const { status, data } = await api("/api/ask", { question: text });
  if (status === 401) return showLogin("登入已逾時，請重新登入");
  if (status === 200) $("question").value = "";
  else $("status").textContent = data.error || "送出失敗";
  refresh();
});

$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("ask-form").requestSubmit();
  }
});

$("new-chat").addEventListener("click", async () => {
  const { data } = await api("/api/new", {});
  if (data.ok === false && data.error) $("status").textContent = data.error;
  refresh();
});

$("logout").addEventListener("click", async () => {
  await api("/api/logout", {});
  showLogin("");
});

refresh();
