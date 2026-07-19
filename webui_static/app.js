const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const THEME_KEY = "grok-webui-theme";

const state = {
  config: {},
  task: {},
  logLines: [],
  es: null,
  accFile: "",
  accRows: [],
  accFiles: [],
  localName: "",
  localFiles: [],
  remoteFiles: [],
  modalHandler: null,
  theme: "light",
};

function getTheme() {
  const t = document.documentElement.getAttribute("data-theme");
  return t === "dark" ? "dark" : "light";
}

function applyTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  state.theme = next;
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem(THEME_KEY, next); } catch {}
  const icon = $("#themeIcon");
  const label = $("#themeLabel");
  if (icon) icon.textContent = next === "light" ? "☀️" : "🌙";
  if (label) label.textContent = next === "light" ? "亮色" : "暗色";
  const btn = $("#btnTheme");
  if (btn) btn.title = next === "light" ? "切换到暗色主题" : "切换到亮色主题";
}

function toggleTheme() {
  applyTheme(getTheme() === "light" ? "dark" : "light");
  toast(getTheme() === "light" ? "已切换亮色主题" : "已切换暗色主题", "ok");
}

const TABS = {
  register: ["注册控制台", "启动批量注册、实时日志、NSFW / CPA 自动入库"],
  accounts: ["账号文件 CRUD", "accounts_*.txt 的增删改查"],
  local: ["本地 Auth CRUD", "xai-*.json 查看 / 编辑 / 删除 / 上传"],
  remote: ["远程 CPA CRUD", "远程 auth-files 列表 / 新建 / 删除"],
  health: ["健康检测", "Token 余额 / 过期 / 401 重认证 / 同步远程"],
  config: ["系统配置", "邮箱、代理、CPA、Cloudflare 全量配置"],
};

