# 专利提案生成器 - Streamlit Web应用

这是一个基于 Streamlit 的 Web 应用，用于生成专利提案。支持两种输入方式：
- Normal 模式：上传 DOCX 技术交底书
- Fast 模式：仅输入简要发明构思，系统自动扩写为结构化交底后再生成专利文稿

应用不依赖外部 AI CLI，直接调用 Python SDK（Anthropic-compatible / OpenAI-compatible API）。

## 快速开始

### 方法1: 使用启动脚本（推荐）

```bash
python run_app.py
```

启动脚本会自动：
- 检查并安装依赖
- 创建必要目录
- 检查可用 Runtime backend
- 启动 Web 应用

### 方法2: 直接启动 Streamlit

```bash
pip install -r requirements.txt
streamlit run patent_writer_app.py
```

## 系统要求

- Python 3.8+
- 至少配置一个模型后端：
  - Anthropic-compatible：`ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`
  - OpenAI-compatible：`OPENAI_API_KEY`
- 网络连接（用于 API 调用）

## 使用方法

1. 启动应用：`python run_app.py`
2. 在侧边栏创建或加载 Session
3. 选择 Runtime backend（`anthropic` 或 `openai`）
4. 选择输入模式：
   - Normal：上传并保存 DOCX
   - Fast：输入发明构思
5. 点击 `Start generation`
6. 在 `Log stream` 查看执行日志，在 `Generated files` 查看产物

## 输出目录

```
output/
├── {session_id}.log
├── {session_id}.pid.json
└── temp_{session_id}/
    ├── 01_input/
    ├── 02_research/
    ├── 03_outline/
    ├── 04_content/
    ├── 05_diagrams/
    └── 06_final/
```

## 故障排除

1. 后端未就绪
- 检查环境变量是否已设置
- 检查依赖是否安装：`pip install -r requirements.txt`
- 在 UI 中查看提示的缺失项（变量名/包名）

2. 进程无法中止
- 先使用 `Stop session`
- 若仍有残留，使用 `Force cleanup runner`

3. 日志不显示
- 点击 `Refresh`
- 检查 `output/{session_id}.log` 是否存在

## 隐私说明

- 上传文件和产物保存在本地工作目录
- API 调用遵循所选模型服务提供方策略
- 建议定期清理日志和历史产物
