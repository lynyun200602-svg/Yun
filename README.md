# 发面馒头

基于个人简历 / 项目资料做面试问答的 Web 应用。上传简历资料后提问，后端调用 Claude 基于资料回答，问答对存入本地 SQLite 数据库，重复问题直接复用历史答案。

## 功能

- 📁 上传简历资料（pdf / docx），原始文件保存在本地磁盘
- 💬 面试问答：后端调用 Claude API，严格基于上传资料回答，禁止编造
- 🗂️ 问答缓存：字符串完全相等匹配复用历史回答；可查看 / 编辑 / 删除 / 导出 Markdown
- ⚙️ 设置：配置 Claude API Key、模型、清空知识库
- 📱 响应式页面，PC 与手机浏览器均适配

## 技术栈

FastAPI · SQLite · Anthropic Claude API · 原生 HTML/CSS/JS

## 目录结构

```
app.py              # FastAPI 主应用（路由 + API 接口）
db.py               # SQLite 问答缓存表操作
document.py         # 读取 pdf/docx 完整文本
claude_client.py    # 调用 Claude API
requirements.txt    # Python 依赖
.env                # 配置（API Key 等，不入库）
data/
  sqlite/qa_cache.db      # 问答缓存数据库
  upload_files/           # 上传的原始简历文档
static/             # 前端页面
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

复制 `.env.example` 为 `.env`，填入你的 Claude API Key（在 https://console.anthropic.com 生成）：

```
ANTHROPIC_API_KEY=sk-ant-xxxx
```

也可以在启动后进入「设置」页面在线填写。

### 3. 启动

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
# 或
python app.py
```

### 4. 访问

- 本机电脑：http://127.0.0.1:8000
- 局域网手机（同一 WiFi）：http://本机局域网IP:8000 （防火墙需放行 8000 端口）

## 说明

- 第一版未使用向量检索 / embedding，`qa_cache.query_embedding` 字段已保留，留作后续语义相似度检索迭代。
- 缓存命中逻辑为「字符串完全相等匹配」，命中直接返回历史答案、不调用 Claude API。
- 上传文件仅保存原始文件，提问时读取全部文档完整文本作为上下文。
