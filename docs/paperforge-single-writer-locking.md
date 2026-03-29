# PaperForge Single-Writer Workspace Lock / Lease 设计说明

## 1. 文档目的

本文档用于定义 PaperForge 后续升级所需的 **single-writer workspace lock / lease** 机制，目标是把当前系统隐含的“单 workspace 单写者”假设显式化，并为：

- CLI
- Agent
- Frontend
- OpsAgent
- MCP 写入能力

提供统一的写入门禁。

本文档不是当前代码实现说明，而是后续实施的设计基线。

---

## 2. 为什么必须前置

当前系统中，多个关键文件都是直接覆盖写或原地修改：

- `workflow_state.json`
- `workflow_idea.json`
- `writeup_checkpoint.json`
- `notes.txt`
- `template.tex`
- `artifacts/upload_manifest.json`
- `latex/checkpoints/*.tex`

而当前安全边界主要依赖两个隐含前提：

1. scientist 并行只在 **不同 idea 目录** 之间发生
2. mvp / writeup 默认只有 **一个写者** 在操作同一 workspace

一旦后续接入：

- 前端触发写操作
- OpsAgent
- MCP 文件写回
- 文献管理器回写
- AIGC / 降重链回写

如果没有统一锁机制，就容易发生：

- 状态覆盖
- checkpoint 错乱
- `template.tex` 被交叉改写
- `notes.txt` block 互相覆盖
- 上传回填与 writeup 冲突

---

## 3. 设计目标

single-writer 机制必须满足：

1. **同一 workspace 任一时刻只有一个写者**
2. scientist 跨 idea 并行仍然允许
3. 支持重入策略
4. 支持 stale lock 恢复
5. 能覆盖 CLI / Agent / Frontend / OpsAgent / MCP
6. 不要求大规模分布式锁系统，优先本地 workspace 粒度稳定性

---

## 4. 锁粒度

## 4.1 推荐粒度：workspace 粒度

锁的基本单位应为：

- mvp：单个 `workspace/`
- scientist：单个 `idea_folder/`

也就是：

> 一个结果目录 / workspace = 一个锁域

### 原因
这是当前文件写入集中发生的真实粒度。

---

## 4.2 不推荐的粒度

### 文件粒度
不推荐。  
因为当前操作通常是跨多个文件的组合写入，例如：

- `template.tex`
- `writeup_checkpoint.json`
- PDF
- `notes.txt`

如果按文件加锁，会让原子性难以保证。

### 全局进程粒度
不推荐。  
会破坏 scientist 的跨 idea 并行能力。

---

## 5. 受保护的写操作范围

以下写操作必须进入 lock 保护域：

### 5.1 mvp 写入
- bootstrap 初始化
- feedback ingest
- optimize run feedback 回写
- refine writeup
- cloud sync backfill

### 5.2 writeup 写入
- `template.tex`
- `writeup_checkpoint.json`
- `latex/checkpoints/*.tex`
- 编译产物 PDF
- sanitize / cite / refine / latex_fix

### 5.3 artifact / state 写入
- `workflow_state.json`
- `workflow_idea.json`
- `upload_manifest.json`
- `notes.txt`

### 5.4 前端触发写入
- 任意 phase trigger
- 任意 artifact rewrite
- 任意 notes / config / upload apply

### 5.5 MCP / OpsAgent 写入
- 远程结果回填
- 文献管理器回写
- 架构图文件写入
- 未来 AIGC / 降重链回写

---

## 6. 锁实现建议

## 6.1 首选：workspace lock 文件 + lease 元数据

推荐在每个 workspace 下维护一个锁文件，例如：

```text
<workspace>/.paperforge.lock
```

锁内容建议包含：

```json
{
  "lock_id": "uuid",
  "owner_type": "cli|agent|frontend|ops|mcp",
  "owner_id": "string",
  "pid": 12345,
  "hostname": "machine-name",
  "acquired_at": "iso8601",
  "last_heartbeat_at": "iso8601",
  "intent": "bootstrap|feedback|writeup|cloud_sync|notes_update",
  "reentrant_token": "optional"
}
```

---

## 6.2 行为要求

### 获取锁
- 若无锁，则创建并进入写区
- 若已有有效锁，则拒绝第二写者进入
- 若持锁者允许重入且 token 匹配，则允许重入

### 保活
- 长任务应刷新 `last_heartbeat_at`

### 释放锁
- 写操作完成后主动释放

---

## 7. 重入策略

## 7.1 为什么需要重入
某些流程是嵌套写操作，例如：

- workflow agent 进入 refine
- refine 内部调用 writeup agent
- writeup agent 继续更新 checkpoint / tex / pdf

