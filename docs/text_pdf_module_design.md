# 文本 / PDF 文件模块技术设计

> 日期：2026-11-19
> 状态：`office_core` 与 `text_office` 已落地；`pdf_office` 与语义编辑层、内容理解层、内容生成层为设计态
> 相关模块：`pipeline/office_core`、`pipeline/text_office`、`pipeline/pdf_office`（待建）

## 1. 背景与目标

在原有 Word 编辑业务线（`pipeline/word_office`）之外，新增两类文件功能：

1. **PDF 模块**：页级结构操作与内容读取。
2. **文本模块**：`.txt / .md / .json / .log` 等纯文本文件的确定性编辑与语义编辑。

核心问题是：**新模块应该复制 Word 的整套代码，还是从 Word 中抽取公共部分？**

结论：**抽取外层执行管线，不抽执行器基类。**

原因：

- Word 的执行层是 python-docx 的 run / XML 操作；
- PDF 的执行层是页对象操作；
- 文本的执行层是整文件字符串操作。

三者的共性不在“怎么改一个字符”，而在“怎么决定改什么、敢不敢改、改坏怎么退、改完留什么证据”。因此公共层只包含流程，不包含格式语义。

## 2. 分层模型

> 术语说明：`docs/architecture.md` 的 A–E 是**任务强度分级**（规则 / 轻 AI / 中 AI / 强 AI / 分块）。
> 本文档不使用字母编号，避免同一组字母出现两套含义。能力层统一用中文名称。

| 能力层 | 能力 | 典型操作 | 当前状态 |
|---|---|---|---|
| 适配层 | 格式适配 | 扫描、画像、读写、应用补丁、校验 | text 已做；pdf 待做 |
| 确定性层 | 确定性编辑 | 替换、删行、插行、JSON 格式化 | text 已做 |
| 语义编辑层 | 语义编辑 | 润色、校对、改写、翻译 | 未接，方案见第 7 节 |
| 内容理解层 | 内容理解 | 摘要、要点、结论、问答 | 未接，代码可复用 |
| 内容生成层 | 内容生成 | 摘要文件、报告、新文档 | 未接，渲染器可复用 |
| 批量层 | 批量处理 | 多文件统一润色/校对 | 未接，Word 已有批处理骨架 |

与 `architecture.md` 任务分级的对应关系：

| 本文章节 | architecture.md 任务级 |
|---|---|
| 确定性层 | A 规则 |
| 内容理解层 | B 轻 AI |
| 语义编辑层 | C 中 AI |
| 内容生成层 | D 强 AI |
| 批量层 | E 分块/多文件 |

映射只用于成本与模型选型参考，不代表同一套编号。

分层原则：

- 确定性层不调模型，零 token，结果确定；
- 语义编辑层、内容理解层、内容生成层可以调模型，但**写回策略不同**：语义编辑写回原文件，内容理解不写回，内容生成写新文件；
- 批量层串行执行，单文件失败不阻塞其他文件。

## 3. 已落地：office_core 公共管线

目录：`pipeline/office_core/`

| 文件 | 职责 |
|---|---|
| `result.py` | 七状态机、`fail()`、`finish()`，统一返回结构 |
| `txn.py` | 写前快照、回滚，备份目录按 tool 拆分 |
| `evidence.py` | 本地证据落盘（gzip），按 tool 分目录 |
| `session.py` | 进程内“最近一次任务”记忆 |
| `adapter.py` | `DocumentAdapter` 协议 |
| `runner.py` | 选文件 → 扫描 → 计划 → 确认 → 预检 → 快照 → 执行 → 校验 → 证据 |

### 3.1 runner 的固定流程

```text
选中文件过滤（按 adapter.exts）
  → input_mode = getattr(adapter, "input_mode", "single")
  → 点名解析：收集文件名或主名出现在指令中的 targets
  → single：命中 1 个 → 收窄；多选且写操作未点名 → needs_confirmation
  → multi_to_one：命中 ≥2 个 → 用命中子集；命中 1 个 → 追问“至少两个”；命中 0 个 → 用全部
  → multi_to_one 最终 paths < 2 → 追问“合并至少需要两个文件”
  → one_to_many：命中 1 个 → 收窄；多选 → 追问“拆页一次只能处理一个”
  → adapter.validate_early(paths)（大小/可读/加密）
  → dry-run 时对每个输入复制临时副本，work_paths = 副本
  → adapter.scan(work_paths)
  → runner 回填 state.files[i].path = 原件、work_path = work_paths[i]
  → adapter.plan(user_input, state)
  → 低置信 → needs_confirmation
  → 危险 op → needs_confirmation
  → op 白名单校验
  → writes = op not in readonly_ops
  → overwrite_set = {op for op in ops if op in overwrite_ops}
  → new_file_set = {op for op in ops if op not in readonly_ops and op not in overwrite_ops}
  → new_file_set 非空 → 检查输出目录可写（失败返回 output_dir_unwritable）
  → overwrite_set 非空才建快照
  → adapter.execute(work_paths, ops, state) → result
  → adapter.verify(work_paths, ops, state, result)
  → 失败：overwrite_set 回滚快照；result.outputs 全部删除；dry-run 清理副本
  → patch_set + evidence + session
```

签名与路径约定：

- `scan / execute` 接收 `list[Path]`；
- `verify` 接收 `list[Path]`、`ops`、`state` 和 `execute` 返回的 `result`；
- `single` 就是长度为 1 的列表，runner 负责归一化；
- `work_paths` 是 `scan / execute / verify` 的唯一输入；原始 `paths` 只用于展示、证据和最终产物命名；
- 非 dry-run 时 `work_paths = paths`；dry-run 时 `work_paths` 指向副本；
- dry-run 时原文件一个字节都不碰，所有校验都在副本上完成；
- `state.files[i]["path"]` 永远是原件路径，`state.files[i]["work_path"]` 是实际读写路径；evidence/UI 读 `path`，执行层读 `work_path`；
- 点名匹配按模式处理：`single` 恰好命中 1 个才收窄；`multi_to_one` 允许命中多个并按指令顺序合并；`one_to_many` 只允许 1 个输入；
- 是否原地覆盖只由 `op in adapter.overwrite_ops` 决定，不由 `input_mode` 决定；
- 这要求同步修改 `text_office` 与现有测试，属于阶段 0 的破坏性变更，必须先定签名再改 runner。

### 3.2 安全约束

- 原地覆盖写前必须有快照；快照失败则拒绝执行；
- 新文件操作不建快照，失败或校验不过时删除本次生成的产物；
- 执行层异常或写后校验失败：原地覆盖回滚快照，新文件删除本次产物，不把半成品留在磁盘；
- 只允许删除本次运行创建的路径，不碰用户已有文件；
- 原文件旁保留 `.bak`，项目内保留 `storage/backups/<tool>/`；
- 证据只落本地，不上传，30 天 / 1000 条自动清理。

## 4. 已落地：text_office

目录：`pipeline/text_office/`

