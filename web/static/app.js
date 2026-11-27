const $ = (sel) => document.querySelector(sel);

let qrPollTimer = null;
let currentQrSessionId = null;
let reauthPollTimer = null;
let reauthQrSessionId = null;
let reauthTargetInstanceId = null;

let cachedApiItems = [];
let cachedOverview = null;
let cachedSessions = [];
let activeSessionId = null;
let cachedCourses = [];
let cachedInstances = [];
let monitorEventSource = null;
let logPollTimer = null;
let activeLogInstanceId = null;

function showToast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.style.background = isError ? "var(--danger)" : "#323232";
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3200);
}

async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok && !data.error && !data.msg) {
    throw new Error(`HTTP ${resp.status}`);
  }
  return { ok: resp.ok, status: resp.status, data };
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------- Tabs & Modals ----------------
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
  });
});

function stopInstanceQrPolling() {
  if (qrPollTimer) {
    clearInterval(qrPollTimer);
    qrPollTimer = null;
  }
  currentQrSessionId = null;
}

function stopReauthPolling() {
  if (reauthPollTimer) {
    clearInterval(reauthPollTimer);
    reauthPollTimer = null;
  }
  reauthQrSessionId = null;
}

function closeModalById(id) {
  $(`#${id}`).classList.add("hidden");
  if (id === "instance-modal") {
    stopInstanceQrPolling();
  } else if (id === "reauth-modal") {
    stopReauthPolling();
    reauthTargetInstanceId = null;
  } else if (id === "log-modal") {
    activeLogInstanceId = null;
    if (logPollTimer) {
      clearInterval(logPollTimer);
      logPollTimer = null;
    }
  }
}

document.querySelectorAll("[data-close-modal]").forEach((el) => {
  el.addEventListener("click", () => closeModalById(el.dataset.closeModal));
});

// ---------------- Real-time SSE Monitor ----------------
function renderSystemOverview(data) {
  if (!data) return;
  cachedOverview = data;
  const hw = data.hardware;
  const th = data.thresholds;
  const guard = data.safety_guard;

  $("#cpu-text").textContent = `${hw.cpu_percent}%`;
  $("#cpu-bar").style.width = `${Math.min(100, hw.cpu_percent)}%`;
  $("#mem-text").textContent = `${hw.mem_percent}%`;
  $("#mem-bar").style.width = `${Math.min(100, hw.mem_percent)}%`;
  $("#running-count-text").textContent = `${guard.running_containers} / ${th.max_containers}`;

  $("#hw-cpu-cores").textContent = `${hw.cpu_count_logical} 核`;
  $("#hw-cpu-limit").textContent = `${hw.cpu_percent}% / ${th.max_cpu_percent}%`;
  $("#hw-mem-detail").textContent = `${hw.mem_available_gb} / ${hw.mem_total_gb} GB`;
  $("#hw-cfg-max").textContent = `${guard.running_containers} / ${th.max_containers}`;

  const badge = $("#safety-guard-badge");
  if (guard.allowed) {
    badge.className = "chip chip-ok";
    badge.textContent = "安全阈值正常";
  } else {
    badge.className = "chip chip-danger";
    badge.textContent = guard.reasons.join("；");
  }
}

function statusChipHtml(status) {
  const map = {
    running: ["运行中", "chip-info"],
    starting: ["启动中", "chip-info"],
    completed: ["已完成", "chip-ok"],
    stopped: ["已停止", "chip-warn"],
    exited: ["已退出", "chip-warn"],
    error: ["异常", "chip-danger"],
    idle: ["未启动", "chip-neutral"],
  };
  const [label, cls] = map[status] || [status || "未知", "chip-neutral"];
  return `<span class="chip ${cls}">${escapeHtml(label)}</span>`;
}

