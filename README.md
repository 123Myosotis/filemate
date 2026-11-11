# FileMate

本地桌面助手：用自然语言**整理文件**、**跨多张 Excel 查询**、**编辑 Word**。

> **契约是权威**：算子 / 枚举以 `contracts/versions/*.json` 为准，`pipeline/synth_contract/authority_scan.py` 会扫描漂移。
> **本地优先**：能规则判的不调模型；AI 只出"方案"，本地机械执行 + 校验。

---

## 功能

### Excel / 表格

- **跨表查询**：多表筛条件、带关联字段，生成结果表并导出。
- **表格清洗**：识别标题行 / 合并表头 / 多级表头 / 矩阵；认不出先问用户。
- **语义识别**：列名同义 / 包含（本地优先，LLM 兜底默认关）。

### Word

- **6 种 op**（全 **run 级**）：
  - `replace`（替换）、`insert`（插入）、`delete`（删除）、`format`（粗体/斜体/字号/字体）、`move`（移动）、`tracked_change`（修订痕迹）。
- **格式保留**：拆 run + 复制 `w:rPr`（前后文格式不丢）。
- **危险操作确认**：`delete` 走 `needs_confirmation`。
- **记忆**：文件主题 + 术语 + 术语↔锚点 + 生命周期 + 混合召回（FTS5 + LIKE + 向量）。

### 记忆（"小脑"）

- **即时**（当前任务，内存）vs **后台**（跨任务，SQLite）。
- **术语**：规则挖掘（**不调 LLM**）+ 术语↔锚点。
- **召回**：FTS5 + LIKE + 可选向量（`api.embedding_key`）。

---

## 环境

- Python 3.11+（开发用 3.14）
- Windows（PyQt6 桌面）

## 安装

```bash
pip install -r requirements.txt
```

## 配置

**推荐：启动后在「设置」里填**，会写入本地 `config.json`（已被 `.gitignore` 忽略）。

| 用途 | 配置项 | 说明 |
|---|---|---|
| **聊天模型** | `api_key` + `api.base_url` + `api.model` | 如 DeepSeek |
| **向量召回（可选）** | `api.embedding_key` | SiliconFlow `BAAI/bge-m3`（DeepSeek 无 embedding） |

```json
{
  "api_key": "聊天模型的 Key",
  "params": {
    "api.base_url": "https://api.deepseek.com/v1",
    "api.model": "deepseek-chat",
    "api.embedding_key": ""
  }
}
```

> - **向量可选**：`api.embedding_key` 空 → 召回自动退化为 **FTS5 + LIKE**（不报错）。
> - 旧字段 `zhipu_api_key` 仍兼容（聊天 Key 的旧名）。

## 运行

- 双击 `启动FileMate.bat`（或 `启动FileMate.vbs`）
- 或 `python main.py`

## 测试

```bash
pytest tests/ -q
```

> 沙箱环境里 `tmp_path` 可能被拒（`PermissionError`），属**环境限制**，不是代码失败。

## 目录结构

| 目录 | 说明 |
|---|---|
| `ui/` | PyQt6 界面 |
| `engine/` | 意图分发 / 执行器 |
| `agent_core/` | 意图、任务、能力、质检 |
| `ai/` | 模型 HTTP 客户端 + `embed.py` |
| `pipeline/` | 解析、分类、表格处理、契约执行、跨表查询、**`word_office/`** |
| `infra/` `storage/` `db/` | 缓存 / 落库 / 数据库 |
| `export/` | 导出 |
| `web/` | 本地 Web（测试 / 管理） |
| `scripts/` | 开发脚本 |
| `tests/` | 回归测试 |
| `contracts/` | 契约库（权威定义） |
| `fixtures/` | 测试夹具 |
| **`docs/`** | **权威文档**：架构 / 规格 / 状态 / 可优化 |

## 文档

| 文件 | 内容 |
|---|---|
| `docs/architecture.md` | 架构（A–E + op 两维度；记忆；复用原则） |
| `docs/specs/word_office_spec.md` | Word 接入设计（**权威规格**） |
| `docs/spec_status.md` | 规格落地状态（6/6 op） |
| `docs/optimizations.md` | 可优化 + 技术弊端（backlog） |

## 说明

- 本仓库**只含源码**：真实业务表、验收证据、内部设计文档**未上传**。
- **不改原文件**：Word/Excel 操作**写前备份**，失败**回滚**。