| 文件 | 职责 |
|---|---|
| `module.py` | `TextModule.handle(request)`，唯一对外入口 |
| `adapter.py` | 编码探测、原子写、指令转 op、执行、校验 |
| `ops.py` | 纯字符串 op 实现，不碰文件系统 |

### 4.1 支持格式

```text
.txt  .md  .markdown  .json  .log  .ini  .yaml  .yml
```

不认领：

- `.csv / .tsv`：属于表格数据，走 `table_query`；
- `.docx`：走 `word_office`；
- `.pdf`：走 `pdf_office`；
- 图片与二进制文件：不支持。

`.yaml / .yml` 目前只按纯文本处理，不校验 YAML 语法。

### 4.2 操作清单

| op | 用户话术示例 | 读写 | 是否确认 |
|---|---|---|---|
| `inspect` | 看看编码、多少行、统计一下 | 只读 | 否 |
| `json_validate` | 校验 JSON 是否合法 | 只读 | 否 |
| `find_replace` | 把 A 替换成 B | 写 | 否 |
| `delete_line` | 删除包含 DEBUG 的行 | 写 | 是 |
| `insert_line` | 在第 3 行后插入一行说明 | 写 | 否 |
| `json_format` | 格式化 JSON | 写 | 否 |
| `json_minify` | 压缩 JSON | 写 | 否 |

### 4.3 编码策略

读取顺序：

1. UTF-8 BOM；
2. UTF-16 BOM；
3. UTF-8；
4. GB18030；
5. 兜底 UTF-8（替换非法字节）。

写回严格使用读取时的编码：

- `utf-8-sig` 写回会保留 BOM；
- `utf-16` 写回会保留 BOM；
- GB18030 无法表示新字符时拒绝写入，不产生半个文件。

UTF-16 不带 BOM 的情况无法可靠识别，属于已知限制。

### 4.4 JSON 安全写

- `json_validate` 只解析，不写文件；
- `json_format / json_minify` 写回前后都要求能重新 `json.loads`；
- 写后校验失败会整包回滚；
- 语法错误会返回中文行号列号，不把 Python 原始报错直接抛给用户。

已知代价：

- 格式化会重排空白与转义，原数字字面量形式不保证保留；
- 不支持带注释的 JSONC。

**待修复的数据风险：重复键**

当前实现直接 `json.loads`，Python 对重复键保留最后一个，`json_format / json_minify` 会把前一个静默丢掉。这属于数据丢失，不应作为“已知代价”接受。

修复要求：

- 在格式化 / 压缩前用 `object_pairs_hook` 检测重复键；
- 检测到重复键时拒绝执行，并返回重复键名与所在行；
- 新增对应用例，确保不会静默丢数据。

### 4.5 测试

`tests/test_text_office.py`，共 12 个用例：

- 编码识别与行数统计；
- 替换成功、替换目标不存在；
- 删除行需要确认、确认后生效；
- 插入行；
- JSON 合法/非法格式化；
- JSON 语法校验；
- GB18030 编码保持；
- 多文件歧义追问；
- 非文本文件拒绝；
- dispatcher 端到端路由。

同时回归 `tests/test_word_office.py` 与 `tests/test_word_report.py`，共 54 个用例通过。

## 5. text_office 能力边界

### 5.1 能做

- 字面量全文替换；
- 按包含关系删除整行；
- 指定行号后插入一行；
- JSON 语法校验、格式化、压缩；
- 查看编码、行数、字符数、换行符。

### 5.2 不能做

| 用户想要 | 原因 | 正确去处 |
|---|---|---|
| 润色、改写、缩写、扩写 | 当前只有确定性层 | 语义层（待接）或 `word_office` |
| 总结、提炼要点 | 属于读内容，不写文件 | `chat` / 内容分析 |
| 正则替换、忽略大小写 | 只做字面量替换 | 后续新增 op |
| 跨行块级替换 | 当前只支持单个 find/replace | 后续新增 op |
| 编码转换 | 写回保持原编码 | 后续新增 op |
| YAML 校验 | 只当纯文本 | 后续新增 op |
| 一次批量改多个文件 | 单文件事务 | 批量层 |
| 改 CSV / Excel 单元格 | 表格数据 | `table_query` |
| 改 docx 排版 | Word 执行层 | `word_office` |

### 5.3 数据边界

- **待修复**：当前未设文件大小上限，整文件读入内存；计划增加 `validate_early`，超限返回 `file_too_large`，不再整读；
- 多文件写操作且指令未点名时，一定追问，不默认取第一个；
- 没有 UI 撤销按钮，只有快照与 `.bak`，同名文件保留最近 20 份；
- `dry_run` 在内部管线已支持，但当前对话入口尚未暴露；计划在 dispatcher 透传，与 `word_office` 保持一致。

## 6. pdf_office 设计边界（未实现）

### 6.1 只读能力

- 页数、页面尺寸；
- 每页文本提取；
- 书签与大纲；
- 元数据；
- 关键字搜索；
- 导出为 txt / md。

### 6.2 页级写能力

| op | 输入 | 输出 | 覆盖语义 | 是否快照 | 是否确认 |
|---|---|---|---|---|---|
| `delete_page` | 1 个 PDF | 改写后的 PDF | 原地覆盖 | 是 | 是 |
| `rotate_page` | 1 个 PDF | 改写后的 PDF | 原地覆盖 | 是 | 否 |
| `reorder_pages` | 1 个 PDF | 改写后的 PDF | 原地覆盖 | 是 | 否 |
| `extract_pages` | 1 个 PDF | 1 个新 PDF | 新文件 | 否 | 否 |
| `split_pages` | 1 个 PDF | N 个新 PDF | 新文件 | 否 | 否 |
| `merge_pdfs` | N 个 PDF | 1 个新 PDF | 新文件 | 否 | 否 |

覆盖语义的机器载体是 `adapter.overwrite_ops`，不是 `input_mode`。`extract_pages` 虽然也是 1→1，但不在 `overwrite_ops` 中，因此不建快照、输出新文件。

确认机制：runner 只认 `adapter.dangerous_ops`，不认文档表格。`PdfAdapter` 必须显式写 `dangerous_ops = {"delete_page"}`，否则删页不会弹确认。

写策略：

- 原地覆盖：先写同目录临时文件，成功后 `os.replace`；必须走快照；
- 输出新文件：直接写目标文件，不碰源文件，不需要快照；
- 写后校验：能重新打开、页数正确；
- 输出命名：提取范围用 `<原名>_p1-p3.pdf`；按页拆分输出目录 `<原名>_split/1.pdf`、`2.pdf`；合并用 `merged_<时间戳>.pdf`，重名自动加序号；
- 输出目录：默认与第一个源文件同目录；跨盘输出必须先确认。

### 6.3 明确不做

- 修改 PDF 正文、字体、排版；
- 扫描件 OCR；
- PDF 转 Word / Excel；
- Word / 图片转 PDF；
- 主动加密、解密、签名（仅权限加密的只读尝试见 §6.4）；
- 表单填写、批注、水印；
- PDF 压缩；
- 从 PDF 表格做跨表查询。

