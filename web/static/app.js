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
let activePortalInstanceId = null;

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
    if (btn.dataset.tab === "bank") {
      loadBankCourses().then(loadBankQuestions);
    }
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
  } else if (id === "portal-modal") {
    activePortalInstanceId = null;
    portalCoursesList = [];
    portalChaptersCourseId = null;
    portalCatalogData = null;
    portalChaptersList = [];
    portalCardsCache = {};
    portalCollapsedUnits = {};
    portalSelectedTargets = {};
    stopRemedyPolling();
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
  const queued = Number(data.queued_tasks || 0);

  $("#cpu-text").textContent = `${hw.cpu_percent}%`;
  $("#cpu-bar").style.width = `${Math.min(100, hw.cpu_percent)}%`;
  $("#mem-text").textContent = `${hw.mem_percent}%`;
  $("#mem-bar").style.width = `${Math.min(100, hw.mem_percent)}%`;
  $("#running-count-text").textContent =
    queued > 0 ? `${guard.running_containers} / ${th.max_containers} (排队 ${queued})` : `${guard.running_containers} / ${th.max_containers}`;

  $("#hw-cpu-cores").textContent = `${hw.cpu_count_logical} 核`;
  $("#hw-cpu-limit").textContent = `${hw.cpu_percent}% / ${th.max_cpu_percent}%`;
  $("#hw-mem-detail").textContent = `${hw.mem_available_gb} / ${hw.mem_total_gb} GB`;
  $("#hw-cfg-max").textContent =
    queued > 0 ? `${guard.running_containers} / ${th.max_containers} · 排队 ${queued}` : `${guard.running_containers} / ${th.max_containers}`;

  const badge = $("#safety-guard-badge");
  if (guard.allowed) {
    badge.className = "chip chip-ok";
    badge.textContent = "安全阈值正常";
  } else {
    badge.className = "chip chip-warn";
    badge.textContent = `已达阈值（新任务自动排队）· ${guard.reasons.join("；")}`;
  }
}

