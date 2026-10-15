// The website's connection to the planner, loaded only by the site build
// (make_site.py), before the page's own script.
//
// The page never does budget math, and neither does this file. An edit is
// saved to the online API as "user-<id>:edits"; the planner server picks it
// up, runs it, and writes the result to "user-<id>:view", which this file
// reads back. Every edit has a number (seq). The view says which number the
// server has finished up to (seq_done) and what happened to each one.
//
// window.BUDGET_WEB = {api: "https://budget-api.lucaguiga.com"} is set by the site build.
(function () {
  "use strict";
  const API = window.BUDGET_WEB.api.replace(/\/$/, "");
  const POLL_START = 700, POLL_MAX = 3000;        // ms between checks while waiting
  const SLOW_AFTER = 15000;                       // ms before saying the server seems away
  const IDLE_CHECK = 60000;                       // ms between checks for edits from other devices
  const DEBOUNCED = new Set(["coverage", "request"]);   // previews that change as you type

  let user = null;        // {user_id, username}
  let view = null;        // the server's last view (see server/sync.py, build_view)
  let pending = [];       // edits sent but not finished: [{seq, id, kind, body, at}]
  const waiters = new Map();   // seq -> {kind, resolve}
  let polling = false;
  let writing = Promise.resolve();
  const timers = {};      // debounce timers by preview type

  const names = () => ({edits: `user-${user.user_id}:edits`, view: `user-${user.user_id}:view`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const key = q => JSON.stringify(sortKeys(q));   // must match server/sync.py answer_key
  function sortKeys(v) {
    if (Array.isArray(v)) return v.map(sortKeys);
    if (v && typeof v === "object") return Object.fromEntries(Object.keys(v).sort().map(k => [k, sortKeys(v[k])]));
    return v;
  }

  // ---------------------------------------------------------------- API

  class LoggedOut extends Error {}

  async function api(method, path, body) {
    let r;
    try {
      r = await fetch(API + path, {
        method, credentials: "include",
        headers: body === undefined ? {} : {"Content-Type": "application/json"},
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new Error("Couldn't reach the website's server. Check your internet connection.");
    }
    if (r.status === 401) { showLogin(); throw new LoggedOut("Please log in again."); }
    if (r.status === 404) return null;
    if (r.status === 429) throw new Error("Too many tries. Wait a few minutes and try again.");
    if (!r.ok) throw new Error(`The website had a problem (error ${r.status}). Try again in a moment.`);
    const text = await r.text();
    return text ? JSON.parse(text) : {};
  }
  const read = async name => (await api("GET", `/data/${encodeURIComponent(name)}`))?.payload ?? null;

  // ---------------------------------------------------------------- views

  function applyView(v) {
    if (!v || (view && v.seq_done < view.seq_done)) return;
    const changed = !view || v.state_rev !== view.state_rev;
    view = v;
    pending = pending.filter(e => e.seq > v.seq_done);
    let handled = false;
    for (const [seq, w] of waiters) {
      if (seq > v.seq_done) continue;
      waiters.delete(seq);
      const res = (v.results || {})[seq] || {ok: true};
      if (w.kind === "action") {
        handled = true;
        w.resolve({ok: res.ok, error: res.error, budget: v.budget, quarter_options: v.quarter_options});
      } else {
        w.resolve(res.ok ? {ok: true, answer: res.answer} : {ok: false, error: res.error});
      }
    }
    if (changed && !handled && transport.onUpdate) transport.onUpdate(v);
  }

  async function poll() {
    if (polling) return;
    polling = true;
    const started = Date.now();
    let delay = POLL_START;
    while (waiters.size || pending.length) {
      await sleep(delay);
      try {
        applyView(await read(names().view));
        status(Date.now() - started > SLOW_AFTER
          ? "Waiting for the planner server. Your change is saved and will go through when it's back."
          : "Updating...");
      } catch (e) {
        if (e instanceof LoggedOut) break;
        status(e.message);
      }
      delay = Math.min(delay * 1.3, POLL_MAX);
    }
    status(null);
    polling = false;
  }

  // Save the edit list. Other devices may have added edits, so merge with
  // what's online first, then number the new edit after everything seen.
  function send(kind, body) {
    return new Promise((resolve) => {
      writing = writing.then(async () => {
        let edit;
        try {
          const online = (await read(names().edits))?.edits || [];
          const done = view ? view.seq_done : 0;
          const ids = new Set(pending.map(e => e.id));
          for (const e of online) if (e.seq > done && !ids.has(e.id)) pending.push(e);
          const top = Math.max(done, ...pending.map(e => e.seq), ...online.map(e => e.seq));
          edit = {seq: top + 1, id: crypto.randomUUID(), kind, body, at: new Date().toISOString()};
          pending.push(edit);
          pending.sort((a, b) => a.seq - b.seq);
          await api("PUT", `/data/${encodeURIComponent(names().edits)}`, {edits: pending});
        } catch (e) {
          if (edit) pending = pending.filter(x => x !== edit);
          resolve({ok: false, error: e.message});
          return;
        }
        waiters.set(edit.seq, {kind, resolve});
        status("Updating...");
        poll();
      });
    });
  }

  // ---------------------------------------------------------------- what the page calls

  const transport = window.BudgetTransport = {
    unreachable: "Couldn't reach the website's server. Check your internet connection.",
    resetNote: "Budget reset.",

    async state() {
      try {
        user = await api("GET", "/session");
      } catch (e) {
        if (e instanceof LoggedOut) return null;
        throw e;
      }
      if (!user) { showLogin(); return null; }
      showAccount();
      const [v, edits] = await Promise.all([read(names().view), read(names().edits)]);
      if (!v) throw new Error("Your budget isn't ready yet. The planner server sets it up; try again in a minute.");
      view = v;
      pending = (edits?.edits || []).filter(e => e.seq > v.seq_done);
      if (pending.length) poll();   // edits from before a reload, still being processed
      return {ok: true, budget: v.budget, quarter_options: v.quarter_options};
    },

    action(body) { return send("action", body); },

    query(q) {
      const answers = view?.answers || {};
      if (key(q) in answers) return Promise.resolve({ok: true, answer: answers[key(q)]});
      if (!DEBOUNCED.has(q.type)) return send("query", q);
      // Typing fires a preview per key; only the last one in a pause is sent.
      return new Promise(resolve => {
        const prev = timers[q.type];
        if (prev) { clearTimeout(prev.timer); prev.resolve({ok: false, error: ""}); }
        timers[q.type] = {resolve, timer: setTimeout(() => {
          delete timers[q.type];
          send("query", q).then(resolve);
        }, 500)};
      });
    },
  };

  // Edits made on another device show up within a minute.
  setInterval(async () => {
    if (!user || polling) return;
    try { applyView(await read(names().view)); } catch (e) { /* tried again next minute */ }
  }, IDLE_CHECK);

  // ---------------------------------------------------------------- login screen, account bar, status

  const css = document.createElement("style");
  css.textContent = `
    #webLogin { position: fixed; inset: 0; background: var(--bg); display: grid; place-items: center; z-index: 50; padding: 16px; }
    #webLogin form { width: 100%; max-width: 340px; }
    #webLogin label { display: block; margin: 12px 0 4px; font-weight: 600; }
    #webLogin input { width: 100%; box-sizing: border-box; background: var(--surface-2); border: 1px solid var(--border);
      border-radius: 8px; padding: 8px 10px; }
    #webLogin button { width: 100%; margin-top: 16px; }
    #webLoginErr { color: var(--danger); min-height: 1.4em; margin: 10px 0 0; }
    #webAccount { position: fixed; right: 12px; bottom: 12px; background: var(--surface); border: 1px solid var(--border);
      border-radius: 99px; padding: 4px 6px 4px 12px; font-size: 13px; box-shadow: var(--shadow); z-index: 40; }
    #webStatus { position: fixed; left: 50%; top: 10px; transform: translateX(-50%); background: var(--accent-soft);
      color: var(--text); border: 1px solid var(--accent); border-radius: 99px; padding: 4px 14px; font-size: 13px;
      z-index: 45; max-width: calc(100% - 32px); text-align: center; display: none; }`;
  document.head.appendChild(css);

  function status(text) {
    let el = document.getElementById("webStatus");
    if (!el) {
      el = document.createElement("div");
      el.id = "webStatus"; el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = text || "";
    el.style.display = text ? "block" : "none";
  }

  function showLogin() {
    user = null;
    document.getElementById("webAccount")?.remove();
    if (document.getElementById("webLogin")) return;
    const box = document.createElement("div");
    box.id = "webLogin";
    box.innerHTML = `<form class="card" novalidate>
        <h1>Guiga School Budget</h1>
        <label for="webUser">Username</label>
        <input id="webUser" autocomplete="username" autocapitalize="none" spellcheck="false" required>
        <label for="webPass">Password</label>
        <input id="webPass" type="password" autocomplete="current-password" required>
        <button class="primary" type="submit">Log in</button>
        <p id="webLoginErr" role="alert"></p>
      </form>`;
    document.body.appendChild(box);
    const err = box.querySelector("#webLoginErr");
    box.querySelector("form").onsubmit = async e => {
      e.preventDefault();
      const username = box.querySelector("#webUser").value.trim();
      const password = box.querySelector("#webPass").value;
      if (!username || !password) { err.textContent = "Enter your username and password."; return; }
      err.textContent = "";
      let r;
      try {
        r = await fetch(API + "/login", {method: "POST", credentials: "include",
          headers: {"Content-Type": "application/json"}, body: JSON.stringify({username, password})});
      } catch (x) {
        err.textContent = "Couldn't reach the website's server. Check your internet connection."; return;
      }
      if (r.status === 429) { err.textContent = "Too many tries. Wait 15 minutes and try again."; return; }
      if (!r.ok) { err.textContent = "That username and password don't match."; return; }
      box.remove();
      transport.reload();
    };
    box.querySelector("#webUser").focus();
  }

  function showAccount() {
    if (document.getElementById("webAccount")) return;
    const bar = document.createElement("div");
    bar.id = "webAccount";
    bar.innerHTML = `<span></span> <button class="link" type="button">Log out</button>`;
    bar.querySelector("span").textContent = user.username;
    bar.querySelector("button").onclick = async () => {
      try { await api("POST", "/logout", {}); } catch (e) { /* logged out either way */ }
      location.reload();
    };
    document.body.appendChild(bar);
  }
})();