function renderInstances(items) {
  cachedInstances = items || [];
  const completedCount = cachedInstances.filter((x) => x.runtime_status === "completed").length;
  const clearBtn = $("#btn-clear-completed");
  if (clearBtn) {
    clearBtn.disabled = completedCount === 0;
    clearBtn.textContent = completedCount > 0 ? `清理已完成 (${completedCount})` : "清理已完成";
  }

  const box = $("#instance-list");
  if (!cachedInstances.length) {
    box.innerHTML = `<div class="card" style="grid-column: 1 / -1;"><div class="empty-hint">暂无任务，点击右上角「+ 新建任务」扫码选课</div></div>`;
    return;
  }

  box.innerHTML = cachedInstances
    .map((inst) => {
      const prog = inst.progress || {};
      const pct = Number(prog.overall_percent ?? 0);
      const isRunning = inst.runtime_status === "running";
      const pidLabel = inst.pid ? `PID ${inst.pid}` : `ID ${inst.id}`;

      const curCourse = prog.current_course
        ? `${prog.current_course.index || 1}/${prog.courses_total || inst.courses.length} · ${prog.current_course.title}`
        : `共 ${prog.courses_total || inst.courses.length} 门课程`;

      const chTotal = prog.chapters_total || 0;
      const chDone = prog.chapters_completed || 0;
      const curChapter = chTotal
        ? `${chDone}/${chTotal}${prog.current_chapter ? ` · ${prog.current_chapter}` : ""}`
        : prog.current_chapter || "-";

      const job = prog.current_job;
      const jobText = job ? `${job.name || job.type} (${job.detail || `${job.percent}%`})` : "-";

      const resChip = isRunning
        ? `<span class="chip chip-neutral">CPU ${inst.cpu_percent}% · ${inst.mem_mb} MB</span>`
        : "";

      return `
        <div class="inst-card">
          <div class="inst-card-head">
            <div>
              <div class="inst-title">
                <span>${escapeHtml(inst.name)}</span>
                ${statusChipHtml(inst.runtime_status)}
              </div>
              <div class="inst-sub"><code>${escapeHtml(pidLabel)}</code></div>
            </div>
            <div class="progress-pct">${pct.toFixed(1)}%</div>
          </div>

          <div class="progress-wrap">
            <div class="progress-track">
              <div class="progress-fill" style="width: ${Math.min(100, Math.max(0, pct))}%"></div>
            </div>
            <span class="hint-inline">${escapeHtml(prog.message || "等待调度")}</span>
          </div>

          <div class="inst-details">
            <div>课程：<strong>${escapeHtml(curCourse)}</strong></div>
            <div>章节：<strong>${escapeHtml(curChapter)}</strong></div>
            <div>任务点：<strong>${escapeHtml(jobText)}</strong></div>
          </div>

          <div class="inst-tags">
            <span class="chip chip-neutral">${escapeHtml(inst.api_profile_name)}</span>
            <span class="chip chip-neutral">${inst.speed}x · ${inst.jobs}并发</span>
            ${resChip}
          </div>

          <div class="card-actions">
            ${
              isRunning
                ? `<button class="btn btn-danger btn-sm" onclick="stopInstance('${inst.id}')">停止</button>`
                : `<button class="btn btn-primary btn-sm" onclick="startInstance('${inst.id}')">启动</button>`
            }
            <button class="btn btn-outlined btn-sm" onclick="openReauthModal('${inst.id}', '${escapeHtml(inst.name)}')">登录</button>
            <button class="btn btn-outlined btn-sm" onclick="openInstanceLogs('${inst.id}', '${escapeHtml(inst.name)}')">日志</button>
            <button class="btn btn-text btn-sm" onclick="deleteInstance('${inst.id}')">删除</button>
          </div>
        </div>
      `;
    })
    .join("");
}

function connectMonitorStream() {
  if (monitorEventSource) {
    monitorEventSource.close();
    monitorEventSource = null;
  }
  const sseBadge = $("#sse-badge");
  sseBadge.textContent = "连接中";
  sseBadge.className = "chip chip-warn";

  const es = new EventSource("/api/stream/monitor");
  monitorEventSource = es;

  es.onopen = () => {
    sseBadge.textContent = "实时";
    sseBadge.className = "chip chip-ok";
  };

  es.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      renderSystemOverview(payload.overview);
      renderInstances(payload.instances);
    } catch (e) {}
  };

  es.onerror = () => {
    sseBadge.textContent = "重连中";
    sseBadge.className = "chip chip-warn";
  };
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (monitorEventSource) {
      monitorEventSource.close();
      monitorEventSource = null;
    }
  } else {
    connectMonitorStream();
  }
});