### 6.4 关键边界与管线改造

#### 多文件输入

现有 runner 的规则是“多选 + 写操作 → 一律追问，追问后只取第一个”。这对合并是错的。

PDF 需要区分三种输入输出形态，但**覆盖语义由 op 决定，不由 `input_mode` 决定**：

| 形态 | 示例 | 输入 | 输出 | 覆盖语义 |
|---|---|---|---|---|
| `single` | 删页、旋转、重排 | 1 个 | 1 个 | 原地覆盖 |
| `single` | 提取页面 / 按范围拆出 | 1 个 | 1 个 | 新文件 |
| `multi_to_one` | 合并 | N 个 | 1 个 | 新文件 |
| `one_to_many` | 拆页 | 1 个 | N 个 | 新文件 |

`input_mode` 只表达输入/输出数量，不能用来判断是否原地覆盖。判断依据是 `op in adapter.overwrite_ops`：

```python
overwrite_ops = {"delete_page", "rotate_page", "reorder_pages"}   # PDF
# extract_pages / split_pages / merge_pdfs 不在其中，按新文件处理

overwrite_ops = {"find_replace", "delete_line", "insert_line",
                 "json_format", "json_minify", "ai_edit"}         # 文本
```

runner 的规则：

- `writes = op not in readonly_ops`；
- `overwrite_set = {op for op in ops if op in overwrite_ops}`；
- `new_file_set = {op for op in ops if op not in readonly_ops and op not in overwrite_ops}`；
- `overwrite_set` 非空才建快照；失败时只回滚 `overwrite_set`；
- `new_file_set` 非空时检查输出目录；失败时删除 `result["outputs"]` 中的全部新文件；
- 不要用全局 `any(op in overwrite_ops)` 判断；一个 plan 里可能同时有原地覆盖和新文件 op，快照与清理必须逐 op 归类、同时执行。

改造要求：

**1. 签名统一为 `list[Path]`**

```python
def validate_early(self, paths: list[Path]) -> dict | None: ...
def scan(self, paths: list[Path]) -> dict: ...
def execute(self, paths: list[Path], ops: list[dict], state: dict) -> dict: ...
def verify(self, paths: list[Path], ops: list[dict], state: dict,
           result: dict) -> dict: ...
```

- `single` 就是长度为 1 的列表，由 runner 归一化；
- `validate_early` 返回 `None` 表示通过，返回 `{"reason": ..., "message": ...}` 时 runner 直接失败，不进入 `scan`；
- `validate_early` 只做轻量检查：`stat` 大小、扩展名、`PdfReader` 头部/xref 可读性、是否加密；
- `validate_early` **不调用** `extract_text()`、不遍历页面、不统计页数；页数由 `scan` 填充，避免整份 PDF 打开两次；
- 增加 `validate_early` 的原因：现有 `scan` 只有正常返回通道，无法表达 `file_too_large`、`password_required`、`unreadable` 这类“还没开始就该拒绝”的情况；
- `verify` 必须接收 `execute` 的 `result`，否则合并/拆页拿不到输出路径；
- 不采用“把 outputs 写回 state”的方案，避免 `state` 产生隐式副作用。

这组签名会让现有 `text_office` 的 `scan / execute / verify` 和 12 个测试全部对不上，属于阶段 0 的破坏性变更。必须先改签名，再改 runner。

**2. `state` 结构**

```json
{
  "mode": "single | multi_to_one | one_to_many",
  "files": [
    {"path": "D:/in/a.pdf", "work_path": "D:/in/a.pdf",
     "size": 0, "pages": 10, "text_pages": 10, "encrypted": false}
  ]
}
```

- `single` 时 `files` 长度为 1，不再保留旧的扁平字段；
- `text_office` 内部改为读 `state["files"][0]`，测试同步修改；
- `state_detail(state)` 也必须改为读 `state["files"][0]`，不能再读 `state.get("encoding")`；
- `path` 永远是原件路径，`work_path` 是 `scan / execute / verify` 实际使用的路径；dry-run 时 `work_path` 是副本；
- `files` 的公共字段与格式字段分开定义：

| 字段 | 适用 | 说明 |
|---|---|---|
| `path` / `work_path` | 全部 | 原件路径、实际读写路径 |
| `size` | 全部 | 字节数 |
| `encoding` / `lines` / `chars` | 文本 | 编码、行数、字符数 |
| `json_valid` | 文本 JSON | 是否能解析 |
| `pages` / `text_pages` | PDF | 总页数、有文本的页数 |
| `encrypted` / `password_required` | PDF | 是否加密、是否必须密码 |
| `has_outline` / `page_sizes` | PDF | 是否有书签、页面尺寸 |

- PDF 的 `plan`（例如“删第 3 页”）只从 `state["files"][i]` 读取 `pages` 等字段，不重新打开文件；
- `execute` 返回的 `result["outputs"]` 是**文件路径列表**，不是目录：
  - `single` 原地覆盖：`[]`；
  - `single` 新文件（如提取页面）：`["D:/out/x_p1-p3.pdf"]`；
  - `multi_to_one`：`["D:/out/merged_20261119.pdf"]`；
  - `one_to_many`：`["D:/out/x_split/1.pdf", "D:/out/x_split/2.pdf"]`；
  - 需要目录时额外返回 `result["output_dir"]`，例如 `one_to_many`；
- `outputs` 必须在**最终改名成功后**按最终路径生成；临时目录或临时文件名不得进入 `outputs`；
- `verify` 按覆盖语义分流：
  - `single` 原地覆盖 → 校验 `work_paths[0]`；
  - `single` 新文件 → 校验 `result["outputs"][0]`；
  - `multi_to_one` → 校验 `result["outputs"][0]`；
  - `one_to_many` → 校验 `result["outputs"]` 中每个文件。

**3. `input_mode` 不写进 Protocol**

`DocumentAdapter` 是 `runtime_checkable`，新增必需属性会让没有该属性的适配器 `isinstance` 失败。

- 不把 `input_mode` 写进 `DocumentAdapter`；
- `TextAdapter` 显式写 `input_mode = "single"`；
- runner 一律用 `getattr(adapter, "input_mode", "single")`；
- `TextAdapter` 必须显式实现 `def validate_early(self, paths): return None`，Protocol 不提供默认实现。

**4. `multi_to_one` 合并**

- runner 跳过 `multiple_candidates` 追问，把完整 `paths` 交给适配器；
- 合并顺序默认取选中顺序；用户明确说“按 A、B、C 合并”时，runner 在 `scan` 前按指令重排 `paths`；
- **合并顺序的唯一载体是 `state["files"]` 的顺序**，不另设 `ops[].order`；适配器按 `state["files"]` 顺序合并；
- 先统一 `validate_early`：所有输入可读、不处于“需要密码”状态；
- 写同目录临时文件，先做轻量可读性检查，再改成最终新文件；
- `result["outputs"]` 在最终改名成功后生成，只记录最终路径；
- `verify` 失败时删除本次生成的最终文件；删除失败返回 `compensation_failed`，并列出残留路径；
- 任一步失败都删除临时文件，不产出半成品；
- 输出是新文件，不需要逐输入快照；
- `PdfAdapter.dangerous_ops` 必须包含 `delete_page`；runner 的危险操作确认只认 `adapter.dangerous_ops`，不认文档表格。删页即使输出到新文件，也要走确认。

