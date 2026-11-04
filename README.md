# FileMate

本地桌面助手：用自然语言整理文件、跨多张 Excel 做复杂条件查询。

## 功能

- **文件整理**：按规则归类、重命名、归档。
- **跨表查询**：从多张表筛条件、带出关联字段，生成结果表并导出。
- **表格清洗**：识别标题行 / 合并表头 / 多级表头 / 矩阵；认不出的表先问用户，再按用户给的表头或模型恢复。
- **语义识别**：列名同义 / 包含匹配（本地优先，LLM 兜底默认关）。

## 环境

- Python 3.11+（开发用 3.14）
- Windows（PyQt6 桌面）

## 安装

```bash
pip install -r requirements.txt
```

## 配置

**推荐：启动后在「设置 → 聊天模型设置 / 记忆·向量设置」里填**，会写入本地 `config.json`（已被 `.gitignore` 忽略，不会上传）。

系统有**两个 API**，可以同一家，也可以不同家：

| 用途 | 配置项 | 说明 |
|---|---|---|
| **聊天模型**（生成/推理） | `api_key` + `params.api.base_url` + `params.api.model` | 如 DeepSeek |
| **语义识别**（embedding，**可选**） | `params.external_embedding.api_key` + `base_url` + `model` | 如 SiliconFlow `BAAI/bge-m3`（DeepSeek 没有 embedding，需另一家） |

```json
{
  "api_key": "聊天模型的 Key",
  "params": {
    "api.base_url": "https://api.deepseek.com/v1",
    "api.model": "deepseek-chat",

    "external_embedding.enabled": true,
    "external_embedding.base_url": "https://api.siliconflow.cn/v1",
    "external_embedding.model": "BAAI/bge-m3",
    "external_embedding.api_key": "语义识别的 Key"
  }
}
```

> - **语义识别可选**：不配就不走向量召回，只靠本地字符串匹配（+ LLM 兜底，默认关）。
> - 旧字段 `zhipu_api_key` 是**聊天 Key 的旧名**（项目最初用智谱），仍兼容；新代码统一写 `api_key`。

## 运行

- 双击 `启动FileMate.bat`（或 `启动FileMate.vbs`）
- 或 `python main.py`

## 测试

```bash
pytest tests/ -q
```

## 目录结构

| 目录 | 说明 |
|---|---|
| `ui/` | PyQt6 界面 |
| `engine/` | 意图分发 / 执行器 |
| `agent_core/` | 意图、任务、能力、质检 |
| `ai/` | 模型 HTTP 客户端 |
| `pipeline/` | 解析、分类、表格处理、契约执行、跨表查询 |
| `infra/` `storage/` `db/` | 缓存 / 落库 / 数据库 |
| `export/` | 导出 |
| `web/` | 本地 Web（测试 / 管理） |
| `scripts/` | 开发脚本 |
| `tests/` | 回归测试 |
| `contracts/` | 契约库（权威定义） |
| `fixtures/` | 测试夹具 |

## 说明

- 本仓库**只含源码**：真实业务表、验收证据、内部设计文档**未上传**。
- 契约是权威：算子 / 枚举以 `contracts/versions/*.json` 为准，`pipeline/synth_contract/authority_scan.py` 会扫描漂移。