const CONFIG_FIELDS = [
  { key: "email_provider", label: "邮箱服务商", type: "select", options: ["yyds", "duckmail", "cloudflare"] },
  { key: "register_count", label: "注册数量", type: "number" },
  { key: "enable_nsfw", label: "开启 NSFW", type: "bool" },
  { key: "proxy", label: "代理", type: "text", full: true },
  { key: "user_agent", label: "User-Agent", type: "text", full: true },
  { key: "yyds_api_key", label: "YYDS API Key", type: "text" },
  { key: "yyds_jwt", label: "YYDS JWT", type: "text" },
  { key: "defaultDomains", label: "默认域名", type: "text" },
  { key: "duckmail_api_key", label: "DuckMail API Key", type: "text" },
  { key: "cloudflare_api_base", label: "Cloudflare API Base", type: "text", full: true },
  { key: "cloudflare_api_key", label: "Cloudflare API Key", type: "text" },
  { key: "cloudflare_auth_mode", label: "Cloudflare Auth Mode", type: "select", options: ["none", "bearer", "x-api-key", "x-admin-auth", "query-key"] },
  { key: "cloudflare_custom_auth", label: "Cloudflare Custom Auth", type: "text" },
  { key: "cloudflare_path_domains", label: "CF path domains", type: "text" },
  { key: "cloudflare_path_accounts", label: "CF path accounts", type: "text" },
  { key: "cloudflare_path_token", label: "CF path token", type: "text" },
  { key: "cloudflare_path_messages", label: "CF path messages", type: "text" },
  { key: "cpa_auto_add", label: "CPA 自动入库", type: "bool" },
  { key: "cpa_auth_dir", label: "CPA 本地目录", type: "text", full: true },
  { key: "cpa_remote_url", label: "CPA 远程地址", type: "text", full: true },
  { key: "cpa_management_key", label: "CPA 管理密钥", type: "text", full: true },
];

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function toast(msg, type = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${type}`.trim();
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2800);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function appendLog(line) {
  state.logLines.push(line);
  if (state.logLines.length > 3000) state.logLines = state.logLines.slice(-2500);
  const view = $("#logView");
  const nearBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 90;
  view.textContent = state.logLines.join("\n");
  if (nearBottom) view.scrollTop = view.scrollHeight;
}

function setRunningUI(running) {
  $("#btnStart").disabled = running;
  $("#btnStop").disabled = !running;
  const badge = $("#runBadge");
  badge.textContent = running ? "运行中" : "就绪";
  badge.className = `pill ${running ? "run" : "idle"}`;
}

function renderTask(task = {}) {
  state.task = task;
  setRunningUI(!!task.running);
  $("#statsBadge").textContent = `成功 ${task.success || 0} · 失败 ${task.fail || 0}`;
  $("#stTarget").textContent = task.target || 0;
  $("#stCurrent").textContent = task.current || 0;
  $("#stSuccess").textContent = task.success || 0;
  $("#stFail").textContent = task.fail || 0;
  $("#taskMeta").textContent = [
    `账号文件: ${task.accounts_file || "-"}`,
    `开始: ${task.started_at || "-"}`,
    `结束: ${task.finished_at || "-"}`,
    task.last_error ? `最近错误: ${task.last_error}` : "",
  ].filter(Boolean).join("\n");

  const list = $("#resultList");
  const results = task.results || [];
  if (!results.length) {
    list.className = "table-wrap empty";
    list.textContent = "暂无结果";
    return;
  }
  list.className = "table-wrap";
  list.innerHTML = `<table class="table"><thead><tr>
      <th class="col-email">邮箱</th><th class="col-pass">密码</th><th class="col-sso">SSO</th>
    </tr></thead><tbody>
    ${results.map((r) => `<tr>
      <td class="col-email" title="${esc(r.email)}"><span class="cell-clip">${esc(r.email)}</span></td>
      <td class="col-pass mono" title="${esc(r.password || "")}"><span class="cell-clip mono">${esc(r.password || "")}</span></td>
      <td class="col-sso mono" title="${esc(r.sso || "")}"><span class="cell-clip mono">${esc(r.sso || "")}</span></td>
    </tr>`).join("")}
  </tbody></table>`;
}

function renderConfigForm(cfg = {}) {
  state.config = cfg;
  const form = $("#configForm");
  form.innerHTML = CONFIG_FIELDS.map((f) => {
    const val = cfg[f.key] ?? "";
    const full = f.full ? " full" : "";
    if (f.type === "bool") {
      return `<div class="field bool${full}"><input type="checkbox" id="cfg_${f.key}" ${val ? "checked" : ""} /><label for="cfg_${f.key}">${f.label}</label></div>`;
    }
    if (f.type === "select") {
      const opts = (f.options || []).map((o) => `<option value="${o}" ${String(val) === o ? "selected" : ""}>${o}</option>`).join("");
      return `<div class="field${full}"><label>${f.label}</label><select id="cfg_${f.key}">${opts}</select></div>`;
    }
    return `<div class="field${full}"><label>${f.label}</label><input id="cfg_${f.key}" type="${f.type === "number" ? "number" : "text"}" value="${esc(val)}" /></div>`;
  }).join("");

  $("#countInput").value = cfg.register_count || 1;
  $("#nsfwToggle").checked = !!cfg.enable_nsfw;
  $("#cpaToggle").checked = !!cfg.cpa_auto_add;
  $("#providerSelect").value = cfg.email_provider || "yyds";
}

function collectConfig() {
  const out = { ...state.config };
  for (const f of CONFIG_FIELDS) {
    const el = document.getElementById(`cfg_${f.key}`);
    if (!el) continue;
    if (f.type === "bool") out[f.key] = !!el.checked;
    else if (f.type === "number") out[f.key] = Number(el.value || 0);
    else out[f.key] = el.value;
  }
  out.register_count = Number($("#countInput").value || out.register_count || 1);
  out.enable_nsfw = $("#nsfwToggle").checked;
  out.cpa_auto_add = $("#cpaToggle").checked;
  out.email_provider = $("#providerSelect").value;
  return out;
}

function openModal(title, bodyHtml, onOk) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  state.modalHandler = onOk;
  $("#modal").classList.remove("hidden");
  const first = $("#modalBody input, #modalBody textarea, #modalBody select");
  if (first) first.focus();
}

function closeModal() {
  $("#modal").classList.add("hidden");
  state.modalHandler = null;
}

async function loadConfig() {
  const data = await api("/api/config");
  renderConfigForm(data.config || {});
}

async function saveConfig() {
  const config = collectConfig();
  const data = await api("/api/config", { method: "POST", body: JSON.stringify({ config }) });
  renderConfigForm(data.config || config);
  toast("配置已保存", "ok");
  appendLog("[UI] 配置已保存");
}

async function refreshStatus() {
  const data = await api("/api/status");
  renderTask(data.task || {});
  if (data.config_summary) {
    $("#providerSelect").value = data.config_summary.email_provider || $("#providerSelect").value;
    $("#countInput").value = data.config_summary.register_count || $("#countInput").value;
    $("#nsfwToggle").checked = !!data.config_summary.enable_nsfw;
    $("#cpaToggle").checked = !!data.config_summary.cpa_auto_add;
  }
}

async function startRegister() {
  await saveConfig();
  const count = Number($("#countInput").value || 1);
  await api("/api/register/start", { method: "POST", body: JSON.stringify({ count }) });
  toast(`已启动 x${count}`, "ok");
  await refreshStatus();
}

async function stopRegister() {
  await api("/api/register/stop", { method: "POST", body: "{}" });
  toast("正在停止…");
  await refreshStatus();
}

// -------------------- Accounts CRUD --------------------
async function loadAccounts() {
  const data = await api("/api/accounts");
  state.accFiles = data.files || [];
  const q = ($("#accSearch").value || "").toLowerCase();
  const files = state.accFiles.filter((f) => !q || f.name.toLowerCase().includes(q));
  const box = $("#accountFiles");
  if (!files.length) {
    box.className = "list empty";
    box.textContent = "暂无 accounts_*.txt";
    return;
  }
  box.className = "list";
  box.innerHTML = files.map((f) => `<div class="item ${state.accFile === f.name ? "active" : ""}" data-name="${esc(f.name)}">
    <div class="t">${esc(f.name)}</div>
    <div class="d">${f.count} 条 · ${esc(f.mtime || "")}</div>
  </div>`).join("");
  box.querySelectorAll(".item").forEach((el) => {
    el.onclick = () => openAccountFile(el.dataset.name);
  });
}

async function openAccountFile(name) {
  state.accFile = name;
  const data = await api(`/api/accounts/${encodeURIComponent(name)}`);
  state.accRows = data.rows || [];
  $("#accDetailTitle").textContent = name;
  $("#btnAccAddRow").disabled = false;
  $("#btnAccSaveAll").disabled = false;
  $("#btnAccDelFile").disabled = false;
  $("#btnAccConvertJson").disabled = false;
  renderAccountRows();
  await loadAccounts();
}

function renderAccountRows() {
  const box = $("#accountDetail");
  if (!state.accRows.length) {
    box.className = "table-wrap empty";
    box.textContent = "空文件，可点击「新增行」";
    return;
  }
  box.className = "detail-list";
  box.innerHTML = state.accRows.map((r, i) => `
    <article class="detail-row" data-index="${i}">
      <div class="detail-row-top">
        <div class="detail-index">#${i}</div>
        <div class="detail-main">
          <div class="detail-email" title="${esc(r.email)}">${esc(r.email || "(无邮箱)")}</div>
          <div class="detail-meta">
            <span class="detail-label">密码</span>
            <code class="detail-value" title="${esc(r.password)}">${esc(r.password || "-")}</code>
          </div>
        </div>
        <div class="detail-actions">
          <button class="btn ghost sm" data-edit="${i}">编辑</button>
          <button class="btn danger sm" data-del="${i}">删除</button>
        </div>
      </div>
      <div class="detail-sso">
        <div class="detail-label">SSO</div>
        <code class="detail-sso-value" title="${esc(r.sso || "")}">${esc(r.sso || "-")}</code>
      </div>
    </article>
  `).join("");
  box.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.onclick = () => editAccountRow(Number(btn.dataset.edit));
  });
  box.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => deleteAccountRow(Number(btn.dataset.del));
  });
}

function accountRowForm(row = {}) {
  return `
    <div class="form-row"><label>邮箱</label><input id="m_email" value="${esc(row.email || "")}" /></div>
    <div class="form-row"><label>密码</label><input id="m_password" value="${esc(row.password || "")}" /></div>
    <div class="form-row"><label>SSO</label><textarea id="m_sso" rows="4">${esc(row.sso || "")}</textarea></div>
  `;
}

function createAccountFile() {
  openModal("新建账号文件", `
    <div class="form-row"><label>文件名（可空，自动生成）</label><input id="m_name" placeholder="accounts_custom.txt" /></div>
    <div class="form-row"><label>首行邮箱（可选）</label><input id="m_email" /></div>
    <div class="form-row"><label>密码</label><input id="m_password" /></div>
    <div class="form-row"><label>SSO</label><textarea id="m_sso" rows="3"></textarea></div>
  `, async () => {
    const name = $("#m_name").value.trim();
    const email = $("#m_email").value.trim();
    const password = $("#m_password").value.trim();
    const sso = $("#m_sso").value.trim();
    const rows = email ? [{ email, password, sso }] : [];
    const data = await api("/api/accounts", { method: "POST", body: JSON.stringify({ name, rows }) });
    toast(`已创建 ${data.name}`, "ok");
    await loadAccounts();
    await openAccountFile(data.name);
  });
}

function addAccountRow() {
  if (!state.accFile) return;
  openModal("新增账号行", accountRowForm(), async () => {
    const email = $("#m_email").value.trim();
    const password = $("#m_password").value.trim();
    const sso = $("#m_sso").value.trim();
    if (!email) throw new Error("邮箱必填");
    await api(`/api/accounts/${encodeURIComponent(state.accFile)}/rows`, {
      method: "POST", body: JSON.stringify({ email, password, sso }),
    });
    toast("已新增", "ok");
    await openAccountFile(state.accFile);
  });
}

function editAccountRow(index) {
  const row = state.accRows[index];
  if (!row) return;
  openModal(`编辑第 ${index} 行`, accountRowForm(row), async () => {
    const email = $("#m_email").value.trim();
    const password = $("#m_password").value.trim();
    const sso = $("#m_sso").value.trim();
    await api(`/api/accounts/${encodeURIComponent(state.accFile)}/rows/${index}`, {
      method: "PUT", body: JSON.stringify({ email, password, sso }),
    });
    toast("已更新", "ok");
    await openAccountFile(state.accFile);
  });
}

async function deleteAccountRow(index) {
  if (!confirm(`删除第 ${index} 行？`)) return;
  await api(`/api/accounts/${encodeURIComponent(state.accFile)}/rows/${index}`, { method: "DELETE" });
  toast("已删除行", "ok");
  await openAccountFile(state.accFile);
}

async function saveAllAccountRows() {
  if (!state.accFile) return;
  await api(`/api/accounts/${encodeURIComponent(state.accFile)}`, {
    method: "PUT",
    body: JSON.stringify({ rows: state.accRows }),
  });
  toast("已保存全部", "ok");
}

async function deleteAccountFile() {
  if (!state.accFile) return;
  if (!confirm(`删除文件 ${state.accFile}？`)) return;
  await api(`/api/accounts/${encodeURIComponent(state.accFile)}`, { method: "DELETE" });
  state.accFile = "";
  state.accRows = [];
  $("#accDetailTitle").textContent = "详情";
  $("#accountDetail").className = "table-wrap empty";
  $("#accountDetail").textContent = "选择左侧文件";
  $("#btnAccAddRow").disabled = true;
  $("#btnAccSaveAll").disabled = true;
  $("#btnAccDelFile").disabled = true;
  $("#btnAccConvertJson").disabled = true;
  toast("文件已删除", "ok");
  await loadAccounts();
}

function convertAccountToJson() {
  if (!state.accFile || !state.accRows.length) {
    toast("请先选择账号文件", "error");
    return;
  }
  const count = state.accRows.length;
  openModal(`生成 CPA JSON（${count} 行）`, `
    <p>将把 <strong>${esc(state.accFile)}</strong> 中所有账号的 SSO 转成 CPA JSON 文件。</p>
    <p class="hint">每个 SSO 会走一次 device flow 换 token，耗时较长（约 ${count * 3}-${count * 8} 秒）。</p>
    <div class="checks" style="margin-top:10px">
      <label class="switch"><input id="mSaveLocal" type="checkbox" checked /><span>保存到本地 Auth 目录</span></label>
      <label class="switch"><input id="mUploadRemote" type="checkbox" /><span>同步上传到远程 CPA</span></label>
    </div>
  `, async () => {
    const saveLocal = $("#mSaveLocal").checked;
    const uploadRemote = $("#mUploadRemote").checked;
    if (!saveLocal && !uploadRemote) {
      toast("请至少选择一个目标", "error");
      throw new Error("未选择目标");
    }
    const btn = $("#btnAccConvertJson");
    if (btn) btn.disabled = true;
    showGlobalProgress(true, "running");
    updateGlobalProgress(0, count, `SSO 换 token 中（${count} 行）…`);
    try {
      const data = await api("/api/accounts/convert-to-json", {
        method: "POST",
        body: JSON.stringify({ name: state.accFile, saveLocal, uploadRemote }),
      });
      showGlobalProgress(true, "done");
      updateGlobalProgress(count, count, `转换完成: 成功 ${data.success} / 失败 ${data.fail}`);
      toast(`转换完成: 成功 ${data.success} / 失败 ${data.fail}`, data.fail ? "error" : "ok");
      await loadLocalAuth();
      if (uploadRemote) await loadRemoteAuth();
    } catch (e) {
      showGlobalProgress(true, "done");
      updateGlobalProgress(0, count, `转换失败: ${e.message || e}`);
      throw e;
    } finally {
      if (btn) btn.disabled = false;
      setTimeout(() => showGlobalProgress(false), 5000);
    }
  });
}

async function convertAllAccountsToJson() {
  // 先拉取最新账号文件列表
  await loadAccounts();
  const allFiles = (state.accFiles || []).map((f) => f.name).filter((n) => n.startsWith("accounts_"));
  if (!allFiles.length) {
    toast("没有 accounts_*.txt 文件", "error");
    return;
  }
  const totalRows = (state.accFiles || []).reduce((a, f) => a + (f.count || 0), 0);
  openModal(`全部重新生成（${allFiles.length} 个文件 / ${totalRows} 行）`, `
    <p>将所有 <strong>accounts_*.txt</strong> 中的 SSO 批量转换为 CPA JSON。</p>
    <p class="hint">每条 SSO 走 device flow 换 token，总耗时较长（预估 ${totalRows * 3}-${totalRows * 8} 秒）。</p>
    <div class="checks" style="margin-top:10px">
      <label class="switch"><input id="mSaveLocalAll" type="checkbox" checked /><span>保存到本地 Auth 目录</span></label>
      <label class="switch"><input id="mUploadRemoteAll" type="checkbox" /><span>同步上传到远程 CPA</span></label>
    </div>
  `, async () => {
    const saveLocal = $("#mSaveLocalAll").checked;
    const uploadRemote = $("#mUploadRemoteAll").checked;
    if (!saveLocal && !uploadRemote) {
      toast("请至少选择一个目标", "error");
      throw new Error("未选择目标");
    }
    let okTotal = 0, failTotal = 0;
    const allDetails = [];
    const btn = $("#btnAccConvertAll");
    if (btn) btn.disabled = true;

    await withBatchProgress({
      buttonId: "btnAccConvertAll",
      items: allFiles,
      action: async (fileName) => {
        const d = await api("/api/accounts/convert-to-json", {
          method: "POST",
          body: JSON.stringify({ name: fileName, saveLocal, uploadRemote }),
        });
        okTotal += d.success || 0;
        failTotal += d.fail || 0;
        allDetails.push({ file: fileName, success: d.success, fail: d.fail, details: d.details });
        if (d.fail) throw new Error(`${d.fail} 行转换失败`);
        return { success: d.success, fail: d.fail };
      },
      labelFn: (fileName) => fileName,
      startLabel: "文件转换中",
      successLabel: `共 ${totalRows} 行`,
      failLabel: "部分失败",
      delay: 500,
      onComplete: async () => {
        toast(`全部完成: ${allFiles.length} 个文件，成功 ${okTotal} 行 / 失败 ${failTotal} 行`, failTotal > 0 ? "error" : "ok");
        await loadLocalAuth();
        if (uploadRemote) await loadRemoteAuth();
      },
    });
  });
}

// -------------------- Local Auth CRUD --------------------
async function loadLocalAuth() {
  const data = await api("/api/local-auth");
  state.localFiles = data.files || [];
  $("#localAuthMeta").textContent = `${data.dir}\n共 ${data.count} 个文件`;
  const q = ($("#localSearch").value || "").toLowerCase();
  const files = state.localFiles.filter((f) => {
    const hay = `${f.email || ""} ${f.name || ""}`.toLowerCase();
    return !q || hay.includes(q);
  });
  const box = $("#localAuthList");
  if (!files.length) {
    box.className = "list empty";
    box.textContent = "暂无 xai-*.json";
    return;
  }
  box.className = "list";
  box.innerHTML = files.map((f) => {
    const active = state.localName === f.name ? "active" : "";
    const disabledTag = f.disabled ? '<span class="tag" style="margin-left:6px;font-size:10px">禁用</span>' : "";
    return `<div class="item ${active}" data-name="${esc(f.name)}">
      <div style="display:flex;align-items:center;gap:10px">
        <input type="checkbox" class="local-check" value="${esc(f.name)}" onclick="event.stopPropagation()" />
        <div style="flex:1;min-width:0">
          <div class="t cell-nowrap">${esc(f.email || f.name)}${disabledTag}</div>
          <div class="d cell-nowrap">${esc(f.name)} · ${esc(f.mtime || "")}</div>
        </div>
      </div>
    </div>`;
  }).join("");
  box.querySelectorAll(".item").forEach((el) => {
    el.onclick = (e) => {
      if (e.target.classList.contains("local-check")) return;
      openLocalAuth(el.dataset.name);
    };
  });
  // 更新全选状态
  const allCb = $("#localCheckAll");
  if (allCb) {
    const boxes = $$(".local-check");
    allCb.checked = boxes.length > 0 && boxes.every((c) => c.checked);
    allCb.indeterminate = !allCb.checked && boxes.some((c) => c.checked);
  }
}

async function uploadCheckedLocal() {
  const names = $$(".local-check:checked").map((c) => c.value).filter(Boolean);
  if (!names.length) {
    toast("请先勾选要上传的文件", "error");
    return;
  }
  if (!confirm(`上传 ${names.length} 个文件到远程 CPA？`)) return;
  await uploadLocal(names);
}

async function openLocalAuth(name) {
  state.localName = name;
  const data = await api(`/api/local-auth/${encodeURIComponent(name)}`);
  $("#localDetailTitle").textContent = name;
  $("#localEditor").disabled = false;
  $("#localEditor").value = JSON.stringify(data.record || {}, null, 2);
  $("#btnLocalSave").disabled = false;
  $("#btnLocalUploadOne").disabled = false;
  $("#btnLocalDelete").disabled = false;
  await loadLocalAuth();
}

function createLocalAuth() {
  openModal("新建本地 Auth", `
    <div class="form-row"><label>文件名（可空）</label><input id="m_name" placeholder="xai-demo@example.com.json" /></div>
    <div class="form-row"><label>JSON 内容</label><textarea id="m_json" rows="14">{
  "type": "xai",
  "email": "demo@example.com",
  "access_token": "",
  "refresh_token": ""
}</textarea></div>
  `, async () => {
    let record;
    try { record = JSON.parse($("#m_json").value); }
    catch { throw new Error("JSON 无效"); }
    const name = $("#m_name").value.trim();
    const data = await api("/api/local-auth", {
      method: "POST",
      body: JSON.stringify({ name, record, overwrite: true }),
    });
    toast(`已写入 ${data.name}`, "ok");
    await loadLocalAuth();
    await openLocalAuth(data.name);
  });
}

async function saveLocalAuth() {
  if (!state.localName) return;
  let record;
  try { record = JSON.parse($("#localEditor").value); }
  catch { toast("JSON 无效", "error"); return; }
  await api(`/api/local-auth/${encodeURIComponent(state.localName)}`, {
    method: "PUT",
    body: JSON.stringify({ record }),
  });
  toast("本地 Auth 已保存", "ok");
  await loadLocalAuth();
}

async function deleteLocalAuth() {
  if (!state.localName) return;
  if (!confirm(`删除 ${state.localName}？`)) return;
  await api(`/api/local-auth/${encodeURIComponent(state.localName)}`, { method: "DELETE" });
  state.localName = "";
  $("#localDetailTitle").textContent = "Auth 详情";
  $("#localEditor").value = "";
  $("#localEditor").disabled = true;
  $("#btnLocalSave").disabled = true;
  $("#btnLocalUploadOne").disabled = true;
  $("#btnLocalDelete").disabled = true;
  toast("已删除", "ok");
  await loadLocalAuth();
}

async function uploadLocal(names) {
  const targetNames = names || state.localFiles.map((f) => f.name);
  if (!targetNames.length) { toast("无文件可上传", "error"); return; }
  await withBatchProgress({
    buttonId: names ? "btnLocalUploadChecked" : "btnLocalUploadAll",
    items: targetNames,
    action: async (name) => {
      const d = await api("/api/local-auth/upload", { method: "POST", body: JSON.stringify({ names: [name] }) });
      if (d.fail) throw new Error(d.details?.[0]?.error || "上传失败");
      return { uploaded: d.success || 0 };
    },
    labelFn: (name) => name,
    startLabel: "上传中",
    successLabel: "已上传",
    failLabel: "失败",
    delay: 200,
    onComplete: async ({ ok, fail }) => {
      toast(`上传完成: 成功 ${ok} / 失败 ${fail}`, fail > 0 ? "error" : "ok");
      await loadRemoteAuth();
    },
  });
}

// -------------------- Remote Auth CRUD --------------------
async function loadRemoteAuth() {
  try {
    const data = await api("/api/remote-auth");
    state.remoteFiles = data.files || [];
    $("#remoteAuthMeta").textContent = `${data.remote}\n共 ${state.remoteFiles.length} 个`;
    renderRemoteTable();
  } catch (e) {
    $("#remoteAuthMeta").textContent = String(e.message || e);
    $("#remoteAuthList").className = "table-wrap empty";
    $("#remoteAuthList").textContent = "查询失败，请检查远程配置";
  }
}

function renderRemoteTable() {
  const q = ($("#remoteSearch").value || "").toLowerCase();
  const files = state.remoteFiles.filter((f) => {
    const hay = `${f.email || ""} ${f.account || ""} ${f.name || ""} ${f.id || ""}`.toLowerCase();
    return !q || hay.includes(q);
  });
  const box = $("#remoteAuthList");
  if (!files.length) {
    box.className = "table-wrap empty";
    box.textContent = "远程暂无账号";
    return;
  }
  box.className = "table-wrap";
  box.innerHTML = `<table class="table"><thead><tr>
    <th style="width:42px"><input type="checkbox" id="remoteCheckAll" /></th>
    <th class="col-email">账号</th>
    <th style="width:90px">状态</th>
    <th style="width:100px">成功/失败</th>
    <th style="width:160px">更新时间</th>
    <th class="ops">操作</th>
  </tr></thead><tbody>
  ${files.map((f) => {
    const name = f.name || f.id || "";
    const email = f.email || f.account || name;
    const status = f.status || "-";
    return `<tr>
      <td><input type="checkbox" class="remote-check" value="${esc(name)}" /></td>
      <td class="col-email" title="${esc(email)} / ${esc(name)}">
        <div class="cell-clip">${esc(email)}</div>
        <div class="d mono cell-clip">${esc(name)}</div>
      </td>
      <td><span class="tag ${status === "active" ? "ok" : ""}">${esc(status)}</span></td>
      <td>${f.success ?? 0} / ${f.failed ?? 0}</td>
      <td class="mono"><span class="cell-clip">${esc(f.updated_at || f.modtime || "")}</span></td>
      <td class="ops"><button class="btn danger sm" data-del-remote="${esc(name)}">删除</button></td>
    </tr>`;
  }).join("")}
  </tbody></table>`;
  const all = $("#remoteCheckAll");
  if (all) {
    all.onchange = () => $$(".remote-check").forEach((c) => { c.checked = all.checked; });
  }
  box.querySelectorAll("[data-del-remote]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`删除远程 ${btn.dataset.delRemote}？`)) return;
      try {
        await api(`/api/remote-auth/${encodeURIComponent(btn.dataset.delRemote)}`, { method: "DELETE" });
        toast("远程已删除", "ok");
        await loadRemoteAuth();
      } catch (e) {
        toast(e.message || e, "error");
      }
    };
  });
}

function createRemoteAuth() {
  openModal("新建远程 Auth", `
    <div class="form-row"><label>文件名（可空）</label><input id="m_name" placeholder="xai-demo@example.com.json" /></div>
    <div class="form-row"><label>从本地文件粘贴 JSON</label><textarea id="m_json" rows="14" placeholder='{"type":"xai","email":"..."}'></textarea></div>
  `, async () => {
    let record;
    try { record = JSON.parse($("#m_json").value); }
    catch { throw new Error("JSON 无效"); }
    const name = $("#m_name").value.trim();
    await api("/api/remote-auth", { method: "POST", body: JSON.stringify({ name, record }) });
    toast("远程已写入", "ok");
    await loadRemoteAuth();
  });
}

async function batchDeleteRemote() {
  const names = $$(".remote-check:checked").map((c) => c.value).filter(Boolean);
  if (!names.length) {
    toast("请先勾选要删除的项", "error");
    return;
  }
  if (!confirm(`删除远程 ${names.length} 个账号？`)) return;

  await withBatchProgress({
    buttonId: "btnRemoteBatchDel",
    items: names,
    action: async (name) => {
      await api(`/api/remote-auth/${encodeURIComponent(name)}`, { method: "DELETE" });
      return { deleted: name };
    },
    labelFn: (name) => name,
    startLabel: "远程删除中",
    successLabel: "已删除",
    failLabel: "失败",
    delay: 150,
    onComplete: async ({ ok, fail }) => {
      toast(`批量删除: 成功 ${ok} / 失败 ${fail}`, fail > 0 ? "error" : "ok");
      await loadRemoteAuth();
    },
  });
}

// -------------------- 全局进度条（用于批量操作） --------------------
function showGlobalProgress(visible, state = "") {
  const wrap = $("#globalProgress");
  const fill = $("#globalProgressFill");
  const text = $("#globalProgressText");
  if (!wrap) return;
  wrap.className = `progress-wrap${visible ? " visible" : ""} ${state}`.trim();
  if (!visible) {
    fill.style.width = "0%";
    text.textContent = "";
  }
}

function updateGlobalProgress(current, total, label = "") {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  const fill = $("#globalProgressFill");
  const text = $("#globalProgressText");
  if (fill) fill.style.width = pct + "%";
  if (text) text.textContent = label || `${current} / ${total} (${pct}%)`;
}

async function withBatchProgress({
  buttonId,
  items,
  action,
  labelFn,
  startLabel = "处理中",
  successLabel = "完成",
  failLabel = "失败",
  delay = 0,
  autoHide = 4000,
  onComplete,
}) {
  const total = items.length;
  if (!total) return { ok: 0, fail: 0, results: [] };

  let ok = 0, fail = 0;
  const results = [];
  showGlobalProgress(true, "running");
  updateGlobalProgress(0, total, `${startLabel} 0/${total}…`);
  const btn = buttonId ? $(`#${buttonId}`) : null;
  if (btn) btn.disabled = true;

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const itemLabel = labelFn ? labelFn(item, i) : String(item);
    updateGlobalProgress(i, total, `${startLabel} ${i + 1}/${total} — ${itemLabel}`);
    try {
      const r = await action(item, i);
      results.push({ item, ...(r || {}), ok: !(r && r.error) });
      ok++;
    } catch (e) {
      results.push({ item, ok: false, error: e.message || String(e) });
      fail++;
      appendLog(`[UI] ${failLabel}: ${itemLabel} — ${e.message || e}`);
    }
    if (delay > 0 && i < items.length - 1) await new Promise((r) => setTimeout(r, delay));
  }

  if (btn) btn.disabled = false;
  showGlobalProgress(true, "done");
  updateGlobalProgress(total, total, fail > 0 ? `${successLabel} ${ok} / ${failLabel} ${fail}` : `${successLabel} ${ok} · 全部完成`);
  if (autoHide > 0) setTimeout(() => showGlobalProgress(false), autoHide);

  if (onComplete) await onComplete({ ok, fail, results });
  return { ok, fail, results };
}