**5. `one_to_many` 拆页**

- 输出是一个目录，例如 `<原名>_split/`；
- 先在输出目录旁建临时目录 `.pdf_split_<uuid>/`，把所有页写进去；
- 临时目录内先做轻量可读性检查，再整目录原子改名为最终目录；
- 最终目录已存在时，事先选好带序号的新名字，避免中途改名失败；
- `result["outputs"]` 在改名成功后按最终路径生成；临时路径不得进入 `outputs`；
- `verify` 在改名后校验 `outputs` 中每个文件；
- `verify` 失败时删除本次已改名的最终目录；删除失败返回 `compensation_failed`，并在消息里列出残留路径；
- 执行阶段任一步失败都删除临时目录，不产出半批产物。

**6. `dry_run`**

- `single`：对原文件复制临时副本，`work_paths` 指向副本，`scan / execute / verify` 都用副本；
- `multi_to_one / one_to_many`：`validate_early → scan → plan`，不执行写操作，返回 `dry_run` 结果；
- 多文件模式不能跳过 `scan`，否则 PDF 计划拿不到页数、顺序和逐文件状态；
- dry-run 结束后清理副本，`result["outputs"]` 保持为空。

**7. 新 reason 与证据**

- 新文件 op 在 `plan` 之后、`execute` 之前检查输出目录：默认取第一个输入的父目录，或 `plan` 指定的 `output_dir`；不存在或不可写返回 `output_dir_unwritable`，不要等到执行层才失败；
- `validate_early` 新产生的 `file_too_large`、`password_required`、`unreadable` 必须显式登记到 `office_core/result.py` 的 `_REASON_STATUS`，避免落到默认 `failed`；
- 建议映射：三者都映射为 `failed`；`output_dir_unwritable` 映射为 `failed`；`outputs_missing` 映射为 `failed_postcheck`；`new_file_cleanup_failed` 映射为 `compensation_failed`；
- `result["outputs"]` 与 `output_dir` 要写入 evidence，并透传给 UI；UI 按文件列表展示，不把目录当文件；
- evidence/UI 使用 `state.files[i]["path"]`（原件），执行层使用 `work_path`（副本）；
- `patch_set` 对新文件操作记录 `outputs`，对原地覆盖记录备份路径；
- `execute` 内部异常时，适配器必须自行清理未完成的临时文件和新文件；runner 只能清理 `result["outputs"]`，拿不到适配器私有临时路径。

这是对 `office_core/runner.py` 与 `text_office` 的结构性改动，必须在 PDF 页级写开工前完成；只写文档、不改 runner，合并与拆页都无法落地。

#### 其他边界

- 需要密码打开的 PDF 直接拒绝；仅权限加密的 PDF 可尝试空密码解密后只读；
- 判断空密码是否解开时显式比较 `PasswordType.NOT_DECRYPTED`，不要依赖 `decrypt("") != 0` 的整数比较；
- `text_pages == 0` 时提示“疑似扫描件或没有文本层”，不断言一定是扫描件（页眉页码也会被算作文本）；
- pypdf 整份载入内存，建议设页数与体积上限。

### 6.5 路由判断

- “合并这几个 PDF”“拆成每页一个”“删掉第 3 页” → `pdf_office`；
- “这个 PDF 讲了什么” → 内容读取 / `chat`；
- “PDF 转 Word” → 不支持，直接说明；
- “扫描件识别文字” → 不支持 OCR。

### 6.6 文字提取策略：直接解析，不经过 Word

结论：**PDF 提取文字直接用 pypdf 解析，不路由给 `word_office`，也不先转成 Word。**

#### 三种做法对比

| 方案 | 依赖 | 速度 | 扫描件 | 版面 | 结论 |
|---|---|---|---|---|---|
| 直接 pypdf 解析 | 已有 `pypdf` | 快 | 无文本层 | 纯文本，不保版面 | **默认方案** |
| 路由给 `word_office` | 无 | — | — | — | 不可行，只收 `.docx` |
| PDF → Word → 解析 | Word/COM 或 pdf2docx | 慢 | 仍无文字 | 部分保留 | 只在明确要求转换时用 |

#### 为什么不能路由给 word_office

- `word_office` 只接收 `.docx`，选 PDF 会直接返回“请选中 .docx”；
- 意图提示词也把 `word_office` 限定为 Word 文档；
- 强行路由只会得到一次无效失败。

#### 为什么不默认先转 Word

- 依赖本机安装 Word 和 COM，无法稳定无界面运行；
- 转换慢，可能弹窗；
- 扫描件转完仍然只是图片；
- 版面还原不保证，表格和分栏容易错；
- 只为读文字引入重量级依赖，不划算。

#### 项目内已有实现

`pipeline/stage1_parser/file_parser.py` 的 `_pdf_to_text` 已经用 pypdf 提取全文，内容整理链路一直在用；`requirements.txt` 也已有 `pypdf>=4.0`。没有理由另起一条 Word 转换链路。

#### 推荐：按页返回的共享提取器

建议新增 `pipeline/pdf_office/extract.py`：

```python
from pypdf import PasswordType, PdfReader


def extract_pages(path) -> dict:
    try:
        reader = PdfReader(str(path))
    except Exception:
        # 损坏、非 PDF、构造期异常都在这里收敛
        return {"ok": False, "reason": "unreadable", "pages": []}

    if reader.is_encrypted:
        # 区分“需要密码才能打开”和“仅权限加密”
        try:
            result = reader.decrypt("")
        except Exception:
            result = PasswordType.NOT_DECRYPTED
        if result == PasswordType.NOT_DECRYPTED:
            return {"ok": False, "reason": "password_required", "pages": []}

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return {
        "ok": True,
        "reason": "",
        "pages": pages,
        "text": "\n\n".join(pages),
        "text_pages": sum(1 for p in pages if p.strip()),
    }
```

按页返回而不是直接拼成一整段，原因：

- “看看第 3 页写了什么”需要页码定位；
- 删页、拆页、合并需要页对象，不只是全文；
- 摘要可以只取有文字的页；
- `text_pages == 0` 可以提示“疑似扫描件或没有文本层”，但不能断言一定是扫描件。

后续可以让 `file_parser._pdf_to_text` 也调用这个共享提取器，避免两套 PDF 解析行为漂移；这属于重构，不作为本次必做项。

#### 提取能力的边界

pypdf 提取的是“页面文本层”，不是版面还原。以下情况效果有限：

