const state = {
  title: "PaperForge Console",
  subtitle: "前端现在由 app.js 单点渲染，展示内容只指向仓库中真实存在的 Agent / Skill / MCP / 文档与最小桥接层。",
  release: "bridge-baseline-v2",
  frontendMode: "static-rendered",
  workflows: [
    {
      id: "scientist",
      title: "Scientist",
      stages: ["idea_generation", "novelty_check", "experiment", "writeup", "review", "improvement", "re_review"],
      entrypoint: "launch_scientist.py",
      status: "bridge"
    },
    {
      id: "mvp",
      title: "MVP",
      stages: ["bootstrap", "cloud", "feedback", "optimize", "refine"],
      entrypoint: "launch_mvp_workflow.py",
      status: "bridge"
    },
    {
      id: "writeup",
      title: "Writeup",
      stages: ["init", "cite", "refine", "latex_fix", "done"],
      entrypoint: "engine/perform_writeup.py + launch_mvp_workflow.py --phase refine",
      status: "bridge"
    }
  ],
  agents: [
    {
      id: "paperforge-coordinator",
      title: "PaperForgeCoordinator",
      responsibility: "统一入口、schema 暴露、前端动作路由",
      path: "../agents/coordinator.py",
      status: "bridge"
    },
    {
      id: "scientist-workflow-agent",
      title: "ScientistWorkflowAgent",
      responsibility: "桥接现有 scientist CLI，返回统一 status / trace / artifacts",
      path: "../agents/scientist_workflow_agent.py",
      status: "bridge"
    },
    {
      id: "mvp-workflow-agent",
      title: "MvpWorkflowAgent",
      responsibility: "桥接现有 MVP phase CLI，保留现有锁与工件语义",
      path: "../agents/mvp_workflow_agent.py",
      status: "bridge"
    },
    {
      id: "writeup-agent",
      title: "WriteupAgent",
      responsibility: "桥接 scientist / mvp 的 writeup 入口，并暴露 writeup skill map",
      path: "../agents/writeup_agent.py",
      status: "bridge"
    }
  ],
  skills: [
    {
      id: "write-section",
      summary: "章节起草",
      path: "../skills/write-section",
      status: "live"
    },
    {
      id: "refine-section",
      summary: "章节精修",
      path: "../skills/refine-section",
      status: "live"
    },
    {
      id: "citation-gap",
      summary: "引文缺口识别",
      path: "../skills/citation-gap",
      status: "live"
    },
    {
      id: "citation-select",
      summary: "候选文献筛选",
      path: "../skills/citation-select",
      status: "live"
    },
    {
      id: "latex-fix",
      summary: "LaTeX 局部修复",
      path: "../skills/latex-fix",
      status: "skeleton"
    },
    {
      id: "de-aigc-rewrite",
      summary: "去机器味改写",
      path: "../skills/de-aigc-rewrite",
      status: "skeleton"
    }
  ],
  mcpServers: [
    {
      id: "literature",
      summary: "接现有文献检索引擎，支持 search / bibtex / dedupe",
      path: "../mcp_servers/literature/server.py",
      status: "live"
    },
    {
      id: "bibliography",
      summary: "最小文献管理器，支持 BibTeX 导入导出、去重、section 映射",
      path: "../mcp_servers/bibliography/server.py",
      status: "live"
    },
    {
      id: "file-gateway",
      summary: "锁感知的 workspace 快照、读写文本网关",
      path: "../mcp_servers/file-gateway/server.py",
      status: "skeleton"
    },
    {
      id: "remote-runner",
      summary: "桥接 cloud cycle / sync 入口，默认以计划模式返回命令",
      path: "../mcp_servers/remote-runner/server.py",
      status: "skeleton"
    },
    {
      id: "diagram",
      summary: "Mermaid 结构图与图注建议生成",
      path: "../mcp_servers/diagram/server.py",
      status: "skeleton"
    },
    {
      id: "aigc-eval",
      summary: "轻量重复短语与相似度启发式检查",
      path: "../mcp_servers/aigc-eval/server.py",
      status: "skeleton"
    }
  ],
  bibliography: {
    storage: "../engine/bibliography.py",
    features: [
      "workspace/artifacts/bibliography/library.json 存储模型",
      "BibTeX 导入导出",
      "按 DOI / 标题 + 年份去重",
      "section -> citation key 映射",
      "供 writeup / citation bridge 复用"
    ],
    caveat: "当前是本地文件存储和 MCP bridge，不是完整多用户文献平台。"
  },
  docs: [
    "../docs/paperforge-workflow-equivalence-spec.md",
    "../docs/paperforge-agent-skill-mcp-mapping.md",
    "../docs/paperforge-safe-upgrade-roadmap.md",
    "../docs/paperforge-equivalence-matrix.md",
    "../docs/paperforge-json-artifact-contracts.md",
    "../docs/paperforge-single-writer-locking.md"
  ],
  tests: [
    "tests/test_agent_bridges.py",
    "tests/test_bibliography.py",
    "tests/test_locking.py",
    "tests/test_smoke.py"
  ],
  backlog: [
    "动态 API / 实时任务状态还未接入",
    "MCP 远程执行和图生成仍是最小骨架",
    "writeup 真实 LLM skill 化尚未替换核心 engine 循环"
  ]
};