// -------------------- Health Check --------------------
const STATUS_LABEL = {
  healthy: ["健康", "ok"],
  expiring_soon: ["即将过期", "warn"],
  refreshed: ["已刷新", "ok"],
  unauthorized: ["401 未授权", "fail"],
  expired: ["已过期", "fail"],
  quota_blocked: ["额度受限", "fail"],
  rate_limited: ["限流中", "warn"],
  disabled: ["已禁用", ""],
  error: ["错误", "fail"],
  invalid_file: ["文件损坏", "fail"],
  unknown: ["未知", ""],
  unchecked: ["待检测", ""],
};

function scoreColor(s) {
  if (s >= 80) return "ok";
  if (s >= 50) return "warn";
  return "fail";
}

function renderHealthSummary(summary) {
  if (!summary) return;
  $("#hlHealthy").textContent = (summary.healthy || 0) + (summary.refreshed || 0);
  $("#hlExpiring").textContent = (summary.expiring_soon || 0) + (summary.rate_limited || 0);
  $("#hlBad").textContent = (summary.unauthorized || 0) + (summary.expired || 0) + (summary.quota_blocked || 0) + (summary.error || 0);
  $("#hlScore").textContent = summary.avg_score ?? "-";
  $("#hlScore").className = `v ${scoreColor(summary.avg_score || 0)}`;
}