- 扫描件：没有文本层，提取为空，需要 OCR；
- 多栏排版：阅读顺序可能错乱；
- 表格：会被拉平成文本，结构丢失；
- 特殊字体：缺少 `ToUnicode` 映射时可能乱码；
- 加密 PDF：需要密码才能打开的拒绝；仅权限加密的尝试空密码后只读；
- 页眉页脚：会混进正文。

因此产品承诺只能是“可以提取文字”，不能承诺“格式、表格、版面完全还原”。

#### 什么时候才走 Word 转换

只有用户明确要求“把这个 PDF 转成 Word 文档”，且本机安装了 Word 时，才做一个独立的转换 op：

- 产物是新 `.docx`；
- 属于格式转换，不是文字提取；
- 转换结果必须让用户检查；
- 扫描件转换后仍然没有可编辑文字；
- 不能当作 PDF 摘要、PDF 搜索的底层实现。

一句话：**Word 的框架可以复用，Word 的解析不能拿来读 PDF。**

## 7. 语义层（理解 / 生成 / 改写）实现方案

### 7.1 先纠正一个说法

语义能力不是“项目里没有”，而是“没有接进 text_office”。Word 模块和内容处理链里已经有大量现成实现。

### 7.2 可直接复用的现有代码

| 能力 | 现有位置 | 复用方式 |
|---|---|---|
| AI JSON 调用 | `ai/client.py` 的 `chat_json` | 直接调用 |
| 输入预算 | `ai/context_budget.py` | 直接调用 |
| AI 出补丁 | `pipeline/word_office/ai.py` | 抽 prompt 模板与返回规整 |
| 补丁应用与对账 | `pipeline/word_office/module.py` 的 `_exec_l1` | 抽流程，应用层按格式重写 |
| 文件画像 | `word_office/module.py` 的 `_portrait` | 抽接口，各格式自己实现 |
| 文件级记忆 | `word_office/memory.py` 的 `save_file_memory / get_file_memory / recall` | 搬为通用记忆 |
| 编辑报告 | `word_office/report.py` | 先复制到 `office_core/report.py` 并参数化只读 op；Word 迁移后再切换，迁移前两边并存 |
| 多文件批处理 | `word_office/batch.py` | 改成接收任意模块 |
| 内容理解 | `pipeline/stage3_processor/ai_caller.py` 的 `analyze_document` | 直接复用，输入纯文本 |
| 报告渲染 | `export/word_renderer.py`、`export/docx_report.py` | 直接复用 |

其中 `analyze_document` 已经实现“摘要 / 要点 / 结论 / 建议”，无 Key 时还会降级为本地截断摘要。理解类需求可以直接接它。

### 7.3 必须各写一份的部分

| 部分 | text | pdf |
|---|---|---|
| 计划规则 | 替换、行操作、JSON | 合并、拆页、删页 |
| 扫描 | 编码、行数、JSON 状态 | 页数、文本、书签 |
| 画像 | 前 N 行、编码、结构 | 每页文本片段、目录 |
| 补丁应用 | `str.replace` | **没有正文写回能力** |
| 写后校验 | 回读、编码、JSON 可解析 | 页数正确、能重新打开 |
| 分块 | 按行切分并加重叠 | 按页切分 |
| 危险操作 | 删行 | 删页（`delete_page`） |
| 覆盖操作 | 全部写 op | 删页、旋转、重排 |

关键差异：

- 文本可以复用语义编辑层“改写”，因为 apply 就是字符串替换；
- PDF 不能复用语义编辑层“改写”，因为 pypdf 不支持把文字写回页面；
- PDF 只能复用内容理解层、内容生成层和页级结构操作。

### 7.4 推荐新增的公共件

```text
office_core/
  semantic.py     # AI 出补丁：prompt + 返回规整 + 补丁对账
  report.py       # 先复制到 office_core/report.py，Word 迁移后切换
  batch.py        # 多文件串行批处理
```

**不要新造一套平行适配器协议。** 阶段 0 完成后的 `DocumentAdapter` 为：

```text
tool / exts / readonly_ops / dangerous_ops / overwrite_ops / allowed_ops / no_target_message
validate_early(paths) / scan(paths) / state_detail(state) / plan(user_input, state)
is_low_confidence / execute(paths, ops, state) / verify(paths, ops, state, result)
```

`overwrite_ops` 与 `dangerous_ops` 同款，都是字面量集合，表示哪些写 op 需要原地覆盖和快照。`input_mode` 不写进 Protocol，由 `getattr(adapter, "input_mode", "single")` 读取。

语义能力作为可选扩展，单独声明子协议：

```python
class SemanticAdapter(DocumentAdapter, Protocol):
    def build_portrait(self, state) -> str: ...
    # 语义改写只支持 single；paths 长度为 1
    def apply_patches(self, path, pairs, state) -> int: ...
    def verify_patches(self, path, pairs, state) -> dict: ...
```

- `TextAdapter` 实现 `SemanticAdapter`，并显式设置 `input_mode = "single"`；
- `PdfAdapter` 实现 `DocumentAdapter`，并额外提供 `build_portrait`；不实现 `apply_patches`；
- `run_patch_edit` 只接受 `SemanticAdapter`，且只在 `input_mode == "single"` 时调用，因此 PDF 与多文件不会被误接入正文改写；
- `execute / verify` 仍是 runner 的唯一调用入口，语义方法只作为内部实现细节。

公共语义流程：

```python
def run_patch_edit(adapter: SemanticAdapter, path, state, user_input, api_key):
    data = call_patch_ai(adapter.tool, user_input,
                         adapter.build_portrait(state), api_key)
    pairs = sanitize_pairs(data)
    if not pairs:
        return suggest_result(data.get("summary"))
    applied = adapter.apply_patches(path, pairs, state)
    verify = adapter.verify_patches(path, pairs, state)
    if not verify["ok"]:
        return fail("verify_failed", verify["reason"])
    return done_result(applied, data.get("summary"))
```

### 7.5 写回策略

| 类型 | 策略 |
|---|---|
| 润色 / 校对 / 改写 / 翻译 | 模型返回 find/replace 对，本地应用并逐条对账 |
| 摘要 / 要点 / 结论 | 只读，结果进聊天；用户明确要求时才写新文件 |
| 生成周报 / 报告 | 写新文件，文件名由系统生成，不覆盖原文 |
| JSON 语义修改 | 模型返回“路径 + 新值”，本地按路径 set，不整文件重写 |

### 7.6 为什么不让模型直接重写全文

- 长文件会被 token 上限截断；
- 模型可能补出原文没有的内容；
- 换行、缩进、编码、JSON 结构可能被破坏；
- 无法逐条审计“哪一句为什么改”；
- 全文覆盖后回滚代价高。

因此语义编辑层坚持：**模型只出局部建议，本地负责应用、计数、校验和回滚。**

### 7.7 文本与 Word 的读写 / 理解同构性

结论：**编排层同构，底层不同。**

文本的“理解”和“正文改写”确实和 Word 走同一条路，但“同一条路”只成立在流程层；读取、应用、校验必须由各自适配器实现。