function badgeClass(status) {
  if (status === "live") return "badge badge-live";
  if (status === "bridge") return "badge badge-bridge";
  if (status === "doc") return "badge badge-doc";
  return "badge badge-skeleton";
}

function badgeLabel(status) {
  if (status === "live") return "live";
  if (status === "bridge") return "bridge";
  if (status === "doc") return "docs";
  return "skeleton";
}

function openPath(path) {
  window.location.href = path;
}

function renderMetrics() {
  const liveSkills = state.skills.filter(item => item.status === "live").length;
  const liveMcp = state.mcpServers.filter(item => item.status === "live").length;
  return `
    <div class="metrics">
      <div class="metric">
        <div class="label">Workflow Entrypoints</div>
        <div class="value">${state.workflows.length}</div>
        <div class="sub">Scientist / MVP / Writeup 都有真实桥接入口。</div>
      </div>
      <div class="metric">
        <div class="label">Agent Bridges</div>
        <div class="value">${state.agents.length}</div>
        <div class="sub">统一输出 command、trace、artifacts、schema。</div>
      </div>
      <div class="metric">
        <div class="label">Skill Contracts</div>
        <div class="value">${liveSkills}/${state.skills.length}</div>
        <div class="sub">4 个实装 + 2 个最小骨架，全部有 schema 和示例 I/O。</div>
      </div>
      <div class="metric">
        <div class="label">MCP Services</div>
        <div class="value">${liveMcp}/${state.mcpServers.length}</div>
        <div class="sub">文献与 bibliography 可跑，其他服务已具备统一启动面。</div>
      </div>
    </div>
  `;
}