function statusChipHtml(status, queuePos = null) {
  const map = {
    running: ["运行中", "chip-info"],
    starting: ["启动中", "chip-info"],
    queued: [queuePos ? `排队中 #${queuePos}` : "排队中", "chip-warn"],
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
      const isQueued = inst.runtime_status === "queued";
      const pidLabel = inst.pid ? `PID ${inst.pid}` : `ID ${inst.id}`;

      const curCourse = prog.current_course
        ? `${prog.current_course.index || 1}/${prog.courses_total || inst.courses.length} · ${prog.current_course.title}`
        : `共 ${prog.courses_total || inst.courses.length} 门课程`;

      const chTotal = prog.chapters_total || 0;
      const chDone = prog.chapters_completed || 0;
      const curChapter = chTotal
        ? `${chDone}/${chTotal}${prog.current_chapter ? ` · ${prog.current_chapter}` : ""}`
        : prog.current_chapter || "-";

      const courseItems =
        Array.isArray(prog.courses) && prog.courses.length
          ? prog.courses
          : (inst.courses || []).map((cid, idx) => ({
              id: String(cid),
              title: (inst.course_titles && inst.course_titles[String(cid)]) || `课程 ${cid}`,
              index: idx + 1,
              status: inst.runtime_status === "completed" ? "completed" : "pending",
              chapters_total: 0,
              chapters_completed: 0,
              percent: inst.runtime_status === "completed" ? 100 : 0,
            }));

      const courseRowsHtml = courseItems
        .map((c) => {
          const st = c.status || "pending";
          const dotCls =
            st === "completed"
              ? "dot-ok"
              : st === "running"
                ? "dot-run"
                : st === "error"
                  ? "dot-err"
                  : "dot-wait";
          const cPct = Number(c.percent || 0).toFixed(0);
          let metaText = "待学习";
          if (c.chapters_total > 0) {
            metaText = `${c.chapters_completed || 0}/${c.chapters_total}章 · ${cPct}%`;
          } else if (st === "completed") {
            metaText = "已完成";
          } else if (st === "stopped") {
            metaText = "已暂停";
          } else if (st === "error") {
            metaText = "异常";
          }
          return `
            <div class="course-mini-row ${st === "running" ? "is-active" : ""}">
              <span class="course-dot ${dotCls}"></span>
              <span class="course-mini-name" title="${escapeHtml(c.title || c.id)}">${escapeHtml(c.title || c.id)}</span>
              <span class="course-mini-meta">${escapeHtml(metaText)}</span>
            </div>
          `;
        })
        .join("");

      const courseListHtml = courseItems.length
        ? `
          <div class="course-mini-list">
            <div class="mini-section-head">
              <span>课程列表 (${courseItems.length})</span>
              <span>${prog.courses_completed || 0}/${courseItems.length} 已完成</span>
            </div>
            <div class="course-mini-items">${courseRowsHtml}</div>
          </div>
        `
        : "";

      const rawJobs = Array.isArray(prog.active_jobs) ? prog.active_jobs : [];
      const activeJobs = isRunning
        ? rawJobs.length
          ? rawJobs
          : prog.current_job
            ? [prog.current_job]
            : []
        : [];
      const jobTypeMap = { video: "视频", work: "答题", document: "文档", read: "阅读" };

      const activeJobsHtml = isRunning
        ? activeJobs.length
          ? `
            <div class="active-jobs-box">
              <div class="mini-section-head">
                <span>并行任务点 (${activeJobs.length})</span>
                <span>实时状态</span>
              </div>
              <div class="active-jobs-list">
                ${activeJobs
                  .map((j) => {
                    const jType = j.type || "job";
                    const typeLabel = jobTypeMap[jType] || jType;
                    const badgeCls =
                      jType === "video"
                        ? "job-badge-video"
                        : jType === "work"
                          ? "job-badge-work"
                          : "job-badge-other";
                    const jPct = Math.min(100, Math.max(0, Number(j.percent || 0)));
                    const stText = j.status ? `${j.status} · ` : "";
                    const detailText = `${stText}${j.detail || `${jPct.toFixed(0)}%`}`;
                    return `
                      <div class="active-job-row">
                        <div class="active-job-top">
                          <span class="job-badge ${badgeCls}">${escapeHtml(typeLabel)}</span>
                          <span class="active-job-name" title="${escapeHtml(j.name || typeLabel)}">${escapeHtml(j.name || typeLabel)}</span>
                          <span class="active-job-detail">${escapeHtml(detailText)}</span>
                        </div>
                        <div class="job-mini-bar">
                          <div class="job-mini-fill ${jType === "work" ? "is-work" : ""}" style="width: ${jPct}%"></div>
                        </div>
                      </div>
                    `;
                  })
                  .join("")}
              </div>
            </div>
          `
          : `
            <div class="active-jobs-box is-idle">
              <div class="mini-section-head">
                <span>并行任务点 (0)</span>
                <span>准备章节中...</span>
              </div>
            </div>
          `
        : "";

      const resChip = isRunning
        ? `<span class="chip chip-neutral">CPU ${inst.cpu_percent}% · ${inst.mem_mb} MB</span>`
        : "";

      return `
        <div class="inst-card">
          <div class="inst-card-head">
            <div>
              <div class="inst-title">
                <span>${escapeHtml(inst.name)}</span>
                ${statusChipHtml(inst.runtime_status, inst.queue_position)}
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
            <div>当前课程：<strong>${escapeHtml(curCourse)}</strong></div>
            <div>当前章节：<strong>${escapeHtml(curChapter)}</strong></div>
          </div>

          ${courseListHtml}
          ${activeJobsHtml}

          <div class="inst-tags">
            <span class="chip chip-neutral">${escapeHtml(inst.api_profile_name)}</span>
            <span class="chip chip-neutral">${inst.speed}x · ${inst.jobs}并发</span>
            ${inst.redo_finished ? `<span class="chip chip-warn">重做测验</span>` : ""}
            ${inst.no_cache ? `<span class="chip chip-neutral">禁用缓存</span>` : ""}
            ${resChip}
          </div>

          <div class="card-actions">
            ${
              isRunning
                ? `<button class="btn btn-danger btn-sm" onclick="stopInstance('${inst.id}')">停止</button>`
                : isQueued
                  ? `<button class="btn btn-outlined btn-sm" onclick="stopInstance('${inst.id}')">取消排队</button>`
                  : `<button class="btn btn-primary btn-sm" onclick="startInstance('${inst.id}')">启动</button>`
            }
            <button class="btn btn-tonal btn-sm" onclick="openChaoxingPortal('${inst.id}', '${escapeHtml(inst.name)}')">学习通后台</button>
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
function hasSessionRemark(s) {
  if (!s) return false;
  if (typeof s.has_custom_label === "boolean") return s.has_custom_label;
  const defaultUid = s.uid ? `UID ${s.uid}` : s.id;
  return Boolean((s.label && s.label !== defaultUid) || s.nickname);
}

function formatSessionDisplay(s) {
  if (!s) return "";
  const uid = s.uid || s.id;
  const defaultUid = s.uid ? `UID ${s.uid}` : s.id;
  const remark = s.label && s.label !== defaultUid ? s.label : s.nickname || "";
  if (remark) {
    return `${remark} (UID ${uid})`;
  }
  return defaultUid;
}

function renderSessionChips() {
  const box = $("#session-chips");
  if (!cachedSessions.length) {
    box.innerHTML = `<span class="hint-inline">暂无已保存账号，请扫码登录</span>`;
    return;
  }
  box.innerHTML = cachedSessions
    .map((s) => {
      const isActive = s.id === activeSessionId;
      const text = formatSessionDisplay(s);
      return `
        <span class="session-chip ${isActive ? "active" : ""}" onclick="selectSavedSession('${escapeHtml(s.id)}')">
          <span>${escapeHtml(text)}</span>
          <button type="button" class="session-chip-edit" title="修改备注" onclick="event.stopPropagation(); editSessionLabel('${escapeHtml(s.id)}')">&#9998;</button>
          <button type="button" class="session-chip-del" title="删除会话" onclick="event.stopPropagation(); removeSavedSession('${escapeHtml(s.id)}')">&times;</button>
        </span>
      `;
    })
    .join("");
}

window.editSessionLabel = async (sessionId, promptText = null) => {
  const sess = cachedSessions.find((s) => s.id === sessionId);
  const uid = (sess && (sess.uid || sess.id)) || sessionId;
  const defaultUid = sess && sess.uid ? `UID ${sess.uid}` : sessionId;
  const current = sess && sess.label && sess.label !== defaultUid ? sess.label : (sess && sess.nickname) || "";
  const input = prompt(promptText || `为账户 UID ${uid} 设置备注（必填，如：张三 / 小号1）：`, current);
  if (input === null) return false;
  const label = input.trim();
  if (!label) {
    showToast("账户备注不能为空", true);
    return false;
  }
  const { ok, data } = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PUT",
    body: JSON.stringify({ label }),
  });
  if (!ok || !data.ok) {
    showToast(data.error || "更新备注失败", true);
    return false;
  }
  await refreshSessionsList();
  const updated = cachedSessions.find((s) => s.id === sessionId) || data.session;
  if (activeSessionId === sessionId && updated) {
    showLoggedInSummary(updated);
  }
  showToast("账户备注已保存");
  return true;
};

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

  $("#inst-auth-summary-text").textContent = `✓ ${formatSessionDisplay(session)}`;
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
      const qrLabel = ($("#inst-qr-label").value || "").trim();
      const labelQs = qrLabel ? `?label=${encodeURIComponent(qrLabel)}` : "";
      const { data: poll } = await apiFetch(`/api/auth/qr/poll/${currentQrSessionId}${labelQs}`);
      if (poll.status === "waiting") {
        setInstQrStatus(poll.msg || "等待扫码...", "warn");
      } else if (poll.status === "scanned") {
        const who = poll.nickname ? ` (${poll.nickname})` : "";
        setInstQrStatus(`已扫码${who}，请在手机确认`, "warn");
      } else if (poll.status === "confirmed") {
        stopInstanceQrPolling();
        $("#inst-qr-label").value = "";
        await refreshSessionsList();
        const saved = poll.saved_session || (cachedSessions.length ? cachedSessions[0] : null);
        if (saved) {
          activeSessionId = saved.id;
          renderSessionChips();
          showLoggedInSummary(saved);
          if (!hasSessionRemark(saved)) {
            await window.editSessionLabel(saved.id, `扫码成功 (UID ${saved.uid || saved.id})，请添加账户备注（必填）：`);
          } else {
            showToast("登录成功，正在加载课程");
          }
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
$("#btn-inst-edit-label").addEventListener("click", () => {
  if (activeSessionId) window.editSessionLabel(activeSessionId);
});
$("#btn-inst-qr-refresh").addEventListener("click", startInstanceQrLogin);

$("#btn-inst-paste-toggle").addEventListener("click", () => {
  stopInstanceQrPolling();
  $("#inst-qr-panel").classList.add("hidden");
  $("#inst-paste-panel").classList.toggle("hidden");
});

$("#btn-inst-paste-save").addEventListener("click", async () => {
  const cookie = $("#inst-paste-cookie").value.trim();
  const label = $("#inst-paste-label").value.trim();
  if (!label) {
    showToast("请先填写账户备注（必填）", true);
    $("#inst-paste-label").focus();
    return;
  }
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
        <input type="checkbox" class="course-chk" value="${escapeHtml(c.courseId)}" data-title="${escapeHtml(c.title)}" data-clazzid="${escapeHtml(c.clazzId || "")}" data-cpi="${escapeHtml(c.cpi || "")}" data-teacher="${escapeHtml(c.teacher || "")}" />
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
  $("#inst-qr-label").value = "";
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

  const curSess = cachedSessions.find((s) => s.id === activeSessionId);
  if (curSess && !hasSessionRemark(curSess)) {
    const savedRemark = await window.editSessionLabel(
      curSess.id,
      `账户 UID ${curSess.uid || curSess.id} 尚未添加备注，请先添加备注后再创建任务：`
    );
    if (!savedRemark) return;
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
  const courseLinks = {};
  document.querySelectorAll(".course-chk:checked").forEach((el) => {
    checkedIds.push(el.value);
    courseTitles[el.value] = el.dataset.title || el.value;
    if (el.dataset.clazzid && el.dataset.cpi) {
      courseLinks[el.value] = {
        clazzId: el.dataset.clazzid,
        cpi: el.dataset.cpi,
        title: el.dataset.title || el.value,
        teacher: el.dataset.teacher || "",
      };
    }
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
    course_links: courseLinks,
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
    if (data.item && data.item.runtime_status === "queued") {
      const pos = data.item.queue_position ? `（第 ${data.item.queue_position} 位）` : "";
      showToast(`已达安全阈值，任务已加入自动排队队列${pos}`);
    } else {
      showToast("任务已创建");
    }
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
  if (data.item && data.item.runtime_status === "queued") {
    const pos = data.item.queue_position ? `（第 ${data.item.queue_position} 位）` : "";
    showToast(`超过安全阈值，已加入自动排队队列${pos}`);
  } else {
    showToast("已启动");
  }
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
  const traceToggle = $("#log-include-trace");
  traceToggle.checked = false;
  const viewer = $("#log-content");
  viewer.textContent = "加载中...";
  $("#log-modal").classList.remove("hidden");

  const pullLogs = async () => {
    if (!activeLogInstanceId) return;
    const traceParam = traceToggle.checked ? "&trace=1" : "";
    const { ok, data } = await apiFetch(`/api/instances/${activeLogInstanceId}/logs?tail=300${traceParam}`);
    if (ok && data.ok) {
      const atBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
      viewer.textContent = data.logs || "（暂无关键日志输出）";
      if (atBottom) viewer.scrollTop = viewer.scrollHeight;
    }
  };

  traceToggle.onchange = pullLogs;
  await pullLogs();
  if (logPollTimer) clearInterval(logPollTimer);
  logPollTimer = setInterval(pullLogs, 2000);
};

// ---------------- Chaoxing Portal & Task Options ----------------
let portalCoursesList = [];
let portalChaptersCourseId = null;
let portalCatalogData = null;
let portalChaptersList = [];
let portalChapterFilter = "all";
let portalCardsCache = {};
let portalCollapsedUnits = {};
let portalSelectedTargets = {};
let remedyPollTimer = null;
let currentRemedyQueueData = null;

function stopRemedyPolling() {
  if (remedyPollTimer) {
    clearInterval(remedyPollTimer);
    remedyPollTimer = null;
  }
}

function getTargetKey(cid, kid, jobid = "") {
  return `${cid}::${kid}::${jobid || ""}`;
}

function updatePortalSelectionUi() {
  const count = Object.keys(portalSelectedTargets).length;
  const chip = $("#portal-selected-chip");
  if (chip) {
    chip.textContent = `已选 ${count} 项`;
    chip.className = `chip ${count > 0 ? "chip-info" : "chip-neutral"}`;
  }
  const startBtn = $("#btn-remedy-start");
  if (startBtn) {
    const isRunning =
      currentRemedyQueueData &&
      (currentRemedyQueueData.status === "running" || currentRemedyQueueData.status === "starting");
    startBtn.textContent = `开始排队答题 (${count})`;
    startBtn.disabled = count === 0 || isRunning;
  }
}

function populateRemedyApiSelect(preferredProfileId) {
  const select = $("#portal-remedy-api");
  if (!select) return;
  if (!cachedApiItems.length) {
    select.innerHTML = `<option value="">请先在「AI API」页面添加配置</option>`;
    return;
  }
  const targetId = preferredProfileId || (cachedApiItems.find((x) => x.is_default) || cachedApiItems[0]).id;
  select.innerHTML = cachedApiItems
    .map((item) => {
      const label = `${item.name} (${item.model})${item.is_default ? " [默认]" : ""}`;
      return `<option value="${escapeHtml(item.id)}" ${item.id === targetId ? "selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");
}

function renderRemedyQueue(data) {
  currentRemedyQueueData = data || { status: "idle", items: [] };
  const status = currentRemedyQueueData.status || "idle";
  const items = currentRemedyQueueData.items || [];
  const isRunning = status === "running" || status === "starting";

  const sumEl = $("#portal-remedy-summary");
  if (sumEl) {
    if (status === "running" || status === "starting") {
      const curIdx = Math.min((currentRemedyQueueData.current_index || 0) + 1, items.length || 1);
      sumEl.textContent = `排队答题中 (${curIdx}/${items.length})`;
      sumEl.className = "chip chip-info";
    } else if (status === "completed") {
      sumEl.textContent = `已完成 (提交 ${currentRemedyQueueData.done_count || 0} · 跳过 ${currentRemedyQueueData.skipped_count || 0} · 失败 ${currentRemedyQueueData.failed_count || 0})`;
      sumEl.className = (currentRemedyQueueData.failed_count || 0) > 0 ? "chip chip-warn" : "chip chip-ok";
    } else if (status === "stopped") {
      sumEl.textContent = "已停止";
      sumEl.className = "chip chip-warn";
    } else {
      sumEl.textContent = "未开始";
      sumEl.className = "chip chip-neutral";
    }
  }

  $("#btn-remedy-stop").classList.toggle("hidden", !isRunning);
  $("#btn-remedy-clear").disabled = isRunning || !items.length;
  updatePortalSelectionUi();

  const listEl = $("#portal-remedy-list");
  if (!items.length) {
    listEl.innerHTML = `<div class="empty-hint">在上方勾选需要补救或重做测验的章节（或展开章节加入指定测验），点击「开始排队答题」由 API 自动顺序完成</div>`;
    return;
  }

  const stMap = {
    pending: ["等待中", "chip-neutral"],
    running: ["答题中", "chip-info"],
    done: ["已完成", "chip-ok"],
    skipped: ["已跳过", "chip-warn"],
    failed: ["失败", "chip-danger"],
    stopped: ["已停止", "chip-warn"],
  };
  listEl.innerHTML = items
    .map((it) => {
      const [label, cls] = stMap[it.status] || [it.status || "未知", "chip-neutral"];
      const titleText = it.work_name ? `${it.chapter_title} · ${it.work_name}` : it.chapter_title;
      return `
        <div class="portal-remedy-row">
          <div class="portal-remedy-info">
            <span class="portal-remedy-name">${escapeHtml(titleText)} <small class="hint-inline">(${escapeHtml(it.course_title || it.course_id)})</small></span>
            <span class="portal-remedy-msg">${escapeHtml(it.message || "")}</span>
          </div>
          <span class="chip ${cls}">${escapeHtml(label)}</span>
        </div>
      `;
    })
    .join("");
}

async function pollRemedyQueue() {
  if (!activePortalInstanceId) {
    stopRemedyPolling();
    return;
  }
  const instId = activePortalInstanceId;
  try {
    const { ok, data } = await apiFetch(`/api/instances/${instId}/remedy-queue`);
    if (activePortalInstanceId !== instId) return;
    if (ok && data.ok) {
      renderRemedyQueue(data);
      if (data.status !== "running" && data.status !== "starting") {
        stopRemedyPolling();
      }
    }
  } catch (e) {}
}

function ensureRemedyPolling() {
  stopRemedyPolling();
  remedyPollTimer = setInterval(pollRemedyQueue, 2000);
}

function renderPortalCourseTabs() {
  const box = $("#portal-course-tabs");
  if (!portalCoursesList.length) {
    box.innerHTML = `<div class="empty-hint">该任务暂无关联课程</div>`;
    return;
  }
  box.innerHTML = portalCoursesList
    .map((c) => {
      const cid = String(c.id);
      const isSel = portalChaptersCourseId === cid;
      const teacherSuffix = c.teacher ? ` · ${c.teacher}` : "";
      return `
        <button type="button" class="btn ${isSel ? "btn-primary" : "btn-outlined"} btn-sm portal-course-tab" data-course-id="${escapeHtml(cid)}" data-course-title="${escapeHtml(c.title || cid)}">
          ${escapeHtml((c.title || cid) + teacherSuffix)}
        </button>
      `;
    })
    .join("");
}

function renderPortalModal(data) {
  portalCoursesList = data.courses || [];
  $("#portal-redo-finished").checked = Boolean(data.redo_finished);
  $("#portal-no-cache").checked = Boolean(data.no_cache);

  const warnEl = $("#portal-warning");
  if (data.warning) {
    warnEl.textContent = data.warning;
    warnEl.classList.remove("hidden");
  } else {
    warnEl.textContent = "";
    warnEl.classList.add("hidden");
  }

  renderPortalCourseTabs();

  const failedWrap = $("#portal-failed-wrap");
  const failedPre = $("#portal-failed-works");
  if (data.failed_works_md && data.failed_works_md.trim()) {
    failedPre.textContent = data.failed_works_md;
    failedWrap.classList.remove("hidden");
  } else {
    failedPre.textContent = "";
    failedWrap.classList.add("hidden");
  }
}

function renderPortalProgressCard(data) {
  const card = $("#portal-progress-card");
  if (!data) {
    card.classList.add("hidden");
    return;
  }
  const jobDone = Number(data.job_done ?? data.completed ?? 0);
  const jobTotal = Number(data.job_total ?? data.total ?? 0);
  const jobPct = Number(data.job_percent ?? (jobTotal > 0 ? (jobDone / jobTotal) * 100 : 100));
  const pendingCh = Number(data.pending ?? data.unfinished ?? 0);
  const completedCh = Number(data.completed ?? 0);
  const lockedCh = Number(data.locked ?? 0);
  const noTaskCh = Number(data.no_task ?? 0);
  const pendingJobs = Number(data.pending_jobs_total ?? 0);

  $("#portal-progress-title").textContent = data.title || data.course_id || "课程进度";
  $("#portal-progress-sub").textContent = `官网已完成任务点：${jobDone} / ${jobTotal}`;
  $("#portal-progress-pct").textContent = `${jobPct.toFixed(1)}%`;
  $("#portal-progress-bar").style.width = `${Math.min(100, Math.max(0, jobPct))}%`;

  const chips = [
    `<span class="chip ${pendingCh > 0 ? "chip-warn" : "chip-ok"}">待完成 ${pendingCh} 章${pendingJobs > 0 ? ` (${pendingJobs} 个任务点)` : ""}</span>`,
    `<span class="chip chip-ok">已完成 ${completedCh} 章</span>`,
  ];
  if (lockedCh > 0) chips.push(`<span class="chip chip-danger">需解锁 ${lockedCh} 章</span>`);
  if (noTaskCh > 0) chips.push(`<span class="chip chip-neutral">无任务点 ${noTaskCh} 章</span>`);
  $("#portal-progress-chips").innerHTML = chips.join("");
  card.classList.remove("hidden");
}

function getChapterStatus(pt) {
  if (pt.status) return pt.status;
  if (pt.has_finished) return "completed";
  if (pt.need_unlock) return "locked";
  return "pending";
}

function renderChapterItemHtml(pt, typeLabelMap) {
  const cid = String(portalChaptersCourseId || "");
  const kid = String(pt.id);
  const chKey = getTargetKey(cid, kid, "");
  const isChSelected = Boolean(portalSelectedTargets[chKey]);
  const st = getChapterStatus(pt);
  let stBadge = "";
  if (st === "completed") {
    stBadge = `<span class="chip chip-ok">已完成</span>`;
  } else if (st === "locked") {
    stBadge = `<span class="chip chip-danger">需解锁${pt.pending_jobs > 0 ? ` (${pt.pending_jobs})` : ""}</span>`;
  } else if (st === "pending") {
    const cnt = pt.pending_jobs || pt.jobCount || 1;
    stBadge = `<span class="chip chip-warn">${cnt} 个待完成</span>`;
  } else {
    stBadge = `<span class="chip chip-neutral">无任务点</span>`;
  }

  const cardsState = portalCardsCache[kid];
  let cardsHtml = "";
  if (cardsState && !cardsState.hidden) {
    if (cardsState.loading) {
      cardsHtml = `<div class="portal-chapter-cards"><div class="empty-hint">正在通过任务 Cookie 读取任务点...</div></div>`;
    } else if (cardsState.error) {
      cardsHtml = `<div class="portal-chapter-cards"><div class="banner banner-err">${escapeHtml(cardsState.error)}</div></div>`;
    } else if (cardsState.not_open) {
      cardsHtml = `<div class="portal-chapter-cards"><div class="hint-inline">该章节尚未开放</div></div>`;
    } else if (!cardsState.cards || !cardsState.cards.length) {
      cardsHtml = `<div class="portal-chapter-cards"><div class="hint-inline">本章节暂无任务点（仅图文/空页面）</div></div>`;
    } else {
      cardsHtml = `
        <div class="portal-chapter-cards">
          ${cardsState.cards
            .map((card) => {
              const tLabel = typeLabelMap[card.type] || card.type;
              const passChip =
                card.is_passed === true
                  ? `<span class="chip chip-ok">已通过</span>`
                  : card.is_passed === false
                    ? `<span class="chip chip-warn">未通过</span>`
                    : "";
              const viewBtn = card.view_url
                ? `<a class="btn btn-tonal btn-sm" href="${escapeHtml(card.view_url)}" target="_blank" rel="noopener noreferrer">免登录查看测验/成绩 ↗</a>`
                : "";
              let pickBtn = "";
              if (card.type === "workid" && card.jobid) {
                const wKey = getTargetKey(cid, kid, card.jobid);
                const isWorkSel = Boolean(portalSelectedTargets[wKey]);
                pickBtn = `<button type="button" class="btn ${isWorkSel ? "btn-primary" : "btn-outlined"} btn-sm btn-remedy-pick-work" data-cid="${escapeHtml(cid)}" data-kid="${escapeHtml(kid)}" data-ch-title="${escapeHtml(pt.title || `章节 ${kid}`)}" data-jobid="${escapeHtml(card.jobid)}" data-work-name="${escapeHtml(card.name || card.jobid)}">${isWorkSel ? "✓ 已加入队列" : "+ 加入补救队列"}</button>`;
              }
              return `
                <div class="portal-card-row">
                  <span class="chip chip-neutral">${escapeHtml(tLabel)}</span>
                  <span class="portal-card-name">${escapeHtml(card.name)}</span>
                  ${passChip}
                  ${pickBtn}
                  ${viewBtn}
                </div>
              `;
            })
            .join("")}
        </div>
      `;
    }
  }

  return `
    <div class="portal-chapter-item">
      <div class="portal-chapter-head" data-kid="${escapeHtml(kid)}">
        <input type="checkbox" class="portal-ch-chk" data-cid="${escapeHtml(cid)}" data-kid="${escapeHtml(kid)}" data-title="${escapeHtml(pt.title || `章节 ${kid}`)}" ${isChSelected ? "checked" : ""} />
        <span class="portal-chapter-title">${escapeHtml(pt.title || `章节 ${kid}`)}</span>
        <div class="btn-group">
          ${stBadge}
          <span class="chip chip-neutral">${cardsState && !cardsState.hidden ? "收起" : "查任务点/测验"}</span>
        </div>
      </div>
      ${cardsHtml}
    </div>
  `;
}

function renderPortalChapters() {
  const listEl = $("#portal-chapter-list");
  const search = ($("#portal-chapter-search").value || "").trim().toLowerCase();
  const hasFilterOrSearch = portalChapterFilter !== "all" || Boolean(search);

  const units =
    portalCatalogData && Array.isArray(portalCatalogData.units) && portalCatalogData.units.length
      ? portalCatalogData.units
      : portalChaptersList.length
        ? [{ index: 1, title: "全部章节", items: portalChaptersList, pending_jobs: 0, task_chapters: portalChaptersList.length, completed_chapters: 0 }]
        : [];

  if (!units.length) {
    listEl.innerHTML = `<div class="empty-hint">未读取到任何章节目录</div>`;
    return;
  }

  const typeLabelMap = { video: "视频", workid: "测验", document: "文档", read: "阅读", live: "直播" };
  const unitHtmlList = [];

  for (const unit of units) {
    const uItems = (unit.items || []).filter((pt) => {
      const st = getChapterStatus(pt);
      if (portalChapterFilter === "pending" && st !== "pending" && st !== "locked") return false;
      if (portalChapterFilter === "completed" && st !== "completed") return false;
      if (portalChapterFilter === "none" && st !== "none") return false;
      if (search && !(pt.title || "").toLowerCase().includes(search) && !(unit.title || "").toLowerCase().includes(search)) {
        return false;
      }
      return true;
    });

    if (!uItems.length && hasFilterOrSearch) continue;

    const uKey = String(unit.index);
    const isCollapsed = hasFilterOrSearch ? false : Boolean(portalCollapsedUnits[uKey]);
    const unitBadge =
      unit.pending_jobs > 0
        ? `<span class="chip chip-warn">待完成 ${unit.pending_jobs}</span>`
        : unit.task_chapters > 0
          ? `<span class="chip chip-ok">已全清 (${unit.completed_chapters}/${unit.task_chapters})</span>`
          : `<span class="chip chip-neutral">${unit.total_chapters || uItems.length} 节</span>`;

    const bodyHtml = isCollapsed
      ? ""
      : `<div class="portal-unit-body">${uItems.map((pt) => renderChapterItemHtml(pt, typeLabelMap)).join("")}</div>`;

    unitHtmlList.push(`
      <div class="portal-unit-block">
        <div class="portal-unit-head" data-unit-idx="${escapeHtml(uKey)}">
          <span class="portal-unit-title">${isCollapsed ? "▸" : "▾"} ${escapeHtml(unit.title || `第 ${uKey} 章`)}</span>
          <div class="btn-group">${unitBadge}</div>
        </div>
        ${bodyHtml}
      </div>
    `);
  }

  listEl.innerHTML = unitHtmlList.length ? unitHtmlList.join("") : `<div class="empty-hint">无匹配章节</div>`;
}

window.loadPortalCourseChapters = async (courseId, courseTitle) => {
  if (!activePortalInstanceId) return;
  const instId = activePortalInstanceId;
  portalChaptersCourseId = String(courseId);
  portalCatalogData = null;
  portalChaptersList = [];
  portalCardsCache = {};
  portalCollapsedUnits = {};
  portalChapterFilter = "all";
  $("#portal-chapter-search").value = "";
  $("#btn-portal-toggle-units").textContent = "全部折叠";
  document.querySelectorAll(".portal-filter-btn").forEach((b) => {
    const isAll = b.dataset.filter === "all";
    b.classList.toggle("active", isAll);
    b.classList.toggle("btn-tonal", isAll);
    b.classList.toggle("btn-outlined", !isAll);
  });

  renderPortalCourseTabs();
  $("#portal-progress-card").classList.add("hidden");
  $("#portal-chapter-list").innerHTML = `<div class="empty-hint">正在通过任务 Cookie 拉取「${escapeHtml(courseTitle || courseId)}」的官网章节进度...</div>`;

  try {
    const { ok, data } = await apiFetch(
      `/api/instances/${instId}/courses/${encodeURIComponent(courseId)}/chapters`
    );
    if (activePortalInstanceId !== instId || portalChaptersCourseId !== String(courseId)) return;
    if (!ok || !data.ok) {
      $("#portal-chapter-list").innerHTML = `<div class="banner banner-err">${escapeHtml(data.error || "读取失败")}</div>`;
      return;
    }
    portalCatalogData = data;
    portalChaptersList = data.points || [];
    renderPortalProgressCard(data);
    renderPortalChapters();
  } catch (err) {
    $("#portal-chapter-list").innerHTML = `<div class="banner banner-err">读取失败：${escapeHtml(err.message)}</div>`;
  }
};

window.togglePortalChapterCards = async (knowledgeId) => {
  if (!activePortalInstanceId || !portalChaptersCourseId) return;
  const kid = String(knowledgeId);
  const existing = portalCardsCache[kid];
  if (existing && !existing.loading && !existing.error) {
    existing.hidden = !existing.hidden;
    renderPortalChapters();
    return;
  }

  const instId = activePortalInstanceId;
  const cid = portalChaptersCourseId;
  portalCardsCache[kid] = { loading: true, hidden: false };
  renderPortalChapters();

  try {
    const { ok, data } = await apiFetch(
      `/api/instances/${instId}/courses/${encodeURIComponent(cid)}/chapters/${encodeURIComponent(kid)}/cards`
    );
    if (activePortalInstanceId !== instId || portalChaptersCourseId !== cid) return;
    if (!ok || !data.ok) {
      portalCardsCache[kid] = { loading: false, hidden: false, error: data.error || "加载任务点失败" };
    } else {
      portalCardsCache[kid] = {
        loading: false,
        hidden: false,
        not_open: Boolean(data.not_open),
        cards: data.cards || [],
      };
    }
    renderPortalChapters();
  } catch (err) {
    portalCardsCache[kid] = { loading: false, hidden: false, error: err.message };
    renderPortalChapters();
  }
};

$("#portal-course-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".portal-course-tab");
  if (!btn) return;
  window.loadPortalCourseChapters(btn.dataset.courseId, btn.dataset.courseTitle);
});

