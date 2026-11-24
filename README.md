# 公文材料 Agent

从自然语言和附件起草中文公文、按意见修改 Word，或把已定稿的通知正文套进自备样张。输出可编辑的 `.docx`，供经办人员核对、修改和走正式审签流程。

## 能做什么

| 场景 | 输入 | 交付 |
| --- | --- | --- |
| 单人起草或编审协作 | 事项、受众、已定事实和参考材料 | 通知、工作方案、经验材料、简报、讲话稿、请示、纪要或征求意见稿。方案可带可编辑分工表 |
| 核稿与续改 | Word 原稿或上一轮交付目录、逐条意见、需保留的内容 | 有明确范围的修订稿，或供审阅的意见表。支持读取 Word 批注和接受明确授权的正文文字修订 |
| 通知套版 | 一份可用的 DOCX 样张，以及已核定的标题、正文、发文字号和落款等字段 | 套版前后两份 Word，便于核对正文与版式 |

起草会区分用户要求、事实材料和参考写法，把缺少依据的事项留待确认。续改按本轮授权和父版定位改动，出件前检查对应的正文与表格。

## 在本机启动

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.13。依赖已通过 `uv.lock` 固定。

```bash
git clone https://github.com/Metroids048/gongwen-materials-agent.git
cd gongwen-materials-agent
uv sync --locked
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8765
```

在浏览器访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。任务登记、Word 原生审阅和通知套版可以在本机处理。网页自然起草和根据外部意见改稿需要已登录的 Codex CLI。确认终端能运行 `codex`，然后以账号可用的模型启动，例如：

```bash
GONGWEN_PUBLIC_CODEX_MODEL=gpt-6-astra \
  uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8765
```

模型起草会把本轮要求和读取的材料文本交给所选模型。请使用公开材料或已获授权的材料。任务数据库与中间文件保存在本机 `data/`。

## CLI 示例

这份虚构的演练通知可以直接试跑。模型参数可替换为自己的可用模型，`--output` 应指向一个新目录。

```bash
uv run --locked python -m app.agent \
  '请起草一份设备点检演练通知，给我 Word 审阅稿。星桥训练中心定于2026年12月8日14:00在星湖馆开展演练，设备值班员和现场协调员参训，携带工牌。落款为星桥训练中心，成文日期留空。' \
  --codex-model gpt-6-astra \
  --output outputs/notice-draft
```

用 `--file` 加入材料，可以重复传入多份文件。CLI 可读取 UTF‑8 TXT、Markdown、CSV、DOCX、XLSX 和带文字层的 PDF。扫描件需要先做文字识别。已生成的草稿可用 `--continue-from outputs/notice-draft` 继续定点修改。用 `--expert-team` 启用素材预审、主笔和独立核稿。

## 源码与测试

- `app/agent.py` 处理自然收件、起草、核稿和续改。`app/draft_layout.py`、`app/revision/` 和 `app/delivery.py` 负责版式、修改和交付。
- `app/main.py`、`web/` 和 `app/runtime/store.py` 提供本地网页、HTTP 接口及任务与版本存储。
- `contracts/`、`prompts/` 和 `skills/` 定义结构化交换、文种规则及核稿要求。背景设计见[开工规格](docs/开工规格.md)。
- `deploy/openagent/20261112/overlay/` 保留 OpenAgent 的源码适配层，包括调用入口、托管模型适配和 OpenClaw 工具。宿主部署需要自己的环境配置和授权。默认本地启动使用根目录版本。固定公开样本的日期改稿试跑另需原始材料及本地校验清单 `data/s0-s1/nanyang-2024-24/SHA256SUMS`，常规起草与 Word 原生审阅可独立使用。

```bash
uv run --locked python -m unittest discover -s tests
# 启动本地 Web 服务后，在另一个终端执行
uv run --locked python tests/http_smoke.py http://127.0.0.1:8765
```

日期、责任归属和审批状态仍需按原始依据及本单位要求人工核对。

本项目以 [MIT](LICENSE) 许可发布。审阅稿文种组织参考了 [gongwen-skill 2.15.0](https://pypi.org/project/gongwen-skill/2.15.0/)，文种规则内保留了来源说明。