#### 逐环节对照

| 环节 | Word | 文本 | 是否同构 |
|---|---|---|---|
| 读取 | python-docx 读段落、表格、样式 | 字节解码为字符串 | 否，adapter 实现 |
| 文件画像 | 段落数、标题、样式、表格数 | 行数、编码、前 N 行、JSON 状态 | 否，adapter 实现 |
| 内容理解 | `analyze_document(正文)` | 同一函数，输入纯文本 | **是，直接共用** |
| 改写计划 | 模型返回 find/replace 对 | 同一协议 | **是，共用 `semantic.py`** |
| 应用补丁 | 定位 run / 段落 / 表格单元格 | `str.replace` | 否，adapter 实现 |
| 写后校验 | 段落数、样式、替换次数、可重新打开 | 回读文本、编码、JSON 可解析、替换次数 | 否，adapter 实现 |
| 快照与回滚 | `txn.py` | 同一套 | **是，直接共用** |
| 证据与报告 | `evidence.py` / `report.py` | 同一套 | **是，直接共用** |

#### 统一适配器接口

阶段 0 后的 `DocumentAdapter` 为：

```python
class DocumentAdapter(Protocol):
    # tool / exts / readonly_ops / dangerous_ops / overwrite_ops / allowed_ops / no_target_message
    # validate_early(paths) / scan(paths) / state_detail(state) / plan(user_input, state)
    # is_low_confidence / execute(paths, ops, state) / verify(paths, ops, state, result)
    ...

class SemanticAdapter(DocumentAdapter, Protocol):
    def build_portrait(self, state) -> str: ...
    def apply_patches(self, path, pairs, state) -> int: ...
    def verify_patches(self, path, pairs, state) -> dict: ...
```

三个语义入口：

```text
理解：adapter 读取正文 → analyze_document() → 结果进聊天（不写回）
改写：build_portrait → call_patch_ai → apply_patches → verify_patches
生成：adapter 读取正文 → 生成内容 → 写新文件（不覆盖原文）
```

#### 文本的读写特殊性

| 环节 | 文本模块要额外处理 |
|---|---|
| 读取 | BOM、UTF-8、GB18030 识别；UTF-16 无 BOM 不可靠 |
| 写入 | 保持原编码；GB18030 无法表示时拒绝；临时文件 + 原子替换 |
| JSON | 写前写后都要能 `json.loads`；格式化会重排空白与转义 |
| 校验 | 替换次数必须对账；JSON 必须仍可解析 |

Word 的特殊性在另一侧：

- 读取要处理 run 拆分、段落样式、表格单元格；
- 应用要处理跨 run 命中与段落合并；
- 校验要看段落数、样式数、表格数是否被意外改变。

#### 边界提醒

“同一条路”不等于“同一个 apply 函数”。如果强行让三种格式共用一个应用实现，就会退化成按扩展名分支的巨型函数，这正是要避免的过度抽象。

唯一真正的不对称在 PDF：

- PDF 有“理解”和“生成”，没有“正文改写”；
- 因此 `PdfAdapter` 只实现 `DocumentAdapter` 的现有成员与 `build_portrait`，不实现 `apply_patches`。

## 8. 路由与接入

| 用户指令 | 正确工具 | 判断依据 |
|---|---|---|
| 改 txt / md / json 的字符 | `text_office` | 目标是文件内容 |
| 改 docx 的文字或格式 | `word_office` | 目标是 Word 对象 |
| 查 CSV / Excel 数据 | `table_query` | 目标是表格数据 |
| 移动、归档文件 | `file_organize` | 不读不改内容 |
| 读内容并总结 | `chat` / 内容分析 | 不写文件 |
| 合并、拆页、删页 | `pdf_office` | 目标是页面结构 |

已修改的接入点：

| 位置 | 改动 |
|---|---|
| `agent_core/intent_router.py` | `VALID_TOOLS` 增加 `text_office`；提示词补强信号与反例 |
| `engine/dispatcher.py` | 新增 `text_office` 分支；Word/Text 共用 `_run_office_module` |
| `engine/dispatcher.py` | 选中非 Word 文件时，本地 Word 只读规则不再抢指令 |
| `ui/main_window.py` | 确认弹窗与失败分支改为 `word_office / text_office` 集合 |

## 9. 契约与版本

当前 `contracts/CURRENT` 仍为 `v1.9.0`，本轮**没有**新增契约段。

### 9.1 当前状态的真实风险

- `text_office` 的 `allowed_ops` 写在 `pipeline/text_office/adapter.py`，契约里没有对应段；
- `word_office` 的 `gate.pre_check` 读的是 `word_office.ops_whitelist`，不会校验文本模块的 op；
- `authority_scan` 只扫描契约里的禁用词，不校验“模块白名单是否来自契约”。

因此当前“契约单一权威”对新模块**不成立**。`authority_scan` 通过只能说明没有命中禁用词，不能说明 op 白名单受契约约束。

### 9.2 开工前必须完成

1. 新建 `contracts/versions/v1.10.0.json`，至少包含：
   - `text_office.ops_whitelist`
   - `text_office.intent_rules`
   - `text_office.budget`
   - `pdf_office.ops_whitelist`
   - `pdf_office.intent_rules`
   - `pdf_office.budget`
   - 两个模块的 `report` 配置
2. 更新 `contracts/CURRENT`；
3. 把 `gate.pre_check` 参数化为按 `tool` 读取契约段，并校验 `op in contract[tool].ops_whitelist`；
4. `authority_scan` 增加一致性检查，但必须写明前提：
   - `allowed_ops` 必须是**字面量** `frozenset({...})`，静态 AST 才能读到；
   - 如果 `allowed_ops` 是动态拼出来的，AST 读不到，子集校验会形同虚设；
   - 动态构造的适配器，把“是否属于契约白名单”的校验放到运行时 `gate.pre_check`，不要依赖 AST。
5. 同步修改断言 CURRENT 的测试：
   - `tests/contract/test_query_engine_v190.py`
   - `tests/contract/test_routing_batch5.py`
6. 检查是否有其他硬编码版本号的测试。

在完成上述步骤前，`text_office` 的状态应标注为“临时白名单，未完成契约接入”，不能标注为“已受契约保护”。

## 10. 已知限制与风险

1. **WordModule 尚未接入 `office_core`**：当前是“复制公共件、Word 保持不动”，两份状态机、快照、证据存在漂移风险。
   - 迁移前至少增加状态映射一致性测试：同一 `reason` 在 `word_office` 与 `office_core.result` 下必须得到同一 `status`；
   - 排一个 Word 迁移批次，迁移完成前不要继续扩大两边差异。