$("#portal-chapter-list").addEventListener("click", (e) => {
  if (e.target.classList.contains("portal-ch-chk")) {
    const chk = e.target;
    const cid = chk.dataset.cid;
    const kid = chk.dataset.kid;
    const chTitle = chk.dataset.title || `章节 ${kid}`;
    const key = getTargetKey(cid, kid, "");
    if (chk.checked) {
      const cMeta = portalCoursesList.find((x) => String(x.id) === String(cid)) || {};
      portalSelectedTargets[key] = {
        course_id: cid,
        course_title: cMeta.title || (portalCatalogData && portalCatalogData.title) || cid,
        clazz_id: (portalCatalogData && portalCatalogData.clazzId) || cMeta.clazzId || "",
        cpi: (portalCatalogData && portalCatalogData.cpi) || cMeta.cpi || "",
        knowledge_id: kid,
        chapter_title: chTitle,
        jobid: "",
        work_name: "",
      };
    } else {
      delete portalSelectedTargets[key];
    }
    updatePortalSelectionUi();
    return;
  }

  const pickBtn = e.target.closest(".btn-remedy-pick-work");
  if (pickBtn) {
    const cid = pickBtn.dataset.cid;
    const kid = pickBtn.dataset.kid;
    const jobid = pickBtn.dataset.jobid;
    const key = getTargetKey(cid, kid, jobid);
    if (portalSelectedTargets[key]) {
      delete portalSelectedTargets[key];
    } else {
      const cMeta = portalCoursesList.find((x) => String(x.id) === String(cid)) || {};
      portalSelectedTargets[key] = {
        course_id: cid,
        course_title: cMeta.title || (portalCatalogData && portalCatalogData.title) || cid,
        clazz_id: (portalCatalogData && portalCatalogData.clazzId) || cMeta.clazzId || "",
        cpi: (portalCatalogData && portalCatalogData.cpi) || cMeta.cpi || "",
        knowledge_id: kid,
        chapter_title: pickBtn.dataset.chTitle || `章节 ${kid}`,
        jobid,
        work_name: pickBtn.dataset.workName || jobid,
      };
    }
    updatePortalSelectionUi();
    renderPortalChapters();
    return;
  }

  if (e.target.closest(".portal-chapter-cards")) {
    return;
  }

  const unitHead = e.target.closest(".portal-unit-head");
  if (unitHead) {
    const idx = unitHead.dataset.unitIdx;
    portalCollapsedUnits[idx] = !portalCollapsedUnits[idx];
    renderPortalChapters();
    return;
  }
  const chHead = e.target.closest(".portal-chapter-head");
  if (chHead && chHead.dataset.kid) {
    window.togglePortalChapterCards(chHead.dataset.kid);
  }
});