function renderHealthTable(results) {
  const q = ($("#healthSearch").value || "").toLowerCase();
  const items = (results || []).filter((r) => !q || `${r.email || ""} ${r.name || ""} ${r.status || ""}`.toLowerCase().includes(q));
  const box = $("#healthList");
  if (!items.length) {
    box.className = "table-wrap empty";
    box.textContent = results && results.length ? "无匹配结果" : "本地无账号文件";
    return;
  }
  box.className = "table-wrap";
  box.innerHTML = `<table class="table"><thead><tr>
    <th style="width:36px"><input type="checkbox" id="healthCheckAll" /></th>
    <th style="min-width:120px">账号</th>
    <th style="width:90px">状态</th>
    <th style="width:50px">分数</th>
    <th style="min-width:160px">信息</th>
    <th style="width:140px">过期时间</th>
    <th style="width:70px">剩余</th>
    <th style="width:60px">模型</th>
    <th style="width:70px">延迟</th>
    <th style="width:80px">操作</th>
  </tr></thead><tbody>
  ${items.map((r) => {
    const [label, tag] = STATUS_LABEL[r.status] || ["未知", ""];
    const exp = r.expiry || {};
    const probe = (r.probe || r).models || {};
    const left = exp.seconds_left;
    const leftStr = r.status === "unchecked" ? "-" : left == null ? "-" : left <= 0 ? "已过期" : left < 3600 ? `${Math.round(left/60)}m` : `${(left/3600).toFixed(1)}h`;
    const score = r.status === "unchecked" ? "-" : (r.score ?? 0);
    const scoreClass = r.status === "unchecked" ? "muted" : scoreColor(r.score || 0);
    const expiryAt = exp.expired_at || r.expired || "-";
    const modelCount = probe.model_count ?? r.model_count ?? "-";
    const latency = probe.latency_ms ?? r.latency_ms ?? "-";
    return `<tr>
      <td><input type="checkbox" class="health-check" value="${esc(r.name || "")}" /></td>
      <td>
        <div class="cell-nowrap" title="${esc(r.email || r.name)}">${esc(r.email || r.name)}</div>
        <div class="d mono cell-nowrap" style="max-width:180px">${esc(r.name)}</div>
      </td>
      <td><span class="tag ${tag}">${label}</span></td>
      <td><span class="${scoreClass}" style="font-weight:700">${score}</span></td>
      <td><span class="cell-nowrap d" style="max-width:200px" title="${esc(r.message || "")}">${esc(r.message || "-")}</span></td>
      <td class="mono">${esc(expiryAt)}</td>
      <td class="mono">${leftStr}</td>
      <td>${modelCount}</td>
      <td class="mono">${latency !== "-" ? latency + "ms" : "-"}</td>
      <td class="ops"><button class="btn ghost sm" data-health-one="${esc(r.name || "")}">检测</button></td>
    </tr>`;
  }).join("")}
  </tbody></table>`;
  const all = $("#healthCheckAll");
  if (all) {
    all.onchange = () => $$(".health-check").forEach((c) => { c.checked = all.checked; });
  }
  box.querySelectorAll("[data-health-one]").forEach((btn) => {
    btn.onclick = () => btn.dataset.healthOne && checkOneHealth(btn.dataset.healthOne);
  });
}