如果完全禁止重入，内部流程会把自己锁死。

---

## 7.2 推荐方案
允许 **同 owner、同 lock context** 的重入。

### 判定条件
满足以下任一可重入：

1. `lock_id` 一致
2. `owner_id` 一致且携带同一 `reentrant_token`
3. 由上层 agent 显式传递 lock context 给下层调用

### 规则
- 重入只能发生在同一工作流上下文中
- 不允许不同前端会话伪装成同一 owner 重入

---

## 8. stale lock 恢复策略

## 8.1 为什么需要
长任务可能异常退出，导致锁残留。

### 可能来源
- 进程崩溃
- IDE/终端强杀
- 前端断线
- 远程任务回调中断

---

## 8.2 推荐判定方式
若满足以下条件之一，可认为锁 stale：

1. `pid` 对应进程已不存在（本机场景）
2. 超过 `lease_timeout_sec`
3. 超过 `heartbeat_timeout_sec`
4. owner 是前端临时会话且 session 已失效

---

## 8.3 stale lock 恢复动作
推荐流程：

1. 先做 stale 判定
2. 记录 stale lock 事件
3. 执行安全接管
4. 新写者重新获取锁
5. 在日志/trace 中标记为“stale lock recovered”

### 禁止行为
- 未判定 stale 就直接覆盖锁
- 忽略锁直接写文件

---

## 9. scientist 与 mvp 的不同处理

## 9.1 scientist
scientist 并行是跨 idea 目录的，因此：

- 每个 `idea_folder` 各自拥有独立锁
- worker 之间不会竞争同一锁
- 不需要全局串行化 scientist

### 结论
scientist 并行能力可保留。

---

## 9.2 mvp
mvp 针对单 workspace 深度写入，必须严格单写者。

### 结论
mvp 各 phase、writeup、feedback、cloud backfill 都必须先拿 workspace 锁。

---

## 10. 与当前代码的映射关系

后续落地时，以下模块优先接 lock：

### 首批接入点
- `launch_mvp_workflow.py`
- `engine/perform_writeup.py`
- `engine/mvp_workflow.py`
- 未来 `PaperForgeCoordinator`
- 未来 `MvpWorkflowAgent`
- 未来 `WriteupAgent`

### 第二批接入点
- `launch_scientist.py` 中对单个 idea 目录的写流程
- Frontend trigger
- OpsAgent
- 文件/回填 MCP

---

## 11. 锁与 owner 边界

推荐 owner_type 枚举：

- `cli`
- `agent`
- `frontend`
- `ops`
- `mcp`

推荐 intent 枚举：

- `bootstrap`
- `feedback`
- `optimize`
- `refine`
- `writeup`
- `cloud_sync`
- `notes_update`
- `artifact_update`
- `review`
- `improvement`

这样 future trace / frontend 就能知道：

- 谁拿了锁
- 为了什么拿锁
- 卡在哪一步

---

## 12. 前端与 MCP 的强约束

本文档明确规定：

### 前端
- 不允许绕过 lock 直接写盘
- 不允许直接修改 `workflow_state.json`
- 不允许直接修改 `template.tex`

### MCP
- 不允许作为独立写者随意回写
- 必须通过受控 service / agent 持锁后写入
- 文献管理器 / 回填 / 图表 / AIGC 回写都一样

---

## 13. 验收标准

single-writer lock / lease 落地后，至少满足以下验收项：

### L-01
scientist 仍然支持跨 idea 并行。

### L-02
同一 workspace 不允许两个写入口同时修改。

### L-03
前端触发写操作前必须先获取锁。

### L-04
OpsAgent 写操作前必须先获取锁。

### L-05
MCP 写操作不得绕过锁。

### L-06
支持重入，不发生自锁死。

### L-07
支持 stale lock 恢复。

---

## 14. 实施建议顺序

### 第一步
定义 lock 文件格式与 API。

### 第二步
优先接入：
- mvp phase
- writeup

### 第三步
接入：
- scientist 单个 idea 执行
- frontend trigger
- ops / mcp 回写

### 第四步
把 lock 状态暴露给 trace / frontend。

---

## 15. 最终结论

single-writer 机制不是附加优化，而是当前 PaperForge 从“单入口脚本系统”升级为“多入口 Agent / Frontend / MCP 系统”的前置条件。

一句话总结：

> **如果不先补 workspace lock / lease，后续前端、OpsAgent、MCP 一接上，就会破坏当前系统隐含但关键的单写者语义。**

因此，这个文档定义的不是可选增强，而是实施前必须先落地的基础设施约束。