$("#btn-portal-select-visible").addEventListener("click", () => {
  const cMeta = portalCoursesList.find((x) => String(x.id) === String(portalChaptersCourseId)) || {};
  document.querySelectorAll("#portal-chapter-list .portal-ch-chk").forEach((chk) => {
    chk.checked = true;
    const cid = chk.dataset.cid;
    const kid = chk.dataset.kid;
    const key = getTargetKey(cid, kid, "");
    portalSelectedTargets[key] = {
      course_id: cid,
      course_title: cMeta.title || (portalCatalogData && portalCatalogData.title) || cid,
      clazz_id: (portalCatalogData && portalCatalogData.clazzId) || cMeta.clazzId || "",
      cpi: (portalCatalogData && portalCatalogData.cpi) || cMeta.cpi || "",
      knowledge_id: kid,
      chapter_title: chk.dataset.title || `章节 ${kid}`,
      jobid: "",
      work_name: "",
    };
  });
  updatePortalSelectionUi();
});

$("#btn-portal-clear-selected").addEventListener("click", () => {
  portalSelectedTargets = {};
  updatePortalSelectionUi();
  renderPortalChapters();
});

$("#btn-remedy-start").addEventListener("click", async () => {
  if (!activePortalInstanceId) return;
  const items = Object.values(portalSelectedTargets);
  if (!items.length) {
    showToast("请先勾选需要补救/重做的章节或测验", true);
    return;
  }
  const apiProfileId = $("#portal-remedy-api").value;
  if (!apiProfileId && !cachedApiItems.length) {
    showToast("请先在「AI API」页面添加至少一个配置", true);
    return;
  }
  const btn = $("#btn-remedy-start");
  btn.disabled = true;
  try {
    const { ok, data } = await apiFetch(`/api/instances/${activePortalInstanceId}/remedy-queue/start`, {
      method: "POST",
      body: JSON.stringify({
        items,
        api_profile_id: apiProfileId,
        no_cache: $("#portal-remedy-nocache").checked,
      }),
    });
    if (!ok || !data.ok) {
      showToast(data.error || "启动排队答题失败", true);
      return;
    }
    renderRemedyQueue(data);
    ensureRemedyPolling();
    showToast(`已启动排队答题，共 ${items.length} 项`);
  } finally {
    updatePortalSelectionUi();
  }
});