// ---------------- Thresholds Modal ----------------
$("#btn-open-thresholds").addEventListener("click", () => {
  if (!cachedOverview) return;
  const { hardware: hw, thresholds: th } = cachedOverview;
  $("#threshold-rec-hint").textContent =
    `当前主机 ${hw.cpu_count_logical} 核 / ${hw.mem_total_gb} GB，推荐并发上限 ${hw.recommended_max_containers}`;
  $("#th-max-containers").value = th.max_containers;
  $("#th-max-cpu").value = th.max_cpu_percent;
  $("#th-max-mem").value = th.max_mem_percent;
  $("#threshold-modal").classList.remove("hidden");
});

$("#threshold-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    max_containers: Number($("#th-max-containers").value),
    max_cpu_percent: Number($("#th-max-cpu").value),
    max_mem_percent: Number($("#th-max-mem").value),
  };
  const { ok, data } = await apiFetch("/api/system/thresholds", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  if (!ok || !data.ok) {
    showToast(data.error || "保存失败", true);
    return;
  }
  closeModalById("threshold-modal");
  if (data.overview) renderSystemOverview(data.overview);
  showToast("阈值已更新");
});

// ---------------- AI API Management ----------------
async function loadAiApis() {
  const { data } = await apiFetch("/api/ai-apis");
  cachedApiItems = data.items || [];
  const box = $("#api-list");
  if (!cachedApiItems.length) {
    box.innerHTML = `<div class="card" style="grid-column: 1 / -1;"><div class="empty-hint">暂无 AI API 配置，点击右上角「+ 添加 API」</div></div>`;
    return;
  }

  box.innerHTML = cachedApiItems
    .map((item) => {
      const defChip = item.is_default ? `<span class="chip chip-ok">默认</span>` : "";
      return `
        <div class="api-card ${item.is_default ? "is-default" : ""}">
          <div class="api-card-head">
            <span class="api-card-title">${escapeHtml(item.name)}</span>
            ${defChip}
          </div>
          <div class="api-meta">
            <div>模型：<code>${escapeHtml(item.model)}</code></div>
            <div>地址：${escapeHtml(item.endpoint)}</div>
            <div>Key：<code>${escapeHtml(item.key_masked)}</code></div>
            <div>策略：${item.submit ? "自动提交" : "仅保存"} · 覆盖率 ${Math.round(item.cover_rate * 100)}% · ${item.use_cache ? "缓存开" : "缓存关"}</div>
          </div>
          <div class="card-actions">
            <button class="btn btn-tonal btn-sm" onclick="testAiApi('${item.id}', this)">测试</button>
            ${item.is_default ? "" : `<button class="btn btn-outlined btn-sm" onclick="setDefaultApi('${item.id}')">设为默认</button>`}
            <button class="btn btn-outlined btn-sm" onclick="openEditApi('${item.id}')">编辑</button>
            <button class="btn btn-text btn-sm" onclick="deleteAiApi('${item.id}')">删除</button>
          </div>
        </div>
      `;
    })
    .join("");
}

function openApiModal(item = null) {
  $("#api-modal-title").textContent = item ? "编辑 AI API" : "添加 AI API";
  $("#api-id").value = item ? item.id : "";
  $("#api-name").value = item ? item.name : "";
  $("#api-endpoint").value = item ? item.endpoint : "https://api.deepseek.com/v1";
  $("#api-key").value = "";
  $("#api-key").required = !item;
  $("#api-key-hint").textContent = item ? `(当前 ${item.key_masked}，留空不变)` : "";
  $("#api-model").value = item ? item.model : "deepseek-chat";
  $("#api-min-interval").value = item ? item.min_interval_seconds : 3;
  $("#api-cover-rate").value = item ? item.cover_rate : 0.9;
  $("#api-delay").value = item ? item.delay : 1.0;
  $("#api-proxy").value = item ? item.http_proxy || "" : "";
  $("#api-submit").checked = item ? Boolean(item.submit) : true;
  $("#api-use-cache").checked = item ? Boolean(item.use_cache) : true;
  $("#api-is-default").checked = item ? Boolean(item.is_default) : cachedApiItems.length === 0;
  $("#api-modal").classList.remove("hidden");
}

