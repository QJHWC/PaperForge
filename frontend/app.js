// PaperForge 工作台 — 实时状态前端
// 每 5 秒自动从 /api/workspaces 拉取真实数据

const REFRESH_MS = 5000;

const PHASE_ORDER = ["bootstrap", "feedback", "optimize", "refine", "cloud"];

const PHASE_LABEL = {
  bootstrap_running: "Bootstrap 运行中",
  bootstrap_completed: "Bootstrap 完成",
  feedback_running: "Feedback 运行中",
  feedback_completed: "Feedback 完成",
  optimize_running: "Optimize 运行中",
  optimize_completed: "Optimize 完成",
  refine_running: "Refine 运行中",
  refine_completed: "Refine 完成",
  cloud_running: "Cloud 运行中",
  cloud_completed: "Cloud 完成",
};

function phaseColor(phase) {
  if (!phase) return "#aaa";
  if (phase.endsWith("_running")) return "#b7791f";
  if (phase.endsWith("_completed")) return "#2d6a4f";
  return "#675d52";
}

function phaseProgress(phase) {
  if (!phase) return 0;
  const name = phase.replace(/_running|_completed/, "");
  const idx = PHASE_ORDER.indexOf(name);
  if (idx === -1) return 0;
  const base = (idx / PHASE_ORDER.length) * 100;
  const extra = phase.endsWith("_completed") ? (1 / PHASE_ORDER.length) * 100 : 0;
  return Math.round(base + extra);
}

function timeAgo(iso) {
  if (!iso) return "—";
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s 前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return `${Math.floor(diff / 86400)}d 前`;
}