2. **语义层未接**：润色、改写、总结、生成目前都不可用。
3. **PDF 完全未实现**：只有设计边界。
4. **文本模块无大小上限**：超大文件会整读入内存；计划增加 `validate_early`，超限返回 `file_too_large`。
5. **UTF-16 无 BOM 不可识别**：会按 GB18030 或 UTF-8 兜底。
6. **JSON 重复键会丢失**：当前格式化会静默保留后一个，属于待修复的数据风险。
7. **多文件只处理一个**：现有 runner 在执行阶段只取 `targets[0]`；改为 `list[Path]` 签名会破坏 `text_office` 现有接口与 12 个测试，必须先改签名再改 runner。
8. **无 UI 撤销**：只有快照与 `.bak`。
9. **契约版本未推进**：新增格式尚未进入契约白名单，`authority_scan` 的通过不代表白名单受契约约束。
10. **适配器协议存在两套写法风险**：现有 `DocumentAdapter` 与文档早期草案的 `Adapter` 成员不重叠；实现时必须以 `DocumentAdapter` 为准，语义方法只作为可选子协议。
11. **`dry_run` 入口未暴露**：内部管线支持，但 dispatcher 未透传。
12. **`authority_scan` 的 AST 前提**：只能校验字面量 `allowed_ops`；动态构造时必须改用运行时 `gate.pre_check`。
13. **`one_to_many` 半批产物**：拆页必须先写临时目录，全部成功后再整目录改名；代码尚未实现。
14. **项目 `.venv` 配置指向不存在的基座路径**：本机测试使用系统 Python 3.14 执行。

## 11. 后续路线

| 阶段 | 内容 | 依赖 |
|---|---|---|
| 0 | 阻断项：契约 `v1.10.0`；协议改为 `list[Path]` + `verify(result)` + `state.files` + `state_detail` + `validate_early` + `dangerous_ops` + `overwrite_ops` + `path/work_path` + `outputs` 形状；点名分模式；`one_to_many` 多选追问；输出目录可写检查；`work_paths` 与产物清理；`state.files` 的 PDF 字段；合并顺序载体；新 reason 映射；`one_to_many` 临时目录；JSON 重复键检测；文本大小上限；同步改 `text_office` 与测试 | 无 |
| 1 | 抽 `office_core/semantic.py`、`report.py`、`batch.py` | 阶段 0 |
| 2 | text_office 接 `ai_edit`：润色、校对 | 阶段 1 |
| 3 | text_office 接改写、翻译 | 阶段 2 |
| 4 | text_office 接 `analyze_document`：摘要、要点 | 无 |
| 5 | 生成新文件：摘要文件、报告 | 报告渲染器 |
| 6 | JSON 路径级语义修改 | 路径 setter |
| 7 | 长文本分块与术语一致 | 阶段 2、分块器 |
| 8 | pdf_office 共享提取器：`extract.py` 按页返回文本 | 无 |
| 9 | pdf_office 只读：页数、文本、书签 | 阶段 8 |
| 10 | pdf_office 页级写：删页、拆分、合并 | 阶段 0、阶段 9 |
| 11 | PDF 文字提取接入内容分析：摘要、要点 | 阶段 8、`analyze_document` |
| 12 | 批量层：多文件统一润色/校对 | 阶段 2、批处理骨架 |

## 12. 关键文件索引

| 模块 | 路径 |
|---|---|
| 公共管线 | `pipeline/office_core/runner.py` |
| 公共结果 | `pipeline/office_core/result.py` |
| 公共快照 | `pipeline/office_core/txn.py` |
| 公共证据 | `pipeline/office_core/evidence.py` |
| 适配器协议 | `pipeline/office_core/adapter.py` |
| 文本入口 | `pipeline/text_office/module.py` |
| 文本适配器 | `pipeline/text_office/adapter.py` |
| 文本 op | `pipeline/text_office/ops.py` |
| 文本测试 | `tests/test_text_office.py` |
| PDF 提取器（待建） | `pipeline/pdf_office/extract.py` |
| PDF 适配器（待建） | `pipeline/pdf_office/adapter.py` |
| Word 语义参考 | `pipeline/word_office/ai.py`、`pipeline/word_office/module.py` |
| 内容分析参考 | `pipeline/stage3_processor/ai_caller.py` |
| 路由 | `agent_core/intent_router.py` |
| 调度 | `engine/dispatcher.py` |
| UI 确认 | `ui/main_window.py` |

## 13. 评审问题闭环

状态说明：**文档已闭环**表示设计层已修正；**代码待做**表示仍需改代码。

| 编号 | 问题 | 级别 | 处理 | 状态 |
|---|---|---|---|---|
| 1 | A–E 与 `architecture.md` 任务级冲突 | 阻断 | 本文档改用中文能力层名称，删除 L0 编号，补与任务级的映射表；`architecture.md` 增加术语边界说明 | 文档已闭环 |
| 2 | runner 多选写操作只取第一个，做不出合并 | 阻断 | 定义 `input_mode`：`single / multi_to_one / one_to_many`，明确 runner 改造要求 | 文档已闭环，代码待做 |
| 3 | 现有 `DocumentAdapter` 与草案 `Adapter` 两套协议 | 阻断 | 统一以 `DocumentAdapter` 为准，语义能力用 `SemanticAdapter` 子协议扩展 | 文档已闭环 |
| 4 | 契约未覆盖 text_office，`authority_scan` 抓不到 | 高 | 明确 `v1.10.0` 必须包含两个模块白名单，并要求 `gate.pre_check` 与 `authority_scan` 校验归属 | 文档已闭环，代码待做 |
| 5 | Word 未接 `office_core`，双份状态机漂移 | 高 | 增加状态映射一致性测试要求与迁移批次；修正 `result.py` 的“共用”表述 | 文档已闭环，代码待做 |
| 6 | `PdfReader` 构造期异常未捕获 | 建议 | 提取器增加 `try/except`，返回 `unreadable` | 文档已闭环 |
| 7 | `is_encrypted` 未区分密码与权限加密 | 建议 | 先尝试空密码解密；失败才返回 `password_required` | 文档已闭环 |
| 8 | `text_pages == 0` 断言为扫描件 | 建议 | 改为“疑似扫描件或没有文本层” | 文档已闭环 |
| 9 | JSON 格式化静默丢重复键 | 建议 | 明确列为数据风险，要求写前检测并拒绝 | 文档已闭环，代码待做 |
| 10 | 文本无大小上限 | 建议 | 要求 `validate_early` 设硬上限并返回 `file_too_large` | 文档已闭环，代码待做 |
| 11 | `dry_run` 入口未暴露 | 建议 | 要求 dispatcher 透传，与 `word_office` 一致 | 文档已闭环，代码待做 |
| 12 | `report.py` 复用措辞不一致 | 建议 | 统一为“先复制到 `office_core/report.py`，Word 迁移后再切换” | 文档已闭环 |
| 13 | 未区分原地覆盖与输出新文件 | 建议 | 补充写策略表、快照差异、输出命名与目录规则 | 文档已闭环 |

### 13.1 第二轮复审（2026-11-19）