$("#btn-add-api").addEventListener("click", () => openApiModal(null));
$("#btn-preset-deepseek").addEventListener("click", () => {
  if (!$("#api-name").value.trim()) $("#api-name").value = "DeepSeek";
  $("#api-endpoint").value = "https://api.deepseek.com/v1";
  $("#api-model").value = "deepseek-chat";
});

window.openEditApi = (id) => {
  const item = cachedApiItems.find((x) => x.id === id);
  if (item) openApiModal(item);
};

window.deleteAiApi = async (id) => {
  if (!confirm("确认删除该 AI API？")) return;
  const { ok, data } = await apiFetch(`/api/ai-apis/${id}`, { method: "DELETE" });
  if (!ok || !data.ok) {
    showToast(data.error || "删除失败", true);
    return;
  }
  showToast("已删除");
  loadAiApis();
};

window.setDefaultApi = async (id) => {
  const { ok, data } = await apiFetch(`/api/ai-apis/${id}/default`, { method: "POST" });
  if (!ok || !data.ok) {
    showToast(data.error || "设置失败", true);
    return;
  }
  showToast("已设为默认");
  loadAiApis();
};

window.testAiApi = async (id, btnEl) => {
  const old = btnEl.textContent;
  btnEl.disabled = true;
  btnEl.textContent = "测试中...";
  try {
    const { data } = await apiFetch(`/api/ai-apis/${id}/test`, { method: "POST" });
    showToast(data.msg || (data.ok ? "连接成功" : "连接失败"), !data.ok);
  } finally {
    btnEl.disabled = false;
    btnEl.textContent = old;
  }
};

$("#api-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#api-id").value.trim();
  const payload = {
    name: $("#api-name").value.trim(),
    endpoint: $("#api-endpoint").value.trim(),
    key: $("#api-key").value.trim(),
    model: $("#api-model").value.trim(),
    min_interval_seconds: Number($("#api-min-interval").value),
    cover_rate: Number($("#api-cover-rate").value),
    delay: Number($("#api-delay").value),
    http_proxy: $("#api-proxy").value.trim(),
    submit: $("#api-submit").checked,
    use_cache: $("#api-use-cache").checked,
    is_default: $("#api-is-default").checked,
  };
  const url = id ? `/api/ai-apis/${id}` : "/api/ai-apis";
  const method = id ? "PUT" : "POST";
  const { ok, data } = await apiFetch(url, { method, body: JSON.stringify(payload) });
  if (!ok || !data.ok) {
    showToast(data.error || "保存失败", true);
    return;
  }
  closeModalById("api-modal");
  showToast("已保存");
  loadAiApis();
});

// ---------------- Multi-Cookie Session + Unified QR + Course Picker ----------------
function renderSessionChips() {
  const box = $("#session-chips");
  if (!cachedSessions.length) {
    box.innerHTML = `<span class="hint-inline">暂无已保存账号，请扫码登录</span>`;
    return;
  }
  box.innerHTML = cachedSessions
    .map((s) => {
      const isActive = s.id === activeSessionId;
      const text = s.nickname ? `${s.nickname} (${s.uid || s.id})` : s.label || s.id;
      return `
        <span class="session-chip ${isActive ? "active" : ""}" onclick="selectSavedSession('${s.id}')">
          <span>${escapeHtml(text)}</span>
          <button type="button" class="session-chip-del" title="删除会话" onclick="event.stopPropagation(); removeSavedSession('${s.id}')">&times;</button>
        </span>
      `;
    })
    .join("");
}