let healthResults = [];

async function loadHealth() {
  // 直接从 local-auth API 拉账号列表，未检测的显示为 unchecked 状态
  try {
    const data = await api("/api/local-auth");
    const files = data.files || [];
    if (files.length && (!healthResults.length || healthResults.every((r) => r.status === "unchecked"))) {
      healthResults = files.map((f) => ({
        name: f.name,
        email: f.email || "",
        status: "unchecked",
        score: 0,
        message: "未检测，点击操作列或「全部检测」",
        expiry: {},
        probe: {},
        quota: {},
      }));
      renderHealthSummary({ healthy: 0, expiring_soon: 0, refreshed: 0, unauthorized: 0, expired: 0, quota_blocked: 0, rate_limited: 0, error: 0, avg_score: "-" });
    }
  } catch {}
  renderHealthTable(healthResults);
}

function showProgress(visible, state = "") {
  const wrap = $("#healthProgress");
  const fill = $("#healthProgressFill");
  const text = $("#healthProgressText");
  wrap.className = `progress-wrap${visible ? " visible" : ""} ${state}`.trim();
  if (!visible) {
    fill.style.width = "0%";
    text.textContent = "就绪";
  }
}

function updateProgress(current, total, label = "") {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  $("#healthProgressFill").style.width = pct + "%";
  $("#healthProgressText").textContent = label || `${current} / ${total} (${pct}%)`;
}

