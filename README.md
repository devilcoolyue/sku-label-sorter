# 面单 SKU 排序合并工具

上传一个或多个面单 PDF，按面单上的 SKU（FDC/HD/BDL/PQ/AMB/LZ 等前缀，可配置）
全局排序后合并输出一个新 PDF。

## 识别策略（三级兜底）

| 级别 | 方式 | 说明 |
|---|---|---|
| 1 | 文本层 | 直接读 PDF 文本，最快、零成本 |
| 2 | 本地 OCR | 文本层没有 SKU 时整页 OCR（rapidocr），免费 |
| 3 | 大模型 | 多模态接口识别整页图片，结构化输出并经正则校验 |

前端可选四种方案：`仅文本` / `OCR` / `OCR+大模型` / `全部大模型`（默认）。
默认走大模型是因为实测面单上的 SKU 多为图片印刷；只想零成本跑的话
改成 `OCR` 即可。

大模型支持两种接口，前端可切换：

| 接口 | 说明 |
|---|---|
| Anthropic | Claude 官方接口，选模型 + 填 Key |
| OpenAI 协议 | base_url / api_key / 模型名全部自填，可对接官方 API、中转站，或 vLLM、Ollama、LM Studio 等自建服务 |

走 OpenAI 协议时模型必须支持图片输入。为兼容各类端点做了两处处理：

- 可选参数（`response_format`、`max_tokens`）被端点拒绝时自动剥离重试，
  且降级结果对后续页面持续生效，不会每页都白跑一次
- User-Agent 固定为 `label-sorter/1.0`。部分中转站的 WAF 把 openai SDK
  自带的 `OpenAI/Python x.y.z` 列入黑名单，直接返回 Cloudflare 1010
  （`Your request was blocked`），换掉即可通过

## 安装依赖

```bash
pip install pymupdf fastapi uvicorn python-multipart          # 基础
pip install rapidocr-onnxruntime opencv-python-headless       # OCR 兜底
pip install anthropic                                         # Anthropic 接口
pip install openai                                            # OpenAI 协议接口
```

## 启动

```bash
cd label_sorter
python app.py                 # 默认 http://0.0.0.0:8000
# 或: uvicorn app:app --host 0.0.0.0 --port 8000
```

使用大模型兜底时，在服务器设置 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
环境变量，或在前端配置页里直接填入 API Key（服务端只在任务期间留在内存里）。

## 登录鉴权