async function refreshSessionsList() {
  const { data } = await apiFetch("/api/sessions");
  cachedSessions = data.items || [];
  if (activeSessionId && !cachedSessions.some((s) => s.id === activeSessionId)) {
    activeSessionId = null;
  }
  renderSessionChips();
}

function showLoggedInSummary(session) {
  stopInstanceQrPolling();
  $("#inst-qr-panel").classList.add("hidden");
  $("#inst-paste-panel").classList.add("hidden");

  const label = session.nickname ? `${session.nickname} (UID: ${session.uid || session.id})` : session.label || session.id;
  $("#inst-auth-summary-text").textContent = `✓ ${label}`;
  $("#inst-auth-summary").classList.remove("hidden");
}

function showQrLoginPanel() {
  $("#inst-auth-summary").classList.add("hidden");
  $("#inst-paste-panel").classList.add("hidden");
  $("#inst-qr-panel").classList.remove("hidden");
  startInstanceQrLogin();
}

function setInstQrStatus(text, tone = "warn") {
  const el = $("#inst-qr-status");
  el.textContent = text;
  el.className = "banner";
  if (tone === "ok") el.classList.add("banner-ok");
  else if (tone === "warn") el.classList.add("banner-warn");
  else if (tone === "err") el.classList.add("banner-err");
}

async function startInstanceQrLogin() {
  stopInstanceQrPolling();
  $("#inst-qr-img").classList.add("hidden");
  $("#inst-qr-placeholder").classList.remove("hidden");
  $("#inst-qr-placeholder").textContent = "获取中...";
  setInstQrStatus("正在获取二维码...", "warn");

  const { ok, data } = await apiFetch("/api/auth/qr/create", { method: "POST" });
  if (!ok || !data.ok) {
    $("#inst-qr-placeholder").textContent = "获取失败";
    setInstQrStatus(data.error || "获取失败", "err");
    return;
  }

  currentQrSessionId = data.session_id;
  $("#inst-qr-img").src = data.qr_image;
  $("#inst-qr-img").classList.remove("hidden");
  $("#inst-qr-placeholder").classList.add("hidden");
  setInstQrStatus("使用学习通 App 扫码", "warn");

  qrPollTimer = setInterval(async () => {
    if (!currentQrSessionId) return;
    try {
      const { data: poll } = await apiFetch(`/api/auth/qr/poll/${currentQrSessionId}`);
      if (poll.status === "waiting") {
        setInstQrStatus(poll.msg || "等待扫码...", "warn");
      } else if (poll.status === "scanned") {
        const who = poll.nickname ? ` (${poll.nickname})` : "";
        setInstQrStatus(`已扫码${who}，请在手机确认`, "warn");
      } else if (poll.status === "confirmed") {
        stopInstanceQrPolling();
        await refreshSessionsList();
        const saved = poll.saved_session || (cachedSessions.length ? cachedSessions[0] : null);
        if (saved) {
          activeSessionId = saved.id;
          renderSessionChips();
          showLoggedInSummary(saved);
          showToast("登录成功，正在加载课程");
          fetchCoursesForActiveSession();
        } else {
          $("#inst-qr-panel").classList.add("hidden");
        }
      } else if (poll.status === "expired" || poll.status === "cancelled") {
        stopInstanceQrPolling();
        setInstQrStatus(poll.msg || "二维码已失效，请刷新", "err");
      }
    } catch (err) {
      stopInstanceQrPolling();
      setInstQrStatus(`轮询异常: ${err.message}`, "err");
    }
  }, 2000);
}

window.selectSavedSession = async (sessionId) => {
  activeSessionId = sessionId;
  renderSessionChips();
  const sess = cachedSessions.find((s) => s.id === sessionId);
  if (sess) {
    showLoggedInSummary(sess);
    fetchCoursesForActiveSession();
  }
};

