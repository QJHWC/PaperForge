const REFRESH_MS = 5000;
const LOG_REFRESH_MS = 1500;
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

let selectedWorkspaceRel = null;
let selectedRunId = null;
let selectedLogOffset = 0;
let selectedLogText = "";
let csrfToken = null;

function renderFatal(message, detail = "") {
  const root = document.getElementById("app");
  if (!root) return;
  root.innerHTML = `
    <div style="width:min(960px,calc(100vw - 32px));margin:40px auto;padding:24px;border:1px solid rgba(164,73,42,0.2);border-radius:18px;background:rgba(255,250,244,0.92)">
      <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:#a4492a;margin-bottom:10px">frontend error</div>
      <h1 style="font-family:'Iowan Old Style','Palatino Linotype',Georgia,serif;font-size:28px;margin:0 0 12px">页面渲染失败</h1>
      <div style="color:#675d52;line-height:1.7">${htmlEscape(message)}</div>
      ${detail ? `<pre style="margin-top:16px;white-space:pre-wrap;word-break:break-word;background:#261d15;color:#f9f1e7;padding:14px;border-radius:12px;font-size:12px;line-height:1.6">${htmlEscape(detail)}</pre>` : ""}
    </div>`;
}

function htmlEscape(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function encodePath(value) {
  return String(value || "")
    .split("/")
    .filter((part) => part.length)
    .map((part) => encodeURIComponent(part))
    .join("/");
}

function workspaceApi(workspaceRel, suffix = "") {
  return `/api/workspace/${encodePath(workspaceRel)}${suffix}`;
}

async function ensureSession(force = false) {
  if (csrfToken && !force) return csrfToken;
  const res = await fetch("/api/session", {
    credentials: "same-origin",
    cache: "no-store",
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok || !payload.csrf_token) {
    throw new Error(payload.error || `Unable to establish UI session (HTTP ${res.status})`);
  }
  csrfToken = payload.csrf_token;
  return csrfToken;
}

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

function statusPill(status) {
  const color =
    status === "running"
      ? "#b7791f"
      : status === "paused"
      ? "#1f5560"
      : status === "completed"
      ? "#2d6a4f"
      : status === "failed"
      ? "#a12626"
      : "#675d52";
  return `<span style="display:inline-flex;padding:4px 10px;border-radius:999px;background:${color}1a;color:${color};font-size:12px;font-weight:700">${htmlEscape(status || "unknown")}</span>`;
}

async function fetchJSON(url) {
  const res = await fetch(url, { credentials: "same-origin", cache: "no-store" });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
  return payload;
}

async function sendJSON(method, url, body, retry = true) {
  const token = await ensureSession();
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-PaperForge-CSRF": token,
    },
    body: JSON.stringify(body || {}),
  });
  const payload = await res.json().catch(() => ({}));
  if (retry && res.status === 403 && String(payload.error || "").includes("CSRF")) {
    await ensureSession(true);
    return sendJSON(method, url, body, false);
  }
  if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
  return payload;
}

async function sendMultipart(url, files, retry = true) {
  const token = await ensureSession();
  const form = new FormData();
  files.forEach((file) => {
    const filename = file.webkitRelativePath || file.name;
    form.append("files", file, filename);
  });
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-PaperForge-CSRF": token },
    body: form,
  });
  const payload = await res.json().catch(() => ({}));
  if (retry && res.status === 403 && String(payload.error || "").includes("CSRF")) {
    await ensureSession(true);
    return sendMultipart(url, files, false);
  }
  if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
  return payload;
}

async function fetchWorkspaces() {
  return fetchJSON("/api/workspaces");
}

async function fetchWorkspaceDetail(workspaceRel) {
  return fetchJSON(workspaceApi(workspaceRel));
}

async function fetchRunLog(workspaceRel, runId, offset) {
  return fetchJSON(`${workspaceApi(workspaceRel, "/log")}?run_id=${encodeURIComponent(runId)}&offset=${offset}`);
}