$("#btn-remedy-stop").addEventListener("click", async () => {
  if (!activePortalInstanceId) return;
  const { ok, data } = await apiFetch(`/api/instances/${activePortalInstanceId}/remedy-queue/stop`, {
    method: "POST",
  });
  if (!ok || !data.ok) {
    showToast(data.error || "停止队列失败", true);
    return;
  }
  stopRemedyPolling();
  renderRemedyQueue(data);
  showToast("已停止排队答题");
});

$("#btn-remedy-clear").addEventListener("click", async () => {
  if (!activePortalInstanceId) return;
  const { ok, data } = await apiFetch(`/api/instances/${activePortalInstanceId}/remedy-queue`, {
    method: "DELETE",
  });
  if (!ok || !data.ok) {
    showToast(data.error || "清空队列失败", true);
    return;
  }
  renderRemedyQueue(data);
  showToast("已清空队列记录");
});

$("#btn-portal-toggle-units").addEventListener("click", () => {
  const units = (portalCatalogData && portalCatalogData.units) || [];
  if (!units.length) return;
  const allCollapsed = units.every((u) => Boolean(portalCollapsedUnits[String(u.index)]));
  for (const u of units) {
    portalCollapsedUnits[String(u.index)] = !allCollapsed;
  }
  $("#btn-portal-toggle-units").textContent = !allCollapsed ? "全部展开" : "全部折叠";
  renderPortalChapters();
});