async function fetchWorkspaces() {
  const res = await fetch("/api/workspaces");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchWorkspaceDetail(ws) {
  const rel = ws.replace(/^.*\/results\//, "results/");
  const res = await fetch(`/api/workspace/${rel}`);
  if (!res.ok) return null;
  return res.json();
}

function renderPhaseBar(phase) {
  const pct = phaseProgress(phase);
  const color = phaseColor(phase);
  const label = PHASE_LABEL[phase] || phase || "未开始";
  return `
    <div style="margin-top:10px">
      <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
        <span style="color:${color};font-weight:600">${label}</span>
        <span style="color:#675d52">${pct}%</span>
      </div>
      <div style="height:6px;border-radius:999px;background:#e8dfd4;overflow:hidden">
        <div style="height:100%;width:${pct}%;background:${color};border-radius:999px;transition:width 0.4s"></div>
      </div>
    </div>`;
}

function renderWorkspaceCard(ws) {
  const state = ws.state || {};
  const phase = state.phase || state.current_phase || null;
  const updatedAt = state.updated_at || null;
  const ideaName = state.idea_name || ws.run_name || "—";
  const runCount = ws.run_count || 0;

  return `
    <div class="ws-card" data-workspace="${ws.workspace}" style="
      background:rgba(255,250,244,0.86);
      border:1px solid rgba(48,37,24,0.12);
      border-radius:18px;
      padding:20px;
      cursor:pointer;
      transition:box-shadow 0.2s;
    ">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:#a4492a;margin-bottom:4px">${ws.experiment}</div>
          <div style="font-weight:600;font-size:15px">${ideaName}</div>
          <div style="font-size:12px;color:#675d52;margin-top:2px">${ws.run_name}</div>
        </div>
        <div style="text-align:right;font-size:12px;color:#675d52">
          <div>${runCount} 次实验</div>
          <div style="margin-top:2px">${timeAgo(updatedAt)}</div>
        </div>
      </div>
      ${renderPhaseBar(phase)}
    </div>`;
}

function renderDetail(detail) {
  if (!detail) return `<div style="padding:20px;color:#675d52">加载失败</div>`;
  const state = detail.state || {};
  const phase = state.phase || state.current_phase || "—";
  const runs = detail.runs || [];
  const latex = detail.latex || {};
  const notes = detail.notes_preview || "";

  return `
    <div style="display:flex;flex-direction:column;gap:16px">
      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">当前阶段</div>
        <div style="font-size:18px;font-weight:700;color:${phaseColor(phase)}">${PHASE_LABEL[phase] || phase}</div>
        ${renderPhaseBar(phase)}
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">实验运行（${runs.length} 次）</div>
        ${runs.map(r => `
          <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(48,37,24,0.08);font-size:13px">
            <span>${r.name}</span>
            <span style="color:${r.has_result ? '#2d6a4f' : '#675d52'}">${r.has_result ? '✓ 有结果' : '无结果'}</span>
          </div>`).join("")}
      </div>

      ${latex.pdf_files && latex.pdf_files.length ? `
        <div>
          <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">LaTeX 输出</div>
          ${latex.pdf_files.map(f => `<div style="font-size:13px;color:#2d6a4f">📄 ${f}</div>`).join("")}
          ${latex.tex_files.map(f => `<div style="font-size:13px;color:#675d52">📝 ${f}</div>`).join("")}
        </div>` : ""}

      ${notes ? `
        <div>
          <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Notes 预览</div>
          <pre style="
            white-space:pre-wrap;word-break:break-word;
            background:#261d15;color:#f9f1e7;
            padding:14px;border-radius:12px;
            font-size:11px;line-height:1.6;
            max-height:200px;overflow:auto;
            margin:0;
          ">${notes.replace(/</g, "&lt;")}</pre>
        </div>` : ""}
    </div>`;
}

// ── 主渲染 ──────────────────────────────────────────────────────

let selectedWorkspace = null;
let lastData = null;

async function render() {
  const root = document.getElementById("app");
  let data;
  try {
    data = await fetchWorkspaces();
    lastData = data;
  } catch (e) {
    root.innerHTML = `<div style="padding:40px;color:#a4492a">⚠ 无法连接到后端服务: ${e.message}<br><small>请确保 python frontend/server.py 已运行</small></div>`;
    return;
  }

  const workspaces = data.workspaces || [];
  const selected = selectedWorkspace;

  // 如果有选中的 workspace，拉取详情
  let detail = null;
  if (selected) {
    detail = await fetchWorkspaceDetail(selected);
  }

  root.innerHTML = `
    <div style="width:min(1200px,calc(100vw - 32px));margin:0 auto;padding:28px 0 48px">

      <!-- Header -->
      <div style="margin-bottom:24px">
        <div style="display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:rgba(164,73,42,0.08);color:#a4492a;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:12px">
          ● LIVE
        </div>
        <h1 style="font-family:'Iowan Old Style','Palatino Linotype',Georgia,serif;font-size:clamp(34px,4vw,54px);margin:0 0 8px;letter-spacing:-0.02em">PaperForge 工作台</h1>
        <div style="color:#675d52;font-size:14px">实时监控研究流水线状态 · 每 ${REFRESH_MS/1000} 秒自动刷新</div>
      </div>

      <!-- 统计条 -->
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px">
        <div style="padding:10px 16px;border-radius:12px;border:1px solid rgba(48,37,24,0.12);background:#fffdf9;font-size:13px">
          📁 <strong>${workspaces.length}</strong> 个 Workspace
        </div>
        <div style="padding:10px 16px;border-radius:12px;border:1px solid rgba(48,37,24,0.12);background:#fffdf9;font-size:13px">
          🔬 <strong>${workspaces.reduce((s, w) => s + (w.run_count || 0), 0)}</strong> 次实验
        </div>
        <div style="padding:10px 16px;border-radius:12px;border:1px solid rgba(48,37,24,0.12);background:#fffdf9;font-size:13px">
          ⏱ 上次刷新: <strong>${new Date().toLocaleTimeString("zh-CN")}</strong>
        </div>
      </div>

      <!-- 主体 -->
      <div style="display:grid;grid-template-columns:${selected ? '1fr 380px' : '1fr'};gap:20px">

        <!-- Workspace 列表 -->
        <div>
          <h2 style="font-family:'Iowan Old Style',Georgia,serif;font-size:20px;margin:0 0 14px">Workspace 列表</h2>
          ${workspaces.length === 0
            ? `<div style="padding:40px;text-align:center;color:#675d52;border:1px dashed rgba(48,37,24,0.2);border-radius:18px">暂无 Workspace<br><small>运行 launch_mvp_workflow.py --phase bootstrap 创建</small></div>`
            : `<div style="display:grid;gap:12px">${workspaces.map(renderWorkspaceCard).join("")}</div>`
          }
        </div>

        <!-- 详情面板 -->
        ${selected ? `
          <div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
              <h2 style="font-family:'Iowan Old Style',Georgia,serif;font-size:20px;margin:0">详情</h2>
              <button id="close-detail" style="border:none;background:none;cursor:pointer;font-size:18px;color:#675d52">✕</button>
            </div>
            <div style="background:rgba(255,250,244,0.86);border:1px solid rgba(48,37,24,0.12);border-radius:18px;padding:20px">
              ${renderDetail(detail)}
            </div>
          </div>` : ""}
      </div>
    </div>`;

  // 事件绑定
  root.querySelectorAll(".ws-card").forEach(card => {
    card.addEventListener("mouseenter", () => card.style.boxShadow = "0 8px 24px rgba(54,37,20,0.14)");
    card.addEventListener("mouseleave", () => card.style.boxShadow = "");
    card.addEventListener("click", () => {
      selectedWorkspace = card.dataset.workspace === selectedWorkspace ? null : card.dataset.workspace;
      render();
    });
  });

  const closeBtn = root.querySelector("#close-detail");
  if (closeBtn) closeBtn.addEventListener("click", () => { selectedWorkspace = null; render(); });
}

render();
setInterval(render, REFRESH_MS);