window.removeSavedSession = async (sessionId) => {
  const { ok, data } = await apiFetch(`/api/sessions/${sessionId}`, { method: "DELETE" });
  if (!ok || !data.ok) {
    showToast(data.error || "删除失败", true);
    return;
  }
  if (activeSessionId === sessionId) {
    activeSessionId = null;
    cachedCourses = [];
    renderCourseList("");
  }
  await refreshSessionsList();
  if (cachedSessions.length && !activeSessionId) {
    window.selectSavedSession(cachedSessions[0].id);
  } else if (!cachedSessions.length) {
    showQrLoginPanel();
  }
};

$("#btn-inst-qr-new").addEventListener("click", showQrLoginPanel);
$("#btn-inst-switch-qr").addEventListener("click", showQrLoginPanel);
$("#btn-inst-qr-refresh").addEventListener("click", startInstanceQrLogin);

$("#btn-inst-paste-toggle").addEventListener("click", () => {
  stopInstanceQrPolling();
  $("#inst-qr-panel").classList.add("hidden");
  $("#inst-paste-panel").classList.toggle("hidden");
});

$("#btn-inst-paste-save").addEventListener("click", async () => {
  const cookie = $("#inst-paste-cookie").value.trim();
  const label = $("#inst-paste-label").value.trim();
  if (!cookie) {
    showToast("请粘贴 Cookie", true);
    return;
  }
  const { ok, data } = await apiFetch("/api/sessions/import", {
    method: "POST",
    body: JSON.stringify({ cookie, label }),
  });
  if (!ok || !data.ok) {
    showToast(data.error || "导入失败", true);
    return;
  }
  $("#inst-paste-cookie").value = "";
  $("#inst-paste-label").value = "";
  await refreshSessionsList();
  if (data.session) {
    activeSessionId = data.session.id;
    renderSessionChips();
    showLoggedInSummary(data.session);
    if (data.valid) {
      showToast("导入成功");
      fetchCoursesForActiveSession();
    } else {
      showToast(`已保存，但校验异常：${data.msg}`, true);
    }
  }
});

function renderCourseList(filterText = "") {
  const box = $("#course-list-box");
  if (!cachedCourses.length) {
    box.innerHTML = `<div class="empty-hint">${activeSessionId ? "暂无可选课程" : "请先扫码或选择账号"}</div>`;
    return;
  }
  const q = filterText.trim().toLowerCase();
  const matched = q
    ? cachedCourses.filter(
        (c) =>
          (c.title || "").toLowerCase().includes(q) ||
          (c.teacher || "").toLowerCase().includes(q) ||
          String(c.courseId || "").includes(q)
      )
    : cachedCourses;

  if (!matched.length) {
    box.innerHTML = `<div class="empty-hint">无匹配课程</div>`;
    return;
  }

  box.innerHTML = matched
    .map(
      (c) => `
      <label class="course-item">
        <input type="checkbox" class="course-chk" value="${escapeHtml(c.courseId)}" data-title="${escapeHtml(c.title)}" />
        <div class="course-item-meta">
          <span>${escapeHtml(c.title)}</span>
          <span class="course-item-sub">ID: ${escapeHtml(c.courseId)} · ${escapeHtml(c.teacher || "-")}</span>
        </div>
      </label>
    `
    )
    .join("");
}

async function fetchCoursesForActiveSession() {
  const box = $("#course-list-box");
  if (!activeSessionId) {
    box.innerHTML = `<div class="empty-hint">请先扫码或选择账号</div>`;
    return;
  }
  box.innerHTML = `<div class="empty-hint">加载课程中...</div>`;
  const { ok, data } = await apiFetch(`/api/sessions/${activeSessionId}/courses`);
  if (!ok || !data.ok) {
    cachedCourses = [];
    box.innerHTML = `<div class="banner banner-err">${escapeHtml(data.error || "拉取失败，请重新扫码")}</div>`;
    return;
  }
  cachedCourses = data.courses || [];
  renderCourseList($("#course-search").value);
}