function summaryFromResults() {
  const checked = healthResults.filter((x) => x.status !== "unchecked");
  return {
    healthy: checked.filter((x) => x.status === "healthy").length,
    refreshed: checked.filter((x) => x.status === "refreshed").length,
    expiring_soon: checked.filter((x) => x.status === "expiring_soon").length,
    rate_limited: checked.filter((x) => x.status === "rate_limited").length,
    unauthorized: checked.filter((x) => x.status === "unauthorized").length,
    expired: checked.filter((x) => x.status === "expired").length,
    quota_blocked: checked.filter((x) => x.status === "quota_blocked").length,
    error: checked.filter((x) => ["error", "invalid_file", "disabled"].includes(x.status)).length,
    avg_score: checked.length > 0 ? Math.round(checked.reduce((a, x) => a + (x.score || 0), 0) / checked.length) : 0,
  };
}

async function runHealthCheck(names) {
  const targets = names
    || healthResults.map((r) => r.name).filter(Boolean)
    || [];
  if (!targets.length) {
    toast("没有可检测的账号", "error");
    return;
  }

  const opts = {
    deep: $("#healthDeep").checked,
    autoRefresh: $("#healthAutoRefresh").checked,
    syncRemote: $("#healthSyncRemote").checked,
  };
  const total = targets.length;
  let done = 0;
  let failCount = 0;

  showProgress(true, "running");
  updateProgress(0, total, `检测中 0/${total}…`);
  $("#btnHealthCheck").disabled = true;

  for (const name of targets) {
    try {
      updateProgress(done, total, `检测中 ${done + 1}/${total} — ${name}`);
      const data = await api(`/api/health-check/${encodeURIComponent(name)}`, {
        method: "POST",
        body: JSON.stringify(opts),
      });
      const r = data.result;
      if (r) {
        const idx = healthResults.findIndex((x) => x.name === name);
        if (idx >= 0) healthResults[idx] = r; else healthResults.push(r);
      }
    } catch (e) {
      failCount++;
      appendLog(`[UI] 检测 ${name} 失败: ${e.message || e}`);
      const idx = healthResults.findIndex((x) => x.name === name);
      if (idx >= 0) {
        healthResults[idx] = {
          ...healthResults[idx],
          status: "error",
          score: 0,
          message: e.message || "检测失败",
        };
      }
    }
    done++;
    const s = summaryFromResults();
    renderHealthSummary(s);
    renderHealthTable(healthResults);
    updateProgress(done, total, `检测中 ${done}/${total}…`);
  }

  $("#btnHealthCheck").disabled = false;
  const s = summaryFromResults();
  renderHealthSummary(s);
  renderHealthTable(healthResults);

  if (failCount) {
    showProgress(true, "done");
    updateProgress(total, total, `完成（${failCount} 个失败）`);
  } else {
    showProgress(true, "done");
    updateProgress(total, total, `全部完成 · 均分 ${s.avg_score}`);
  }
  setTimeout(() => showProgress(false), 4000);

  // 全部检测完成后：自动勾选失效账号
  const FAIL_STATUSES = new Set(["unauthorized", "expired", "quota_blocked", "error", "invalid_file", "disabled"]);
  setTimeout(() => {
    $$(".health-check").forEach((c) => {
      const r = healthResults.find((x) => x.name === c.value);
      c.checked = r && FAIL_STATUSES.has(r.status);
    });
    // 同步全选框状态
    const all = $("#healthCheckAll");
    if (all) {
      const boxes = $$(".health-check");
      all.checked = boxes.length > 0 && boxes.every((c) => c.checked);
    }
  }, 100);

  const bad = s.unauthorized + s.expired + s.quota_blocked + s.error;
  toast(`检测完成: 健康 ${s.healthy + s.refreshed} / 有问题 ${bad} / 均分 ${s.avg_score}`, bad > 0 ? "error" : "ok");
  await loadLocalAuth();
}