function renderPhaseBar(phase) {
  const pct = phaseProgress(phase);
  const color = phaseColor(phase);
  const label = PHASE_LABEL[phase] || phase || "未开始";
  return `
    <div style="margin-top:10px">
      <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
        <span style="color:${color};font-weight:600">${htmlEscape(label)}</span>
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
  return `
    <div class="ws-card" data-workspace-rel="${ws.workspace_rel}" style="
      background:rgba(255,250,244,0.86);
      border:1px solid rgba(48,37,24,0.12);
      border-radius:18px;
      padding:20px;
      cursor:pointer;
      transition:box-shadow 0.2s;
    ">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:#a4492a;margin-bottom:4px">${htmlEscape(ws.experiment)}</div>
          <div style="font-weight:600;font-size:15px">${htmlEscape(ideaName)}</div>
          <div style="font-size:12px;color:#675d52;margin-top:2px">${htmlEscape(ws.run_name)}</div>
        </div>
        <div style="text-align:right;font-size:12px;color:#675d52">
          <div>${ws.run_count || 0} 次实验</div>
          <div style="margin-top:2px">${timeAgo(updatedAt)}</div>
        </div>
      </div>
      ${renderPhaseBar(phase)}
    </div>`;
}

function fileHref(workspaceRel, relPath) {
  return `/files/results/${encodePath(workspaceRel)}/${encodePath(relPath)}`;
}

function latestPdf(detail) {
  const rootPdfs = detail.root_pdf_files || [];
  if (rootPdfs.includes("paper_refined.pdf")) return "paper_refined.pdf";
  if (rootPdfs.length) return rootPdfs[0];
  const latexPdfs = detail.latex?.pdf_files || [];
  if (latexPdfs.length) return `latex/${latexPdfs[0]}`;
  return null;
}

function historyItemRow(label, actions) {
  return `
    <div style="display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid rgba(48,37,24,0.08)">
      <div>${label}</div>
      <div class="actions" style="margin-top:0">${actions}</div>
    </div>`;
}

function renderSourceDrafts(detail) {
  const activeId = detail.config?.active_source_draft_id || null;
  const drafts = detail.source_drafts || [];
  if (!drafts.length) return `<div class="sub">暂无导入初稿。</div>`;
  return drafts
    .map((draft) => {
      const actions = [];
      actions.push(`<button class="set-active-draft-btn" data-draft-id="${draft.draft_id}">${draft.draft_id === activeId ? "当前初稿" : "设为当前"}</button>`);
      actions.push(`<a href="${fileHref(detail.workspace_rel, `source_drafts/${draft.draft_id}/source.tex`)}" target="_blank">Open TEX</a>`);
      if (draft.preview_pdf) {
        actions.push(`<a href="${fileHref(detail.workspace_rel, `source_drafts/${draft.draft_id}/${draft.preview_pdf}`)}" target="_blank">Preview PDF</a>`);
      }
      actions.push(`<button class="recycle-item-btn" data-kind="source_drafts" data-item-id="${draft.draft_id}">删除</button>`);
      return historyItemRow(
        `<strong>${htmlEscape(draft.draft_name || draft.draft_id)}</strong><br><small>${htmlEscape(draft.source_profile || "generic")} · ${timeAgo(draft.created_at)}</small>`,
        actions.join("")
      );
    })
    .join("");
}

function renderTemplates(detail) {
  const activeId = detail.config?.active_template_id || null;
  const templates = detail.templates || [];
  if (!templates.length) return `<div class="sub">暂无导入模板目录。</div>`;
  return templates
    .map((template) => {
      const actions = [];
      actions.push(`<button class="set-active-template-btn" data-template-id="${template.template_id}">${template.template_id === activeId ? "当前模板" : "设为当前"}</button>`);
      if (template.main_tex) {
        actions.push(`<a href="${fileHref(detail.workspace_rel, `template_library/${template.template_id}/files/${template.main_tex}`)}" target="_blank">Open Main TEX</a>`);
      }
      if (template.migration_ready) {
        actions.push(`<button class="run-migration-btn" data-template-id="${template.template_id}">迁移</button>`);
      }
      actions.push(`<button class="recycle-item-btn" data-kind="templates" data-item-id="${template.template_id}">删除</button>`);
      return historyItemRow(
        `<strong>${htmlEscape(template.template_name || template.template_id)}</strong><br><small>${htmlEscape(template.profile)} · ${template.migration_ready ? "可迁移" : "仅浏览"} · ${timeAgo(template.created_at)}</small>`,
        actions.join("")
      );
    })
    .join("");
}

function renderMigrations(detail) {
  const latestId = detail.config?.latest_migration_id || null;
  const migrations = detail.migrations || [];
  if (!migrations.length) return `<div class="sub">暂无迁移记录。</div>`;
  return migrations
    .map((migration) => {
      const actions = [];
      if (migration.output?.workspace_tex) {
        actions.push(`<a href="${fileHref(detail.workspace_rel, migration.output.workspace_tex)}" target="_blank">Open TEX</a>`);
      }
      if (migration.output?.workspace_pdf) {
        actions.push(`<a href="${fileHref(detail.workspace_rel, migration.output.workspace_pdf)}" target="_blank">Open PDF</a>`);
      }
      actions.push(`<a href="${fileHref(detail.workspace_rel, `migrations/${migration.migration_id}/output/compile_output.log`)}" target="_blank">Compile Log</a>`);
      actions.push(`<button class="recycle-item-btn" data-kind="migrations" data-item-id="${migration.migration_id}">删除</button>`);
      return historyItemRow(
        `<strong>${htmlEscape(migration.migration_id)}</strong><br><small>${htmlEscape(migration.source_profile)} → ${htmlEscape(migration.target_profile)} · ${migration.migration_id === latestId ? "最新" : "历史"} · ${timeAgo(migration.created_at)}</small>`,
        actions.join("")
      );
    })
    .join("");
}

function renderRecycleBin(detail) {
  const items = detail.deleted_items || [];
  if (!items.length) return `<div class="sub">回收站为空。</div>`;
  return items
    .map((item) =>
      historyItemRow(
        `<strong>${htmlEscape(item.original_item_id || item.recycle_id)}</strong><br><small>${htmlEscape(item.original_kind)} · ${timeAgo(item.created_at)}</small>`,
        `<button class="restore-item-btn" data-recycle-id="${item.recycle_id}">恢复</button>`
      )
    )
    .join("");
}

function renderRunList(frontendRuns) {
  if (!frontendRuns || !frontendRuns.length) return `<div class="sub">暂无前端触发的运行记录。</div>`;
  return frontendRuns
    .map((run) => {
      const active = run.run_id === selectedRunId;
      return `
        <div class="mini-item run-row" data-run-id="${run.run_id}" style="border-color:${active ? "rgba(164,73,42,0.42)" : "var(--line)"}">
          <div style="display:flex;justify-content:space-between;gap:12px">
            <div>
              <div style="font-weight:600">${htmlEscape(run.entry)} · ${htmlEscape(run.run_id)}</div>
              <div style="font-size:12px;color:#675d52;margin-top:4px">${timeAgo(run.started_at)}</div>
            </div>
            <div>${statusPill(run.status)}</div>
          </div>
        </div>`;
    })
    .join("");
}

function renderV3Status(detail) {
  const provider = detail.provider_status || detail.v3?.provider || {};
  const coverage = detail.claim_coverage || detail.v3?.claim_coverage || {};
  const releaseGate = detail.release_gate || detail.v3?.release_gate || {};
  const approvals = detail.approvals || detail.v3?.approvals || [];
  const previews = detail.artifact_previews || detail.v3?.artifact_previews || [];
  const coveragePercent = Math.round(Number(coverage.coverage_ratio || 0) * 100);
  const providerConfig = provider.config || {};
  const releaseChecks = releaseGate.checks || {};
  const workflowStatus = detail.v3?.workflow?.status || detail.resume?.status || "READY";
  return `
    <div style="display:grid;gap:14px">
      <div class="metrics">
        <div class="metric"><div class="label">Execution Profile</div><div class="value" style="font-size:20px">${htmlEscape(detail.profile || "full")}</div></div>
        <div class="metric"><div class="label">Workflow</div><div class="value" style="font-size:20px">${htmlEscape(workflowStatus)}</div></div>
        <div class="metric"><div class="label">Claim Coverage</div><div class="value" style="font-size:20px">${coveragePercent}%</div></div>
        <div class="metric"><div class="label">Release Gate</div><div class="value" style="font-size:20px;color:${releaseGate.passed ? "#2d6a4f" : "#a12626"}">${releaseGate.passed ? "PASS" : "BLOCKED"}</div></div>
      </div>
      <div class="mini-item">
        <strong>Provider</strong>
        <div class="sub" style="margin-top:6px">
          ${htmlEscape(provider.status || "UNKNOWN")} ·
          ${htmlEscape(providerConfig.provider || "unknown")} /
          ${htmlEscape(providerConfig.model || detail.config?.writeup_model || "unknown")}
          ${providerConfig.base_url ? ` · ${htmlEscape(providerConfig.base_url)}` : ""}
        </div>
      </div>
      <div class="mini-item">
        <strong>Release checks</strong>
        <div class="legend">
          ${Object.entries(releaseChecks).length
            ? Object.entries(releaseChecks)
                .map(([name, passed]) => `<span class="badge ${passed ? "badge-live" : "badge-skeleton"}">${htmlEscape(name)}: ${passed ? "pass" : "blocked"}</span>`)
                .join("")
            : `<span class="sub">尚无 authoritative release gate 结果。</span>`}
        </div>
      </div>
      <div class="mini-item">
        <strong>Approvals (${approvals.length})</strong>
        <div style="margin-top:8px">
          ${approvals.length
            ? approvals
                .slice(0, 8)
                .map((item) => `<div class="sub">${htmlEscape(item.proposal_id)} · ${htmlEscape(item.status)} · ${htmlEscape(item.approved_by || "")}</div>`)
                .join("")
            : `<div class="sub">暂无审批记录。</div>`}
        </div>
      </div>
      <div class="mini-item">
        <strong>Artifact preview (${previews.length})</strong>
        <div class="actions">
          ${previews.length
            ? previews
                .slice(0, 20)
                .map((item) => `<a href="${htmlEscape(item.url)}" target="_blank" rel="noopener">${htmlEscape(item.path)} (${htmlEscape(item.kind)})</a>`)
                .join("")
            : `<span class="sub">暂无可预览产物。</span>`}
        </div>
      </div>
      <div class="mini-item">
        <strong>V3 CLI gates</strong>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px">
          <label style="display:grid;gap:6px;font-size:13px"><span>Proposal ID</span><input id="v3-proposal-id" placeholder="proposal_..." value="${htmlEscape(detail.resume?.proposal_id || "")}" /></label>
          <label style="display:grid;gap:6px;font-size:13px"><span>Publish Template</span><select id="v3-publish-template"><option value="generic">generic</option><option value="cvpr">cvpr</option><option value="ieee">ieee</option><option value="elsevier">elsevier</option></select></label>
        </div>
        <div class="actions">
          <button id="v3-approve-btn">Approve</button>
          <button id="v3-resume-btn" ${detail.resume?.available ? "" : "disabled"}>Resume Workflow</button>
          <button id="v3-publish-btn">Publish</button>
          <button id="v3-release-btn">Validate Release</button>
          <button id="v3-preflight-btn">Provider Preflight</button>
        </div>
      </div>
    </div>`;
}

async function uploadSourceDraft(detail) {
  const input = document.getElementById("draft-upload-input");
  const files = Array.from(input.files || []);
  if (!files.length) throw new Error("请选择至少一个源稿文件");
  await sendMultipart(workspaceApi(detail.workspace_rel, "/drafts/import"), files);
  input.value = "";
  await render();
}

async function uploadTemplate(detail) {
  const input = document.getElementById("template-upload-input");
  const files = Array.from(input.files || []);
  if (!files.length) throw new Error("请选择模板目录");
  await sendMultipart(workspaceApi(detail.workspace_rel, "/templates/import"), files);
  input.value = "";
  await render();
}

async function runMigration(detail, templateId = null) {
  const sourceDraftId =
    document.getElementById("migration-source-draft")?.value ||
    detail.config?.active_source_draft_id;
  const template_id =
    templateId ||
    document.getElementById("migration-template")?.value ||
    detail.config?.active_template_id;
  const outputName = document.getElementById("migration-output-name")?.value?.trim() || "migration";
  if (!sourceDraftId) throw new Error("请先选择源稿");
  if (!template_id) throw new Error("请先选择模板");
  const result = await sendJSON("POST", workspaceApi(detail.workspace_rel, "/migrations"), {
    source_draft_id: sourceDraftId,
    template_id,
    output_name: outputName,
  });
  selectedRunId = result.run_id;
  selectedLogOffset = 0;
  selectedLogText = "";
  await render();
}

async function saveConfig(detail) {
  const payload = {
    writeup_model: document.getElementById("cfg-writeup-model").value.trim() || "gpt-5.4-xhigh",
    gateway_profile: document.getElementById("cfg-gateway-profile").value,
    existing_draft: document.getElementById("cfg-existing-draft").value.trim() || null,
    skip_chktex_fix: document.getElementById("cfg-skip-chktex-fix").checked,
    active_source_draft_id: detail.config?.active_source_draft_id || null,
    active_template_id: detail.config?.active_template_id || null,
    latest_migration_id: detail.config?.latest_migration_id || null,
  };
  await sendJSON("PUT", workspaceApi(detail.workspace_rel, "/config"), payload);
  await render();
}

async function setActiveConfig(detail, patch) {
  const payload = {
    ...(detail.config || {}),
    ...patch,
  };
  await sendJSON("PUT", workspaceApi(detail.workspace_rel, "/config"), payload);
  await render();
}

async function recycleArtifact(detail, kind, itemId) {
  await sendJSON(
    "POST",
    workspaceApi(detail.workspace_rel, `/artifacts/${encodeURIComponent(kind)}/${encodeURIComponent(itemId)}/recycle`),
    {}
  );
  await render();
}

async function restoreArtifact(detail, recycleId) {
  await sendJSON(
    "POST",
    workspaceApi(detail.workspace_rel, `/recycle/${encodeURIComponent(recycleId)}/restore`),
    {}
  );
  await render();
}

async function controlRun(action) {
  if (!selectedRunId) return;
  await sendJSON("POST", `/api/runs/${selectedRunId}/action`, { action });
  await render();
}

async function launchWorkflow(detail, entry, extra = {}) {
  const payload = {
    entry,
    workspace_rel: detail.workspace_rel,
    profile: detail.profile || "full",
    experiment: detail.workspace_rel.split("/")[0],
    gateway_profile: detail.config?.gateway_profile || "safe",
    writeup_model: detail.config?.writeup_model || "gpt-5.4-xhigh",
    ...extra,
  };
  const result = await sendJSON("POST", "/api/runs", payload);
  selectedRunId = result.run_id;
  selectedLogOffset = 0;
  selectedLogText = "";
  await render();
}

async function launchV3Action(detail, entry, extra = {}) {
  const result = await sendJSON("POST", "/api/runs", {
    entry,
    workspace_rel: detail?.workspace_rel || null,
    profile: detail?.profile || "full",
    ...extra,
  });
  selectedRunId = result.run_id;
  selectedLogOffset = 0;
  selectedLogText = "";
  if (detail) await render();
  return result;
}

async function refreshSelectedLog() {
  if (!selectedWorkspaceRel || !selectedRunId) return;
  try {
    const payload = await fetchRunLog(selectedWorkspaceRel, selectedRunId, selectedLogOffset);
    if (payload.text) {
      selectedLogText += payload.text;
      selectedLogOffset = payload.next_offset || selectedLogOffset;
      const panel = document.getElementById("log-panel");
      if (panel) {
        panel.textContent = selectedLogText;
        panel.scrollTop = panel.scrollHeight;
      }
    }
  } catch (_) {
    // ignore refresh races
  }
}

function renderLauncher() {
  return `
    <div class="card panel span-12">
      <h2>控制台启动器</h2>
      <div class="sub">点击直接启动现有流程，或在右侧详情里导入初稿、模板目录并执行迁移。</div>
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:16px">
        <label style="display:grid;gap:6px;font-size:13px"><span>入口</span><select id="launch-entry"><option value="mvp">mvp</option><option value="scientist">scientist</option><option value="research_partner">research_partner</option><option value="writeup">writeup</option></select></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Profile</span><select id="launch-profile"><option value="writing-only">writing-only</option><option value="research">research</option><option value="full" selected>full</option></select></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Phase</span><select id="launch-phase"><option value="bootstrap">bootstrap</option><option value="feedback">feedback</option><option value="optimize">optimize</option><option value="refine" selected>refine</option><option value="cloud">cloud</option><option value="all">all</option></select></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Gateway</span><select id="launch-gateway"><option value="safe" selected>safe</option><option value="full">full</option></select></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Workspace</span><input id="launch-workspace" placeholder="paper_writer/202604..." value="${selectedWorkspaceRel || ""}" /></label>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:12px">
        <label style="display:grid;gap:6px;font-size:13px"><span>Experiment</span><input id="launch-experiment" value="paper_writer" /></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Idea Name</span><input id="launch-idea-name" placeholder="paper_writer_user_entry" /></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Title</span><input id="launch-title" placeholder="Workspace title" /></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Existing Draft</span><input id="launch-existing-draft" placeholder="source_drafts/.../source.tex" /></label>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:12px;margin-top:12px">
        <label style="display:grid;gap:6px;font-size:13px"><span>Description</span><input id="launch-description" placeholder="Description" /></label>
        <label style="display:grid;gap:6px;font-size:13px"><span>Writeup Model</span><input id="launch-writeup-model" value="gpt-5.4-xhigh" /></label>
        <div style="display:flex;align-items:end"><button id="launch-run-btn">▶ 运行</button></div>
      </div>
    </div>`;
}

function renderDetail(detail) {
  const state = detail.state || {};
  const phase = state.phase || state.current_phase || "—";
  const frontendRuns = detail.frontend_runs || [];
  const activeRun = frontendRuns.find((run) => run.run_id === selectedRunId) || frontendRuns[0] || null;
  const pdfPath = latestPdf(detail);
  const sourceOptions = (detail.source_drafts || [])
    .map((draft) => `<option value="${draft.draft_id}" ${draft.draft_id === detail.config?.active_source_draft_id ? "selected" : ""}>${htmlEscape(draft.draft_name || draft.draft_id)}</option>`)
    .join("");
  const templateOptions = (detail.templates || [])
    .filter((item) => item.migration_ready)
    .map((item) => `<option value="${item.template_id}" ${item.template_id === detail.config?.active_template_id ? "selected" : ""}>${htmlEscape(item.template_name || item.template_id)}</option>`)
    .join("");

  return `
    <div style="display:flex;flex-direction:column;gap:18px">
      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">当前阶段</div>
        <div style="font-size:18px;font-weight:700;color:${phaseColor(phase)}">${htmlEscape(PHASE_LABEL[phase] || phase)}</div>
        ${renderPhaseBar(phase)}
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">PaperForge V3 Status</div>
        ${renderV3Status(detail)}
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Workflow Controls</div>
        <div class="actions">
          ${["feedback", "optimize", "refine", "cloud"].map((phaseName) => `<button class="phase-run-btn" data-phase="${phaseName}">▶ ${phaseName}</button>`).join("")}
          <button id="run-writeup-btn">Run Writeup</button>
          <button id="run-scientist-btn">Run Scientist</button>
          <button id="run-rp-btn">Run Research Partner</button>
          <button id="pause-run-btn" ${activeRun ? "" : "disabled"}>Pause</button>
          <button id="resume-run-btn" ${activeRun ? "" : "disabled"}>Resume</button>
          <button id="stop-run-btn" ${activeRun ? "" : "disabled"}>Stop</button>
          <button id="open-pdf-btn" ${pdfPath ? "" : "disabled"}>Open PDF</button>
        </div>
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Source Drafts</div>
        <div class="actions">
          <input id="draft-upload-input" type="file" multiple />
          <button id="upload-draft-btn">导入初稿</button>
        </div>
        <div style="margin-top:12px">${renderSourceDrafts(detail)}</div>
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Template Library</div>
        <div class="actions">
          <input id="template-upload-input" type="file" webkitdirectory directory multiple />
          <button id="upload-template-btn">导入模板目录</button>
        </div>
        <div style="margin-top:12px">${renderTemplates(detail)}</div>
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Migration Runs</div>
        <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:12px">
          <label style="display:grid;gap:6px;font-size:13px">
            <span>Source Draft</span>
            <select id="migration-source-draft">${sourceOptions || `<option value="">无可用源稿</option>`}</select>
          </label>
          <label style="display:grid;gap:6px;font-size:13px">
            <span>Template</span>
            <select id="migration-template">${templateOptions || `<option value="">无可用模板</option>`}</select>
          </label>
          <label style="display:grid;gap:6px;font-size:13px">
            <span>Output Name</span>
            <input id="migration-output-name" value="migration" />
          </label>
        </div>
        <div class="actions">
          <button id="run-migration-main-btn">执行模板迁移</button>
        </div>
        <div style="margin-top:12px">${renderMigrations(detail)}</div>
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Recycle Bin</div>
        <div>${renderRecycleBin(detail)}</div>
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Workspace Config</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <label style="display:grid;gap:6px;font-size:13px"><span>Writeup Model</span><input id="cfg-writeup-model" value="${htmlEscape(detail.config?.writeup_model || "gpt-5.4-xhigh")}" /></label>
          <label style="display:grid;gap:6px;font-size:13px"><span>Gateway Profile</span><select id="cfg-gateway-profile"><option value="safe" ${detail.config?.gateway_profile === "safe" ? "selected" : ""}>safe</option><option value="full" ${detail.config?.gateway_profile === "full" ? "selected" : ""}>full</option></select></label>
          <label style="display:grid;gap:6px;font-size:13px;grid-column:span 2"><span>Existing Draft</span><input id="cfg-existing-draft" value="${htmlEscape(detail.config?.existing_draft || "")}" /></label>
          <label style="display:flex;align-items:center;gap:8px;font-size:13px"><input type="checkbox" id="cfg-skip-chktex-fix" ${detail.config?.skip_chktex_fix ? "checked" : ""} /><span>Skip chktex auto-fix</span></label>
        </div>
        <div class="actions"><button id="save-config-btn">Save Config</button></div>
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Run History</div>
        <div class="mini-list">${renderRunList(frontendRuns)}</div>
      </div>

      <div>
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#675d52;margin-bottom:8px">Log</div>
        <pre id="log-panel" style="white-space:pre-wrap;word-break:break-word;background:#261d15;color:#f9f1e7;padding:14px;border-radius:12px;font-size:11px;line-height:1.6;max-height:320px;overflow:auto;margin:0">${htmlEscape(selectedLogText || "选择一条运行记录以查看日志。")}</pre>
      </div>
    </div>`;
}

async function launchFromForm() {
  const payload = {
    entry: document.getElementById("launch-entry").value,
    profile: document.getElementById("launch-profile").value,
    phase: document.getElementById("launch-phase").value,
    gateway_profile: document.getElementById("launch-gateway").value,
    workspace_rel: document.getElementById("launch-workspace").value.trim() || null,
    experiment: document.getElementById("launch-experiment").value.trim() || "paper_writer",
    idea_name: document.getElementById("launch-idea-name").value.trim() || null,
    title: document.getElementById("launch-title").value.trim() || null,
    description: document.getElementById("launch-description").value.trim() || null,
    writeup_model: document.getElementById("launch-writeup-model").value.trim() || "gpt-5.4-xhigh",
    existing_draft: document.getElementById("launch-existing-draft").value.trim() || null,
  };
  if (payload.entry === "writeup") payload.workflow_kind = "mvp";
  const result = await sendJSON("POST", "/api/runs", payload);
  selectedRunId = result.run_id;
  selectedLogOffset = 0;
  selectedLogText = "";
  await render();
}

async function render() {
  const root = document.getElementById("app");
  try {
    let workspacePayload;
    try {
      workspacePayload = await fetchWorkspaces();
    } catch (e) {
      root.innerHTML = `<div style="padding:40px;color:#a4492a">⚠ 无法连接到后端服务: ${htmlEscape(e.message)}<br><small>请确保 python frontend/server.py 已运行</small></div>`;
      return;
    }
    const workspaces = workspacePayload.workspaces || [];
    if (selectedWorkspaceRel && !workspaces.find((ws) => ws.workspace_rel === selectedWorkspaceRel)) {
      selectedWorkspaceRel = null;
      selectedRunId = null;
      selectedLogOffset = 0;
      selectedLogText = "";
    }
    let detail = null;
    if (selectedWorkspaceRel) {
      detail = await fetchWorkspaceDetail(selectedWorkspaceRel);
      const frontendRuns = detail.frontend_runs || [];
      if (!selectedRunId && frontendRuns.length) {
        selectedRunId = frontendRuns[0].run_id;
        selectedLogOffset = 0;
        selectedLogText = "";
      }
    }

    root.innerHTML = `
    <div style="width:min(1320px,calc(100vw - 32px));margin:0 auto;padding:28px 0 48px">
      <div style="margin-bottom:24px">
        <div style="display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:rgba(164,73,42,0.08);color:#a4492a;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;margin-bottom:12px">● INTERACTIVE</div>
        <h1 style="font-family:'Iowan Old Style','Palatino Linotype',Georgia,serif;font-size:clamp(34px,4vw,54px);margin:0 0 8px;letter-spacing:-0.02em">PaperForge 工作台</h1>
        <div style="color:#675d52;font-size:14px">导入 LaTeX 初稿、导入模板目录、执行模板迁移，并直接控制工作流运行。</div>
      </div>

      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px">
        <div class="pill">📁 <strong>${workspaces.length}</strong> 个 Workspace</div>
        <div class="pill">⏱ 上次刷新: <strong>${new Date().toLocaleTimeString("zh-CN")}</strong></div>
        <div class="pill">🖱 可交互控制已启用</div>
      </div>

      <div class="grid">
        ${renderLauncher()}
        <div class="card panel span-${selectedWorkspaceRel ? "5" : "12"}">
          <h2>Workspace 列表</h2>
          <div class="sub">点击任意 workspace 查看初稿、模板、迁移记录、回收站和日志。</div>
          <div style="display:grid;gap:12px;margin-top:16px">
            ${workspaces.length ? workspaces.map(renderWorkspaceCard).join("") : `<div class="sub">暂无 Workspace。</div>`}
          </div>
        </div>
        ${selectedWorkspaceRel ? `<div class="card panel span-7"><h2>Workspace 详情</h2><div class="sub" style="margin-bottom:16px">${htmlEscape(selectedWorkspaceRel)}</div>${renderDetail(detail)}</div>` : ""}
      </div>
    </div>`;

    root.querySelectorAll(".ws-card").forEach((card) => {
      card.addEventListener("mouseenter", () => (card.style.boxShadow = "0 8px 24px rgba(54,37,20,0.14)"));
      card.addEventListener("mouseleave", () => (card.style.boxShadow = ""));
      card.addEventListener("click", async () => {
        selectedWorkspaceRel = card.dataset.workspaceRel;
        selectedRunId = null;
        selectedLogOffset = 0;
        selectedLogText = "";
        await render();
      });
    });

    const launchBtn = document.getElementById("launch-run-btn");
    if (launchBtn) launchBtn.addEventListener("click", () => launchFromForm().catch((e) => alert(e.message)));

    if (detail) {
      document.getElementById("upload-draft-btn")?.addEventListener("click", () => uploadSourceDraft(detail).catch((e) => alert(e.message)));
      document.getElementById("upload-template-btn")?.addEventListener("click", () => uploadTemplate(detail).catch((e) => alert(e.message)));
      document.getElementById("run-migration-main-btn")?.addEventListener("click", () => runMigration(detail).catch((e) => alert(e.message)));
      document.getElementById("save-config-btn")?.addEventListener("click", () => saveConfig(detail).catch((e) => alert(e.message)));
      document.getElementById("open-pdf-btn")?.addEventListener("click", () => {
        const pdf = latestPdf(detail);
        if (pdf) window.open(fileHref(detail.workspace_rel, pdf), "_blank");
      });
      document.getElementById("v3-approve-btn")?.addEventListener("click", () => {
        const proposalId = document.getElementById("v3-proposal-id")?.value?.trim();
        if (!proposalId) {
          alert("请输入 Proposal ID");
          return;
        }
        launchV3Action(detail, "approve", { proposal_id: proposalId }).catch((e) => alert(e.message));
      });
      document.getElementById("v3-resume-btn")?.addEventListener("click", () =>
        launchV3Action(detail, "resume").catch((e) => alert(e.message))
      );
      document.getElementById("v3-publish-btn")?.addEventListener("click", () =>
        launchV3Action(detail, "publish", {
          template: document.getElementById("v3-publish-template")?.value || "generic",
        }).catch((e) => alert(e.message))
      );
      document.getElementById("v3-release-btn")?.addEventListener("click", () =>
        launchV3Action(detail, "release").catch((e) => alert(e.message))
      );
      document.getElementById("v3-preflight-btn")?.addEventListener("click", () =>
        launchV3Action(detail, "preflight", { live_provider: true }).catch((e) => alert(e.message))
      );
      document.getElementById("run-writeup-btn")?.addEventListener("click", () => launchWorkflow(detail, "writeup", { workflow_kind: "mvp" }).catch((e) => alert(e.message)));
      document.getElementById("run-scientist-btn")?.addEventListener("click", () => launchWorkflow(detail, "scientist").catch((e) => alert(e.message)));
      document.getElementById("run-rp-btn")?.addEventListener("click", () => launchWorkflow(detail, "research_partner", { title: detail.state?.idea_name || detail.workspace_rel }).catch((e) => alert(e.message)));
      document.getElementById("pause-run-btn")?.addEventListener("click", () => controlRun("pause").catch((e) => alert(e.message)));
      document.getElementById("resume-run-btn")?.addEventListener("click", () => controlRun("resume").catch((e) => alert(e.message)));
      document.getElementById("stop-run-btn")?.addEventListener("click", () => controlRun("stop").catch((e) => alert(e.message)));

      root.querySelectorAll(".phase-run-btn").forEach((btn) => {
        btn.addEventListener("click", () => launchWorkflow(detail, "mvp", { phase: btn.dataset.phase }).catch((e) => alert(e.message)));
      });
      root.querySelectorAll(".run-migration-btn").forEach((btn) => {
        btn.addEventListener("click", () => runMigration(detail, btn.dataset.templateId).catch((e) => alert(e.message)));
      });
      root.querySelectorAll(".set-active-draft-btn").forEach((btn) => {
        btn.addEventListener("click", () => setActiveConfig(detail, { active_source_draft_id: btn.dataset.draftId }).catch((e) => alert(e.message)));
      });
      root.querySelectorAll(".set-active-template-btn").forEach((btn) => {
        btn.addEventListener("click", () => setActiveConfig(detail, { active_template_id: btn.dataset.templateId }).catch((e) => alert(e.message)));
      });
      root.querySelectorAll(".recycle-item-btn").forEach((btn) => {
        btn.addEventListener("click", () => recycleArtifact(detail, btn.dataset.kind, btn.dataset.itemId).catch((e) => alert(e.message)));
      });
      root.querySelectorAll(".restore-item-btn").forEach((btn) => {
        btn.addEventListener("click", () => restoreArtifact(detail, btn.dataset.recycleId).catch((e) => alert(e.message)));
      });
      root.querySelectorAll(".run-row").forEach((row) => {
        row.addEventListener("click", () => {
          selectedRunId = row.dataset.runId;
          selectedLogOffset = 0;
          selectedLogText = "";
          refreshSelectedLog();
        });
      });
    }
  } catch (e) {
    console.error("render failed", e);
    renderFatal(e?.message || "Unknown render error", e?.stack || "");
  }
}

async function refreshSelectedLog() {
  if (!selectedWorkspaceRel || !selectedRunId) return;
  try {
    const payload = await fetchRunLog(selectedWorkspaceRel, selectedRunId, selectedLogOffset);
    if (payload.text) {
      selectedLogText += payload.text;
      selectedLogOffset = payload.next_offset || selectedLogOffset;
      const panel = document.getElementById("log-panel");
      if (panel) {
        panel.textContent = selectedLogText;
        panel.scrollTop = panel.scrollHeight;
      }
    }
  } catch (_) {
    // ignore
  }
}

window.addEventListener("error", (event) => {
  console.error("window error", event.error || event.message);
  renderFatal(event.message || "Unhandled error", event.error?.stack || "");
});

window.addEventListener("unhandledrejection", (event) => {
  console.error("unhandled rejection", event.reason);
  renderFatal(event.reason?.message || "Unhandled rejection", event.reason?.stack || String(event.reason || ""));
});

ensureSession()
  .then(() => render())
  .catch((e) => renderFatal(e?.message || "Initial render failed", e?.stack || ""));
setInterval(() => {
  render().catch((e) => renderFatal(e?.message || "Refresh render failed", e?.stack || ""));
}, REFRESH_MS);
setInterval(() => {
  refreshSelectedLog().catch(() => {});
}, LOG_REFRESH_MS);