function renderTimeline() {
  return `
    <div class="timeline">
      ${state.workflows
        .map(
          workflow => `
            <div class="step">
              <div>
                <strong>${workflow.title}</strong>
                <div class="sub">${workflow.entrypoint}</div>
              </div>
              <div><code>${workflow.stages.join(" → ")}</code></div>
              <div class="${badgeClass(workflow.status)}">${badgeLabel(workflow.status)}</div>
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

function renderTable(items, headers) {
  return `
    <table>
      <thead>
        <tr>${headers.map(item => `<th>${item}</th>`).join("")}</tr>
      </thead>
      <tbody>
        ${items.join("")}
      </tbody>
    </table>
  `;
}

function renderAgents() {
  const rows = state.agents.map(
    agent => `
      <tr>
        <td><code>${agent.title}</code></td>
        <td>${agent.responsibility}</td>
        <td><a href="${agent.path}">${agent.path.replace("../", "")}</a></td>
        <td><span class="${badgeClass(agent.status)}">${badgeLabel(agent.status)}</span></td>
      </tr>
    `
  );
  return renderTable(rows, ["Agent", "职责", "路径", "状态"]);
}

function renderSkills() {
  const rows = state.skills.map(
    skill => `
      <tr>
        <td><code>${skill.id}</code></td>
        <td>${skill.summary}</td>
        <td><a href="${skill.path}/SKILL.md">${skill.path.replace("../", "")}</a></td>
        <td><span class="${badgeClass(skill.status)}">${badgeLabel(skill.status)}</span></td>
      </tr>
    `
  );
  return renderTable(rows, ["Skill", "用途", "路径", "状态"]);
}

function renderMcp() {
  const rows = state.mcpServers.map(
    server => `
      <tr>
        <td><code>${server.id}</code></td>
        <td>${server.summary}</td>
        <td><a href="${server.path}">${server.path.replace("../", "")}</a></td>
        <td><span class="${badgeClass(server.status)}">${badgeLabel(server.status)}</span></td>
      </tr>
    `
  );
  return renderTable(rows, ["MCP", "用途", "路径", "状态"]);
}

function renderDocButtons() {
  return state.docs
    .map(path => `<button data-path="${path}">${path.split("/").pop().replace(".md", "")}</button>`)
    .join("");
}

function renderTests() {
  return `
    <div class="mini-list">
      ${state.tests
        .map(
          testPath => `
            <div class="mini-item">
              <code>${testPath}</code>
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

function render() {
  const root = document.getElementById("app");
  root.innerHTML = `
    <div class="shell">
      <section class="hero">
        <div class="card hero-main">
          <div class="eyebrow">Rendered By app.js</div>
          <h1>${state.title}</h1>
          <p class="hero-copy">${state.subtitle}</p>
          <div class="status-strip">
            <span class="pill">版本: <strong>${state.release}</strong></span>
            <span class="pill">前端模式: <strong>${state.frontendMode}</strong></span>
            <span class="pill">展示原则: <strong>show only real files</strong></span>
          </div>
        </div>
        <aside class="card hero-side">
          <h2>当前边界</h2>
          <p class="sub">这还是静态前端，没有后台 API，也不会直接改工作区。但页面里列出的 Agent / Skill / MCP / 测试文件都已经在仓库中落地。</p>
          <div class="legend">
            <span class="badge badge-live">live</span>
            <span class="badge badge-bridge">bridge</span>
            <span class="badge badge-skeleton">skeleton</span>
          </div>
        </aside>
      </section>

      <section class="card panel span-12">
        <h2>完成度</h2>
        ${renderMetrics()}
      </section>

      <section class="grid">
        <div class="card panel span-8">
          <h2>工作流与入口</h2>
          <p class="sub">工作流顺序仍由现有 CLI / engine 保持，agent 层只做统一 schema、trace、artifact 输出与路由。</p>
          ${renderTimeline()}
        </div>
        <div class="card panel span-4">
          <h2>文档与检查</h2>
          <p class="sub">README、结构图、前端状态和自动化 smoke/regression 已按当前仓库状态对齐。</p>
          <div class="actions">${renderDocButtons()}</div>
        </div>
      </section>

      <section class="grid">
        <div class="card panel span-7">
          <h2>Agent 层</h2>
          ${renderAgents()}
        </div>
        <div class="card panel span-5">
          <h2>Bibliography 最小落地</h2>
          <p class="sub"><code>${state.bibliography.storage.replace("../", "")}</code></p>
          <ul>
            ${state.bibliography.features.map(item => `<li>${item}</li>`).join("")}
          </ul>
          <p class="sub" style="margin-top: 14px;">${state.bibliography.caveat}</p>
        </div>
      </section>

      <section class="grid">
        <div class="card panel span-6">
          <h2>Skill 层</h2>
          ${renderSkills()}
        </div>
        <div class="card panel span-6">
          <h2>MCP 层</h2>
          ${renderMcp()}
        </div>
      </section>

      <section class="grid">
        <div class="card panel span-5">
          <h2>自动化检查</h2>
          <p class="sub">验收矩阵已落到 unittest smoke / lock / contract / bridge regression。</p>
          ${renderTests()}
        </div>
        <div class="card panel span-7">
          <h2>仍是骨架的部分</h2>
          <ul>
            ${state.backlog.map(item => `<li>${item}</li>`).join("")}
          </ul>
          <div class="mono-preview">${JSON.stringify(
            {
              workflows: state.workflows.length,
              agents: state.agents.length,
              skills: state.skills.length,
              mcpServers: state.mcpServers.length,
              docs: state.docs.length,
              tests: state.tests.length
            },
            null,
            2
          )}</div>
        </div>
      </section>
    </div>
  `;

  root.querySelectorAll("[data-path]").forEach(button => {
    button.addEventListener("click", () => openPath(button.dataset.path));
  });
}

render();