async function checkOneHealth(name) {
  const body = {
    deep: $("#healthDeep").checked,
    autoRefresh: $("#healthAutoRefresh").checked,
    syncRemote: $("#healthSyncRemote").checked,
  };
  const data = await api(`/api/health-check/${encodeURIComponent(name)}`, { method: "POST", body: JSON.stringify(body) });
  const r = data.result;
  if (r) {
    const idx = healthResults.findIndex((x) => x.name === name);
    if (idx >= 0) healthResults[idx] = r; else healthResults.push(r);
    const s = {
      healthy: healthResults.filter((x) => x.status === "healthy").length,
      refreshed: healthResults.filter((x) => x.status === "refreshed").length,
      expiring_soon: healthResults.filter((x) => x.status === "expiring_soon").length,
      unauthorized: healthResults.filter((x) => x.status === "unauthorized").length,
      expired: healthResults.filter((x) => x.status === "expired").length,
      quota_blocked: healthResults.filter((x) => x.status === "quota_blocked").length,
      rate_limited: healthResults.filter((x) => x.status === "rate_limited").length,
      error: healthResults.filter((x) => ["error","invalid_file"].includes(x.status)).length,
      avg_score: Math.round(healthResults.filter((x) => x.status !== "unchecked").reduce((a, x) => a + (x.score || 0), 0) / Math.max(healthResults.filter((x) => x.status !== "unchecked").length, 1)),
    };
    renderHealthSummary(s);
    renderHealthTable(healthResults);
    toast(`${name}: ${r.message || r.status}`, (r.status === "healthy" || r.status === "refreshed") ? "ok" : "error");
  }
  await loadLocalAuth();
}

async function refreshCheckedHealth() {
  const names = $$(".health-check:checked").map((c) => c.value).filter(Boolean);
  if (!names.length) { toast("请先勾选账号", "error"); return; }

  await withBatchProgress({
    buttonId: "btnHealthRefresh",
    items: names,
    action: async (name) => {
      const data = await api(`/api/health-check/${encodeURIComponent(name)}`, {
        method: "POST",
        body: JSON.stringify({
          deep: false,
          autoRefresh: true,
          syncRemote: $("#healthSyncRemote").checked,
        }),
      });
      const r = data.result;
      if (r) {
        const idx = healthResults.findIndex((x) => x.name === name);
        if (idx >= 0) healthResults[idx] = r; else healthResults.push(r);
      }
      return r;
    },
    labelFn: (name) => name,
    startLabel: "刷新中",
    successLabel: "已刷新",
    failLabel: "失败",
    delay: 300,
    onComplete: async ({ ok, fail }) => {
      renderHealthTable(healthResults);
      renderHealthSummary(summaryFromResults());
      toast(`刷新完成: 成功 ${ok} / 失败 ${fail}`, fail > 0 ? "error" : "ok");
      await loadLocalAuth();
    },
  });
}

async function deleteCheckedHealth() {
  const FAIL_STATUSES = new Set(["unauthorized", "expired", "quota_blocked", "error", "invalid_file", "disabled"]);
  const allChecked = $$(".health-check:checked").map((c) => c.value).filter(Boolean);
  if (!allChecked.length) {
    toast("请先勾选要删除的账号", "error");
    return;
  }
  const failNames = allChecked.filter((n) => {
    const r = healthResults.find((x) => x.name === n);
    return r && FAIL_STATUSES.has(r.status);
  });
  let targets = failNames;
  if (!failNames.length) {
    if (!confirm(`勾选的 ${allChecked.length} 个账号均为健康/未检测状态，确认仍然删除？`)) return;
    targets = allChecked;
  } else if (failNames.length < allChecked.length) {
    if (!confirm(`仅 ${failNames.length}/${allChecked.length} 个为失效状态，只删除这 ${failNames.length} 个？\n（取消则不删除任何账号）`)) return;
    targets = failNames;
  } else {
    if (!confirm(`确认删除 ${failNames.length} 个失效账号（本地 + 远程）？`)) return;
  }

  const total = targets.length;
  let done = 0;
  let okCount = 0;
  let failCount = 0;

  showProgress(true, "running");
  updateProgress(0, total, `删除中 0/${total}…`);
  $("#btnHealthDeleteFailed").disabled = true;

  // 逐个调用本地删除，远程删除在后端 API 内已完成
  // 分批 5 个一组调用，每组完成后更新进度
  const BATCH = 5;
  for (let i = 0; i < targets.length; i += BATCH) {
    const batch = targets.slice(i, i + BATCH);
    updateProgress(done, total, `删除中 ${done + 1}-${Math.min(done + batch.length, total)}/${total}…`);
    try {
      const data = await api("/api/health-delete", {
        method: "POST",
        body: JSON.stringify({ names: batch, deleteRemote: true }),
      });
      okCount += data.success || 0;
      failCount += data.fail || 0;
    } catch (e) {
      failCount += batch.length;
      appendLog(`[UI] 批量删除失败: ${e.message || e}`);
    }
    done += batch.length;
    const deleted = new Set(batch);
    healthResults = healthResults.filter((r) => !deleted.has(r.name));
    renderHealthTable(healthResults);
    renderHealthSummary(summaryFromResults());
    updateProgress(done, total, `删除中 ${done}/${total}…`);
  }

  $("#btnHealthDeleteFailed").disabled = false;
  if (failCount) {
    showProgress(true, "done");
    updateProgress(total, total, `完成（${failCount} 个失败）`);
  } else {
    showProgress(true, "done");
    updateProgress(total, total, `全部完成 · 删除 ${okCount} 个`);
  }
  setTimeout(() => showProgress(false), 4000);

  toast(`删除完成: 成功 ${okCount} / 失败 ${failCount}`, failCount > 0 ? "error" : "ok");
  await loadLocalAuth();
}

async function ssoReloginChecked() {
  const checked = $$(".health-check:checked").map((c) => c.value).filter(Boolean);
  const names = checked.length ? checked : undefined;  // 不勾选 = 全部
  const total = checked.length || healthResults.length;
  if (!total) { toast("无账号可操作", "error"); return; }

  const isBatch = !checked.length;
  if (isBatch) {
    if (!confirm(`将对所有账号尝试重新登录（refresh → 原始 SSO device flow），确认？`)) return;
  } else {
    if (!confirm(`将对勾选的 ${checked.length} 个账号尝试重新登录，确认？`)) return;
  }

  showProgress(true, "running");
  updateProgress(0, total, `重新登录中…`);
  $("#btnHealthRelogin").disabled = true;

  const data = await api("/api/sso-relogin", {
    method: "POST",
    body: JSON.stringify({ names, syncRemote: true }),
  }).catch((e) => {
    toast(e.message || e, "error");
    return null;
  });

  $("#btnHealthRelogin").disabled = false;
  if (!data) { showProgress(false); return; }

  const { success = 0, fail = 0, skip = 0, details = [] } = data;
  showProgress(true, "done");
  updateProgress(total, total, `完成: 成功 ${success} / 跳过 ${skip} / 失败 ${fail}`);
  setTimeout(() => showProgress(false), 5000);

  // 更新表格里对应行的状态
  const byName = new Map((details || []).map((d) => [d.name, d]));
  for (const d of details) {
    const idx = healthResults.findIndex((x) => x.name === d.name);
    if (idx >= 0) {
      healthResults[idx] = {
        ...healthResults[idx],
        status: d.ok ? "refreshed" : healthResults[idx].status,
        message: d.message || healthResults[idx].message,
      };
    }
  }
  renderHealthTable(healthResults);
  renderHealthSummary(summaryFromResults());

  toast(`重新登录: 成功 ${success} / 跳过 ${skip} / 失败 ${fail}`, fail > 0 ? "error" : "ok");
  await runHealthCheck(names || undefined);
}