$("#btn-fetch-courses").addEventListener("click", fetchCoursesForActiveSession);
$("#course-search").addEventListener("input", (e) => renderCourseList(e.target.value));
$("#btn-courses-all").addEventListener("click", () => {
  document.querySelectorAll(".course-chk").forEach((c) => (c.checked = true));
});
$("#btn-courses-none").addEventListener("click", () => {
  document.querySelectorAll(".course-chk").forEach((c) => (c.checked = false));
});

$("#btn-new-instance").addEventListener("click", async () => {
  $("#inst-name").value = `task-${cachedInstances.length + 1}`;
  $("#inst-manual-courses").value = "";
  $("#course-search").value = "";
  $("#inst-paste-panel").classList.add("hidden");
  cachedCourses = [];
  renderCourseList("");

  const select = $("#inst-api-profile");
  const options = cachedApiItems.map((item) => {
    const label = `${item.name} (${item.model})${item.is_default ? " [默认]" : ""}`;
    return `<option value="${escapeHtml(item.id)}" ${item.is_default ? "selected" : ""}>${escapeHtml(label)}</option>`;
  });
  options.push(`<option value="" ${cachedApiItems.length === 0 ? "selected" : ""}>不启用 AI 答题（仅刷视频）</option>`);
  select.innerHTML = options.join("");

  $("#instance-modal").classList.remove("hidden");
  await refreshSessionsList();

  if (cachedSessions.length) {
    const targetId = activeSessionId || cachedSessions[0].id;
    window.selectSavedSession(targetId);
  } else {
    showQrLoginPanel();
  }
});

$("#instance-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!activeSessionId) {
    showToast("请先登录账号", true);
    return;
  }

  const speed = Number($("#inst-speed").value);
  const jobs = Number($("#inst-jobs").value);
  if (speed < 1 || speed > 2) {
    showToast("倍速范围 1.0 ~ 2.0", true);
    return;
  }
  if (!Number.isInteger(jobs) || jobs < 1 || jobs > 4) {
    showToast("并发范围 1 ~ 4", true);
    return;
  }

  const checkedIds = [];
  const courseTitles = {};
  document.querySelectorAll(".course-chk:checked").forEach((el) => {
    checkedIds.push(el.value);
    courseTitles[el.value] = el.dataset.title || el.value;
  });

  const manualIds = $("#inst-manual-courses")
    .value.split(/[\s,，]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  const courses = Array.from(new Set([...checkedIds, ...manualIds]));

  if (!courses.length) {
    showToast("请选择或输入课程 ID", true);
    return;
  }

  const payload = {
    name: $("#inst-name").value.trim(),
    cookie_source: "session",
    cookie_session_id: activeSessionId,
    courses,
    course_titles: courseTitles,
    api_profile_id: $("#inst-api-profile").value,
    speed,
    jobs,
    notopen_action: $("#inst-notopen").value,
    redo_finished: $("#inst-redo-finished").checked,
    no_cache: $("#inst-no-cache").checked,
    auto_start: $("#inst-auto-start").checked,
  };

  const btn = $("#btn-submit-instance");
  btn.disabled = true;
  try {
    const { ok, data } = await apiFetch("/api/instances", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!ok || !data.ok) {
      showToast(data.error || "创建失败", true);
      return;
    }
    closeModalById("instance-modal");
    showToast("任务已创建");
  } finally {
    btn.disabled = false;
  }
});

// ---------------- Re-auth Existing Instance via QR ----------------
function setReauthStatus(text, tone = "warn") {
  const el = $("#reauth-qr-status");
  el.textContent = text;
  el.className = "banner";
  if (tone === "ok") el.classList.add("banner-ok");
  else if (tone === "warn") el.classList.add("banner-warn");
  else if (tone === "err") el.classList.add("banner-err");
}