document.querySelectorAll(".portal-filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    portalChapterFilter = btn.dataset.filter || "all";
    document.querySelectorAll(".portal-filter-btn").forEach((b) => {
      const active = b === btn;
      b.classList.toggle("active", active);
      b.classList.toggle("btn-tonal", active);
      b.classList.toggle("btn-outlined", !active);
    });
    renderPortalChapters();
  });
});
$("#portal-chapter-search").addEventListener("input", renderPortalChapters);

window.openChaoxingPortal = async (id, name, refresh = false) => {
  activePortalInstanceId = id;
  portalCoursesList = [];
  portalChaptersCourseId = null;
  portalCatalogData = null;
  portalChaptersList = [];
  portalCardsCache = {};
  portalCollapsedUnits = {};
  portalSelectedTargets = {};
  currentRemedyQueueData = null;
  stopRemedyPolling();
  updatePortalSelectionUi();

  const instObj = cachedInstances.find((x) => x.id === id);
  populateRemedyApiSelect(instObj ? instObj.api_profile_id : "");

  $("#portal-modal-title").textContent = `学习通章节与测验 · ${name}`;
  $("#portal-warning").classList.add("hidden");
  $("#portal-failed-wrap").classList.add("hidden");
  $("#portal-progress-card").classList.add("hidden");
  $("#portal-course-tabs").innerHTML = `<div class="empty-hint">${refresh ? "正在刷新课程信息..." : "正在加载课程..."}</div>`;
  $("#portal-chapter-list").innerHTML = `<div class="empty-hint">正在读取课程列表...</div>`;
  $("#portal-remedy-list").innerHTML = `<div class="empty-hint">加载队列状态中...</div>`;
  $("#portal-modal").classList.remove("hidden");

  pollRemedyQueue().then(() => {
    if (
      activePortalInstanceId === id &&
      currentRemedyQueueData &&
      (currentRemedyQueueData.status === "running" || currentRemedyQueueData.status === "starting")
    ) {
      ensureRemedyPolling();
    }
  });

  const qs = refresh ? "?refresh=1" : "";
  try {
    const { ok, data } = await apiFetch(`/api/instances/${id}/portal${qs}`);
    if (!ok || !data.ok) {
      $("#portal-course-tabs").innerHTML = `<div class="banner banner-err">${escapeHtml(data.error || "加载失败")}</div>`;
      $("#portal-chapter-list").innerHTML = "";
      return;
    }
    if (activePortalInstanceId !== id) return;
    renderPortalModal(data);
    if (portalCoursesList.length) {
      const first = portalCoursesList[0];
      await window.loadPortalCourseChapters(first.id, first.title);
    } else {
      $("#portal-chapter-list").innerHTML = `<div class="empty-hint">该任务暂无关联课程</div>`;
    }
  } catch (err) {
    $("#portal-course-tabs").innerHTML = `<div class="banner banner-err">加载失败：${escapeHtml(err.message)}</div>`;
    $("#portal-chapter-list").innerHTML = "";
  }
};