async function ssoExport() {
  const data = await api("/api/sso-export?filter=all");
  const lines = data.lines || [];
  if (!lines.length) {
    toast("无可导出的 SSO", "error");
    return;
  }

  // 弹窗预览 + 下载
  const preview = lines.slice(0, 50).map((l) => {
    const parts = l.split("----");
    const email = parts[0] || "";
    const sso = parts[1] || "";
    const masked = sso.length > 16 ? sso.slice(0, 8) + "…" + sso.slice(-8) : sso;
    return `${email}  →  ${masked}`;
  }).join("\n");
  const more = lines.length > 50 ? `\n…共 ${lines.length} 条` : "";

  openModal(`导出 SSO（${lines.length} 条）`, `
    <div class="meta" style="max-height:300px;overflow:auto;white-space:pre;font-size:12px;line-height:1.5">
${esc(preview)}${esc(more)}
    </div>
    <p class="hint">下载完整文件请点击「下载」，内容格式为 <code>email----sso</code></p>
  `, async () => {
    // 触发下载
    const blob = new Blob([lines.join("\n") + "\n"], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `sso_export_${new Date().toISOString().slice(0, 10)}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast(`已下载 ${lines.length} 条 SSO`, "ok");
  });
}

// Logs / Tabs --------------------
function connectLogs() {
  if (state.es) state.es.close();
  const es = new EventSource("/api/logs/stream");
  state.es = es;
  $("#connState").textContent = "日志流连接中…";
  es.onopen = () => { $("#connState").textContent = "日志流已连接"; };
  es.onerror = () => { $("#connState").textContent = "日志流重连中…"; };
  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.ping) return;
      if (data.line) appendLog(data.line);
    } catch {}
  };
}

function setupTabs() {
  $$(".nav-item").forEach((btn) => {
    btn.onclick = () => {
      $$(".nav-item").forEach((b) => b.classList.remove("active"));
      $$(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
      const meta = TABS[btn.dataset.tab] || ["", ""];
      $("#pageTitle").textContent = meta[0];
      $("#pageDesc").textContent = meta[1];
    };
  });
}

function bind() {
  setupTabs();
  applyTheme(getTheme());
  $("#btnTheme").onclick = toggleTheme;
  $("#btnStart").onclick = () => startRegister().catch((e) => toast(e.message || e, "error"));
  $("#btnStop").onclick = () => stopRegister().catch((e) => toast(e.message || e, "error"));
  $("#btnClearLog").onclick = () => { state.logLines = []; $("#logView").textContent = ""; };
  $("#btnBottom").onclick = () => { const v = $("#logView"); v.scrollTop = v.scrollHeight; };
  $("#btnSaveCfg").onclick = () => saveConfig().catch((e) => toast(e.message || e, "error"));
  $("#btnReloadCfg").onclick = () => loadConfig().catch((e) => toast(e.message || e, "error"));
  $("#btnRefreshAll").onclick = () => refreshAll().catch((e) => toast(e.message || e, "error"));

  $("#btnAccCreate").onclick = createAccountFile;
  $("#btnAccConvertAll").onclick = () => convertAllAccountsToJson().catch((e) => toast(e.message || e, "error"));
  $("#btnAccRefresh").onclick = () => loadAccounts().catch((e) => toast(e.message || e, "error"));
  $("#btnAccConvertJson").onclick = convertAccountToJson;
  $("#btnAccAddRow").onclick = addAccountRow;
  $("#btnAccSaveAll").onclick = () => saveAllAccountRows().catch((e) => toast(e.message || e, "error"));
  $("#btnAccDelFile").onclick = () => deleteAccountFile().catch((e) => toast(e.message || e, "error"));
  $("#accSearch").oninput = () => loadAccounts().catch(() => {});

  $("#btnLocalCreate").onclick = createLocalAuth;
  $("#btnLocalRefresh").onclick = () => loadLocalAuth().catch((e) => toast(e.message || e, "error"));
  $("#btnLocalSave").onclick = () => saveLocalAuth().catch((e) => toast(e.message || e, "error"));
  $("#btnLocalDelete").onclick = () => deleteLocalAuth().catch((e) => toast(e.message || e, "error"));
  $("#btnLocalUploadOne").onclick = () => uploadLocal(state.localName ? [state.localName] : null).catch((e) => toast(e.message || e, "error"));
  $("#btnLocalUploadAll").onclick = () => uploadLocal(null).catch((e) => toast(e.message || e, "error"));
  $("#btnLocalUploadChecked").onclick = () => uploadCheckedLocal().catch((e) => toast(e.message || e, "error"));
  $("#localSearch").oninput = () => loadLocalAuth().catch(() => {});

  $("#btnRemoteCreate").onclick = createRemoteAuth;
  $("#btnRemoteRefresh").onclick = () => loadRemoteAuth().catch((e) => toast(e.message || e, "error"));
  $("#btnRemoteBatchDel").onclick = () => batchDeleteRemote().catch((e) => toast(e.message || e, "error"));
  $("#remoteSearch").oninput = renderRemoteTable;

  $("#btnHealthCheck").onclick = () => runHealthCheck(null).catch((e) => toast(e.message || e, "error"));
  $("#btnHealthRefresh").onclick = () => refreshCheckedHealth().catch((e) => toast(e.message || e, "error"));
  $("#btnHealthRelogin").onclick = () => ssoReloginChecked().catch((e) => toast(e.message || e, "error"));
  $("#btnHealthDeleteFailed").onclick = () => deleteCheckedHealth().catch((e) => toast(e.message || e, "error"));
  $("#btnSsoExport").onclick = () => ssoExport().catch((e) => toast(e.message || e, "error"));
  $("#healthSearch").oninput = () => renderHealthTable(healthResults);

  $("#modalClose").onclick = closeModal;
  $("#modalCancel").onclick = closeModal;
  $("#modalOk").onclick = async () => {
    if (!state.modalHandler) return closeModal();
    try {
      await state.modalHandler();
      closeModal();
    } catch (e) {
      toast(e.message || e, "error");
    }
  };
  $("#modal").addEventListener("click", (e) => {
    if (e.target.id === "modal") closeModal();
  });
}

async function refreshAll() {
  await Promise.all([
    loadConfig(),
    refreshStatus(),
    loadAccounts(),
    loadLocalAuth(),
    loadRemoteAuth(),
    loadHealth(),
  ]);
  toast("已刷新", "ok");
}

async function boot() {
  bind();
  connectLogs();
  await refreshAll().catch((e) => appendLog(`[UI] 初始化失败: ${e.message || e}`));
  setInterval(() => refreshStatus().catch(() => {}), 3000);
}

boot();