| 编号 | 问题 | 级别 | 处理 | 状态 |
|---|---|---|---|---|
| 14 | `input_mode` 与 `scan/execute` 单 path 签名冲突 | 阻断 | 统一改为 `list[Path]`；定义 `state.files` 结构；明确同步改 `text_office` 与 12 个测试 | 文档已闭环，代码待做 |
| 15 | `scan` 没有失败通道 | 阻断 | 新增 `validate_early(paths) -> dict 或 None`，在 `scan` 前拒绝超限/加密/不可读 | 文档已闭环，代码待做 |
| 16 | `one_to_many` 中途失败留半批产物 | 阻断 | 先写 `.pdf_split_<uuid>/` 临时目录，全部校验后整目录原子改名；失败删除临时目录 | 文档已闭环，代码待做 |
| 17 | `authority_scan` 子集校验前提缺失 | 阻断 | 写明 `allowed_ops` 必须是字面量 `frozenset`；动态构造改用运行时 `gate.pre_check` | 文档已闭环，代码待做 |
| 18 | 文档顶部仍写“语义层（B/C/D）” | 小修 | 改为“语义编辑层、内容理解层、内容生成层” | 文档已闭环 |
| 19 | §7.4 注释仍写“从 word_office/report.py 搬” | 小修 | 改为“先复制到 office_core/report.py，Word 迁移后切换” | 文档已闭环 |
| 20 | §3.1 流程图未标 `multi_to_one` 例外 | 小修 | 流程图加入 `input_mode` 判断与 `validate_early` | 文档已闭环 |
| 21 | `decrypt("") != 0` 依赖整数比较 | 加固 | 显式比较 `PasswordType.NOT_DECRYPTED` | 文档已闭环 |
| 22 | `input_mode` 放进 Protocol 会破坏 `runtime_checkable` | 加固 | 不写进 Protocol；`TextAdapter` 显式默认值；runner 用 `getattr` 兜底 | 文档已闭环 |

### 13.2 第三轮复审（2026-11-19）

| 编号 | 问题 | 级别 | 处理 | 状态 |
|---|---|---|---|---|
| 23 | `verify` 拿不到输出路径 | 阻断 | `verify` 增加 `result` 参数：`verify(paths, ops, state, result)`；从 `result["outputs"]` 读取输出 | 文档已闭环，代码待做 |
| 24 | `state_detail` 仍读扁平 state | 小缺口 | 明确 `state_detail(state)` 改为读 `state["files"][0]` | 文档已闭环，代码待做 |
| 25 | `TextAdapter` 缺 `validate_early` 实现 | 小缺口 | 明确必须显式实现 `def validate_early(self, paths): return None` | 文档已闭环，代码待做 |
| 26 | `dry_run` 对 `multi_to_one` 漏了 `scan` | 小缺口 | 改为 `validate_early → scan → plan`，不执行写 | 文档已闭环，代码待做 |
| 27 | `PdfAdapter.dangerous_ops` 未落到机制 | 小缺口 | 明确 `dangerous_ops = {"delete_page"}`，runner 只认这个集合 | 文档已闭环，代码待做 |
| 28 | `result["outputs"]` 形状未定义 | 小缺口 | 定义为文件路径列表；`single` 为空，`multi_to_one` 一个，`one_to_many` 多个；目录放 `output_dir` | 文档已闭环，代码待做 |
| 29 | “点名”匹配规则未写 | 建议 | 补充：文件名或主名出现在指令中，恰好命中 1 个才收窄 | 文档已闭环 |

### 13.3 第四轮复审（2026-11-19）

| 编号 | 问题 | 级别 | 处理 | 状态 |
|---|---|---|---|---|
| 30 | `outputs` 与整目录改名的时序未写 | 中 | 明确 `outputs` 只在最终改名成功后按最终路径生成，临时路径不得进入 | 文档已闭环，代码待做 |
| 31 | `verify` 失败后是否删最终产物未写 | 中 | 新增：失败删除本次已改名产物；删除失败返回 `compensation_failed` | 文档已闭环，代码待做 |
| 32 | `state.files` 缺 PDF 字段 | 中 | 增加公共/文本/PDF 字段表：`pages / text_pages / encrypted / has_outline` 等 | 文档已闭环，代码待做 |
| 33 | dry_run 的 paths 指向未定 | 中 | 引入 `work_paths`：`single` 指向副本，`scan/execute/verify` 全用副本 | 文档已闭环，代码待做 |
| 34 | 合并顺序的载体未定 | 中 | 明确唯一载体是 `state["files"]` 顺序，不另设 `ops[].order` | 文档已闭环，代码待做 |
| 35 | `validate_early` 与 `scan` 重复打开 PDF | 低 | `validate_early` 只做 stat + 头部/xref + 加密判断，不提取文本、不统计页数 | 文档已闭环 |
| 36 | 新 reason 未登记 `_REASON_STATUS` | 低 | 显式登记 `file_too_large / password_required / unreadable` 等映射 | 文档已闭环，代码待做 |
| 37 | `outputs` 与 `output_dir` 未接证据/UI | 低 | 明确写入 evidence 并透传 UI，UI 按文件列表展示 | 文档已闭环，代码待做 |

### 13.4 第五轮复审（2026-11-19）

| 编号 | 问题 | 级别 | 处理 | 状态 |
|---|---|---|---|---|
| 38 | `input_mode` 决定不了是否原地覆盖 | 偏高 | 新增 `adapter.overwrite_ops`；是否快照只由 `op in overwrite_ops` 决定；`extract_pages` 显式标为新文件 | 文档已闭环，代码待做 |
| 39 | `one_to_many` 多选未定义 | 中 | 多选时追问“拆页一次只能处理一个”；命中 1 个才收窄 | 文档已闭环，代码待做 |
| 40 | 合并“点名子集”未处理 | 中 | `multi_to_one` 命中 ≥2 个时用命中子集并按指令顺序；命中 1 个追问；命中 0 个用全部 | 文档已闭环，代码待做 |
| 41 | 输出目录可写未提前检查 | 低 | `plan` 后、`execute` 前检查输出目录，失败返回 `output_dir_unwritable` | 文档已闭环，代码待做 |
| 42 | dry_run 时 state 显示副本路径 | 低 | 引入 `path`（原件）与 `work_path`（副本）；evidence/UI 读 `path`，执行层读 `work_path` | 文档已闭环，代码待做 |

### 13.5 第六轮复审（2026-11-19）

| 编号 | 问题 | 级别 | 处理 | 状态 |
|---|---|---|---|---|
| 43 | `single` 新文件的 `outputs` / `verify` 没跟上 | 中偏高 | `single` 新文件 `outputs = [输出]`；`verify` 按 `overwrites` 分流：原地校验原文件，新文件校验 `outputs[0]` | 文档已闭环，代码待做 |
| 44 | 合并最终输入不足 2 个 | 中 | `multi_to_one` 在点名与兜底之后仍 `len(paths) < 2` → 追问“合并至少需要两个文件” | 文档已闭环，代码待做 |
| 45 | 混合 op 用全局 `overwrites` 布尔 | 低 | 改为 `overwrite_set` / `new_file_set` 逐 op 归类；快照与产物清理同时执行 | 文档已闭环，代码待做 |