async function savePortalOptions(restart = false) {
  if (!activePortalInstanceId) return;
  const id = activePortalInstanceId;
  const payload = {
    redo_finished: $("#portal-redo-finished").checked,
    no_cache: $("#portal-no-cache").checked,
  };
  const btnOnly = $("#btn-portal-save-only");
  const btnRestart = $("#btn-portal-save-restart");
  btnOnly.disabled = true;
  btnRestart.disabled = true;
  try {
    const { ok, data } = await apiFetch(`/api/instances/${id}/options`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    if (!ok || !data.ok) {
      showToast(data.error || "保存设置失败", true);
      return;
    }
    if (!restart) {
      showToast("任务参数已保存（下次启动生效）");
      return;
    }
    const inst = data.item || {};
    if (inst.runtime_status === "running") {
      await apiFetch(`/api/instances/${id}/stop`, { method: "POST" });
    }
    const { ok: startOk, data: startData } = await apiFetch(`/api/instances/${id}/start`, { method: "POST" });
    if (!startOk || !startData.ok) {
      showToast(startData.error || "启动失败", true);
      return;
    }
    closeModalById("portal-modal");
    showToast("已保存设置并启动任务");
  } finally {
    btnOnly.disabled = false;
    btnRestart.disabled = false;
  }
}

$("#btn-portal-refresh").addEventListener("click", () => {
  if (!activePortalInstanceId) return;
  const inst = cachedInstances.find((x) => x.id === activePortalInstanceId);
  window.openChaoxingPortal(activePortalInstanceId, inst ? inst.name : activePortalInstanceId, true);
});
$("#btn-portal-save-only").addEventListener("click", () => savePortalOptions(false));
$("#btn-portal-save-restart").addEventListener("click", () => savePortalOptions(true));

// ---------------- Question Bank (Course-Partitioned SQLite) ----------------
let activeBankCourseId = "";
const bankItemMap = new Map();

async function loadBankCourses() {
  const { data } = await apiFetch("/api/bank/courses");
  const courses = data.items || [];
  const box = $("#bank-courses");
  if (!courses.length) {
    activeBankCourseId = "";
    box.innerHTML = `<span class="hint-inline">暂无题库记录，AI 答题后会自动按课程入库</span>`;
    return;
  }
  if (activeBankCourseId && !courses.some((c) => c.course_id === activeBankCourseId)) {
    activeBankCourseId = "";
  }
  const allChip = `<span class="session-chip ${activeBankCourseId === "" ? "active" : ""}" onclick="selectBankCourse('')">全部课程</span>`;
  const chips = courses.map((c) => {
    const active = activeBankCourseId === c.course_id ? "active" : "";
    const extra = [
      c.verified_count > 0 ? `验${c.verified_count}` : "",
      c.wrong_count > 0 ? `错${c.wrong_count}` : "",
    ].filter(Boolean).join(" · ");
    return `
      <span class="session-chip ${active}" onclick="selectBankCourse('${escapeHtml(c.course_id)}')">
        <span>${escapeHtml(c.course_title)} (${c.question_count}题 · 命中${c.total_hits}${extra ? ` · ${extra}` : ""})</span>
        <button type="button" class="session-chip-del" title="清空该课程题库" onclick="event.stopPropagation(); clearBankCourse('${escapeHtml(c.course_id)}')">&times;</button>
      </span>
    `;
  });
  box.innerHTML = allChip + chips.join("");
}

async function loadBankQuestions() {
  const q = encodeURIComponent($("#bank-search").value.trim());
  const cid = activeBankCourseId ? `&course_id=${encodeURIComponent(activeBankCourseId)}` : "";
  const srcFilter = $("#bank-source-filter") ? $("#bank-source-filter").value.trim() : "";
  const srcParam = srcFilter ? `&source=${encodeURIComponent(srcFilter)}` : "";
  const { data } = await apiFetch(`/api/bank/questions?limit=100${cid}${srcParam}&q=${q}`);
  const items = data.items || [];
  const total = data.total ?? items.length;
  $("#bank-total-chip").textContent = `${total} 题`;
  bankItemMap.clear();
  items.forEach((it) => bankItemMap.set(it.id, it));

  const box = $("#bank-questions");
  if (!items.length) {
    box.innerHTML = `<div class="card"><div class="empty-hint">暂无匹配的题目</div></div>`;
    return;
  }

  const typeMap = {
    single: "单选",
    multiple: "多选",
    judgement: "判断",
    completion: "填空",
    shortanswer: "简答",
  };
  const tierMeta = (item) => {
    if (item.source === "verified") return { cls: "chip-ok", text: `已验证 · 命中 ${item.hit_count}` };
    if (item.source === "manual") return { cls: "chip-info", text: `人工 · 命中 ${item.hit_count}` };
    if (item.source === "wrong") return { cls: "chip-danger", text: "已知错误（已停用）" };
    return { cls: "chip-neutral", text: `AI 未验证 · 命中 ${item.hit_count}` };
  };
  box.innerHTML = items
    .map((item) => {
      const tLabel = typeMap[item.q_type] || item.q_type || "未知";
      const tier = tierMeta(item);
      const wrongList = Array.isArray(item.wrong_answers) ? item.wrong_answers : [];
      const wrongHtml = wrongList.length
        ? `<div class="bank-wrong-row">已排除错误答案：${escapeHtml(wrongList.join(" / "))}</div>`
        : "";
      return `
        <div class="bank-item">
          <div class="bank-item-head">
            <div class="bank-stem"><span class="chip chip-info">${escapeHtml(tLabel)}</span> ${escapeHtml(item.title)}</div>
            <div class="btn-group">
              <span class="chip chip-neutral">${escapeHtml(item.course_title || item.course_id)}</span>
              <span class="chip ${tier.cls}">${tier.text}</span>
            </div>
          </div>
          ${item.options ? `<div class="bank-opts">${escapeHtml(item.options)}</div>` : ""}
          ${wrongHtml}
          <div class="bank-ans-row">
            <div>${item.source === "wrong" ? "原错误答案" : "答案"}：<strong>${escapeHtml(item.answer)}</strong></div>
            <div class="btn-group">
              <button class="btn btn-outlined btn-sm" onclick="editBankAnswer(${item.id})">修改答案</button>
              <button class="btn btn-text btn-sm" onclick="deleteBankQuestion(${item.id})">删除</button>
            </div>
          </div>
        </div>
      `;
    })
    .join("");
}

window.selectBankCourse = (cid) => {
  activeBankCourseId = cid;
  loadBankCourses().then(loadBankQuestions);
};

window.clearBankCourse = async (cid) => {
  if (!confirm("确认清空该课程下的全部自建题目？")) return;
  const { ok, data } = await apiFetch(`/api/bank/courses/${encodeURIComponent(cid)}`, { method: "DELETE" });
  if (!ok || !data.ok) {
    showToast(data.error || "清空失败", true);
    return;
  }
  showToast(`已清空 ${data.removed} 道题`);
  loadBankCourses().then(loadBankQuestions);
};

window.editBankAnswer = async (qid) => {
  const item = bankItemMap.get(qid);
  const currentAns = item ? String(item.answer || "") : "";
  const nextAns = prompt("输入新答案（单选/多选填字母如 A 或 AC，判断填 正确/错误）：", currentAns);
  if (nextAns === null) return;
  const clean = nextAns.trim();
  if (!clean) {
    showToast("答案不能为空", true);
    return;
  }
  const { ok, data } = await apiFetch(`/api/bank/questions/${qid}`, {
    method: "PUT",
    body: JSON.stringify({ answer: clean }),
  });
  if (!ok || !data.ok) {
    showToast(data.error || "更新失败", true);
    return;
  }
  showToast("答案已更新");
  loadBankQuestions();
};

window.deleteBankQuestion = async (qid) => {
  const { ok, data } = await apiFetch(`/api/bank/questions/${qid}`, { method: "DELETE" });
  if (!ok || !data.ok) {
    showToast(data.error || "删除失败", true);
    return;
  }
  showToast("已删除题目");
  loadBankCourses().then(loadBankQuestions);
};

$("#btn-bank-refresh").addEventListener("click", () => loadBankCourses().then(loadBankQuestions));
$("#bank-search").addEventListener("input", loadBankQuestions);
if ($("#bank-source-filter")) {
  $("#bank-source-filter").addEventListener("change", loadBankQuestions);
}

// ---------------- Init ----------------
loadAiApis();
refreshSessionsList();
connectMonitorStream();