async function startReauthQr() {
  stopReauthPolling();
  $("#reauth-qr-img").classList.add("hidden");
  $("#reauth-qr-placeholder").classList.remove("hidden");
  $("#reauth-qr-placeholder").textContent = "获取中...";
  setReauthStatus("正在获取二维码...", "warn");

  const { ok, data } = await apiFetch("/api/auth/qr/create", { method: "POST" });
  if (!ok || !data.ok) {
    setReauthStatus(data.error || "获取失败", "err");
    return;
  }

  reauthQrSessionId = data.session_id;
  $("#reauth-qr-img").src = data.qr_image;
  $("#reauth-qr-img").classList.remove("hidden");
  $("#reauth-qr-placeholder").classList.add("hidden");
  setReauthStatus("使用学习通 App 扫码确认", "warn");

  reauthPollTimer = setInterval(async () => {
    if (!reauthQrSessionId || !reauthTargetInstanceId) return;
    try {
      const { data: poll } = await apiFetch(
        `/api/auth/qr/poll/${reauthQrSessionId}?instance_id=${encodeURIComponent(reauthTargetInstanceId)}`
      );
      if (poll.status === "scanned") {
        setReauthStatus("已扫码，请在手机确认", "warn");
      } else if (poll.status === "confirmed") {
        stopReauthPolling();
        closeModalById("reauth-modal");
        showToast("登录已更新");
        refreshSessionsList();
      } else if (poll.status === "expired" || poll.status === "cancelled") {
        stopReauthPolling();
        setReauthStatus(poll.msg || "二维码已失效", "err");
      }
    } catch (err) {
      stopReauthPolling();
      setReauthStatus(`轮询异常: ${err.message}`, "err");
    }
  }, 2000);
}

window.openReauthModal = (id, name) => {
  reauthTargetInstanceId = id;
  $("#reauth-modal-title").textContent = `更新登录 · ${name}`;
  $("#reauth-modal").classList.remove("hidden");
  startReauthQr();
};
$("#btn-reauth-refresh").addEventListener("click", startReauthQr);

// ---------------- Instance Actions & Logs ----------------
window.startInstance = async (id) => {
  const { ok, data } = await apiFetch(`/api/instances/${id}/start`, { method: "POST" });
  if (!ok || !data.ok) {
    showToast(data.error || "启动失败", true);
    return;
  }
  showToast("已启动");
};

window.stopInstance = async (id) => {
  const { ok, data } = await apiFetch(`/api/instances/${id}/stop`, { method: "POST" });
  if (!ok || !data.ok) {
    showToast(data.error || "停止失败", true);
    return;
  }
  showToast("已停止");
};

window.deleteInstance = async (id) => {
  if (!confirm("确认删除该任务？")) return;
  const { ok, data } = await apiFetch(`/api/instances/${id}`, { method: "DELETE" });
  if (!ok || !data.ok) {
    showToast(data.error || "删除失败", true);
    return;
  }
  showToast("已删除");
};

$("#btn-clear-completed").addEventListener("click", async () => {
  const count = cachedInstances.filter((x) => x.runtime_status === "completed").length;
  if (!count) return;
  if (!confirm(`确认清理全部 ${count} 个已完成任务？`)) return;
  const { ok, data } = await apiFetch("/api/instances/clear-completed", { method: "POST" });
  if (!ok || !data.ok) {
    showToast(data.error || "清理失败", true);
    return;
  }
  showToast(`已清理 ${data.removed} 个已完成任务`);
});

window.openInstanceLogs = async (id, name) => {
  activeLogInstanceId = id;
  $("#log-modal-title").textContent = `日志 · ${name}`;
  const viewer = $("#log-content");
  viewer.textContent = "加载中...";
  $("#log-modal").classList.remove("hidden");

  const pullLogs = async () => {
    if (!activeLogInstanceId) return;
    const { ok, data } = await apiFetch(`/api/instances/${activeLogInstanceId}/logs?tail=300`);
    if (ok && data.ok) {
      const atBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
      viewer.textContent = data.logs || "（暂无输出）";
      if (atBottom) viewer.scrollTop = viewer.scrollHeight;
    }
  };

  await pullLogs();
  if (logPollTimer) clearInterval(logPollTimer);
  logPollTimer = setInterval(pullLogs, 2000);
};

// ---------------- Init ----------------
loadAiApis();
refreshSessionsList();
connectMonitorStream();