服务默认开启登录，未登录时**任何路径都返回登录页**、任何 `/api/` 请求都返回
401 —— 拦截做在服务端中间件里，不是前端跳转，所以直接敲 URL 也拿不到页面。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SORTER_USER` | `admin` | 用户名 |
| `SORTER_PASSWORD` | 随机生成 | 未设置时启动生成一个强口令，打印到日志并存入 `$SORTER_WORK_DIR/.password`（0600） |
| `SORTER_SESSION_HOURS` | `12` | 登录有效期 |
| `SORTER_COOKIE_SECURE` | `0` | 上了 HTTPS 就设为 `1` |

- token 是 HMAC-SHA256 签名的，密钥存在 `$SORTER_WORK_DIR/.secret`，
  **服务重启不掉线**；无服务端会话表，也就没有多进程共享的问题
- token 放在 `HttpOnly` + `SameSite=Strict` 的 Cookie 里，JS 读不到
- 同一 IP 连续 5 次登录失败锁定 15 分钟

刻意不设弱默认口令（没有 `admin/admin`），漏配 `SORTER_PASSWORD` 时会随机生成
而不是放行。

## 页面流程

1. 拖拽/选择一个或多个 PDF
2. 配置 SKU 前缀与识别方案（选大模型时再选接口、模型、填 Key）
3. 点击「保存配置」可把当前配置留在本浏览器，下次打开自动回填
4. 点击「开始排序合并」，看每页状态格子实时变色（每行 15 个，格内为页码）：
   灰=待处理　蓝=处理中　绿=已识别　黄=无 SKU　红=出错　紫=复查修正
5. 完成后查看统计、复查改动、可疑告警，下载合并后的 PDF
6. 需要确认时点「用相同配置复查比对」，重跑一遍并逐页对照
7. 历史记录保留最近 50 次任务，可回看配置与统计、重新下载 PDF、逐条删除
   （服务重启后仍在，超出上限的任务目录会被自动清理）

「保存配置」写入浏览器 localStorage，**包含填在页面上的 API Key**，
共用电脑上请留意；服务端不会落盘保存 Key。

## 并发

识别以等待网络为主，默认 4 线程并发，前端可调（1~16）。实测 20 页、
每次请求 400ms：1 线程 9.3s，4 线程 2.3s，8 线程 1.4s。

- 每个线程独立打开 PDF —— `fitz.Document` 不是线程安全的
- OCR 走锁串行，因为 onnxruntime 本身已经吃满多核，再并发无益
- 页序与串行完全一致（结果按索引回填，排序仍是稳定排序）
- 连续 5 页失败即中止，避免 Key/地址配错时把整批跑完才报错

## 识别纠错

模型读图偶尔会漏读一位，把 `HD-722-3*1` 读成 `HD-72-3*1` —— 格式合法，
正则校验挡不住，但会让这页排到完全错误的位置。三道处理：

| 手段 | 作用 |
|---|---|
| `temperature=0` + 提示词强调逐位读 | 降低出错概率 |
| 批内一致性检查 | 少一位、又比高频 SKU 罕见得多的，标为可疑 |
| **切块复查** | 对可疑页把整页横向切 3 条带（带重叠）分别重读 |

切块复查的道理：多模态模型会把整页降采样到短边约 768px，SKU 数字只剩
几十像素；切成条带后同样的像素预算只覆盖三分之一页面，等效放大重读。
**直接调高渲染倍率没有用** —— 3x 已经触及降采样上限，4x/6x 到模型那里
是同一张图，只多花钱。

复查只针对少数可疑页，每页多 3 次调用。结果不一致时采用复查值，并在
界面上逐页列出改动；一致则确认原值、消除告警。可在前端关闭。

**只兜「漏读一位」这一类。** 若模型把 `722` 读成 `723`（替换而非漏读），
批内比对发现不了，复查也不会触发。

## 整批复查比对

结果页的「用相同配置复查比对」会把同一批文件用原配置**完整重跑一遍**，
逐页对照两次结果，列出有出入的页。适合在一批面单交付前做一次确认 ——
识别有随机性，跑两遍都一致才比较可信。

- 逐页结果落在任务目录的 `pages.json`，服务重启后仍可复查
- API Key 不落盘，由前端在发起复查时重新带上
- 复查是独立任务，有自己的进度网格与输出 PDF，在历史里带「复查」标记
- 统计与新 PDF 用的是**第二次**的结果

## 排序规则

- 每页取第一个 SKU 作为排序键，自然排序（HD-2 排在 HD-10 前）
- 相同 SKU 的页面保持上传文件顺序与页内顺序（稳定排序）
- 未识别到 SKU 的页面保持原顺序排在文件末尾，并在结果页给出警告

## 文件结构

```
.
├── sort_labels_by_sku.py       # 命令行版（无需 Web，单文件可独立使用）
├── Dockerfile                  # 容器化部署（老系统装不动依赖时走这个）
└── label_sorter/               # Web 版
    ├── app.py                  # FastAPI 后端（上传/任务/进度/历史/下载）
    ├── auth.py                 # 登录鉴权（签名 token + 失败锁定）
    ├── sorter.py               # 核心识别与排序逻辑（也可单独 import 使用）
    └── static/
        ├── index.html          # 主页面
        └── login.html          # 登录页
```

## Docker 部署

```bash
docker build -t sku-label-sorter .
docker run -d --name sku-label-sorter --restart unless-stopped \
  -p 8000:8000 -v /your/data:/data \
  -e SORTER_PASSWORD='你的口令' \
  sku-label-sorter
```

镜像基于 `python:3.11-slim`，约 712MB（大头是 OCR 用的 onnxruntime）。
数据卷挂 `/data`，历史记录与登录密钥都在里面，容器重建不丢。

**必须单进程跑**，不要加 `--workers` —— 任务状态存在进程内存里。

## License

MIT
