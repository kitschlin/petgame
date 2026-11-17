# 萌宠竞技场

同屏双人宠物回合制对战 Demo，带一个基于 LLM 的 AI Coach（选将推荐、战斗指导、战后复盘、培养建议）。浏览器里玩，后端是 Python。

## 1. 配置环境

需要 Python 3.10 及以上（推荐 3.12）。任选一种：

```bash
# 方式 A：conda
conda env create -f environment.yml
conda activate petarena

# 方式 B：venv + pip
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. 启动

```bash
python run.py                      # 打开 http://127.0.0.1:8765
```

默认端口是 **8765**（以前是 8000）。8000 很常用，本机或 IDE 的端口转发上容易被别的程序或旧的转发占着，浏览器打开会变得很慢甚至连到别处；换端口用 `--port`。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 想让局域网 / 公网上的人访问，改为 `0.0.0.0` |
| `--port` | `8765` | 端口 |
| `--data-dir` | `data/` | 存档目录（对局记录、PVE 成长、AI 经验库）；删掉即重置进度 |
| `--seed` | 无 | 固定随机种子，用于复现对局 |

### 部署到服务器给别人玩

```bash
python run.py --host 0.0.0.0 --port 8765
```

- 记得在服务器防火墙 / 安全组里放行该端口，别人用 `http://<服务器IP>:8765` 访问。
- 在远程机器上只想自己看：`ssh -L 8765:127.0.0.1:8765 <主机>`，然后本地浏览器打开 `http://127.0.0.1:8765`。
- 用 VS Code 远程开发时：在 Ports 面板转发 8765，再用外部浏览器打开它显示的 Forwarded Address。如果 VS Code 内置浏览器很快、外部浏览器很慢，多半是这条转发或本机端口被占了：Stop Forwarding 后重新转发，或换一个端口。
- 后台常驻：`nohup python run.py --host 0.0.0.0 > petarena.log 2>&1 &`。
- 存档是单份的：所有访问者共用同一个进度和同一局对战，适合演示，不是多人联机服务。

## 3. LLM API 的使用方式

AI Coach 通过 **OpenAI 兼容的 `/chat/completions` 接口** 调用大模型，默认接 DeepSeek。**不配置 key 也能完整地玩**：AI 军师面板只显示「请先配置 LLM API」和配置方法，不会给任何建议（不会退回假模型）。

1. 复制配置文件：

   ```bash
   cp .env.example .env
   ```

2. 在 `.env` 里填入自己的 key（`.env` 已在 `.gitignore` 中，不会被提交）：

   ```bash
   COACH_API_KEY=sk-xxxxxxxx      # 旧名 DEEPSEEK_API_KEY 仍然有效
   ```

3. 重新运行 `python run.py`，页面上出现 AI 军师 / AI 指导面板即为启用成功。

也可以不写文件，直接用环境变量：`COACH_API_KEY=sk-xxx python run.py`（环境变量优先于 `.env`）。

### 换用其他模型 / 服务商

只要接口兼容 OpenAI 的 `/chat/completions`，改下面三项即可。例如 SiliconFlow：

```bash
COACH_API_KEY=<你的 key>
COACH_BASE_URL=https://api.siliconflow.cn/v1  # 默认 https://api.deepseek.com
COACH_MODEL=deepseek-ai/DeepSeek-V3.2         # 默认 deepseek-flash
```

客户端按服务商和模型自动选参数：DeepSeek 官方接口用 `thinking: disabled`；SiliconFlow 上的 Qwen3 / Qwen3.5、DeepSeek-V3.1 及以后这类混合思考模型用 `enable_thinking: false`；DeepSeek-R1 这类推理模型关不掉思考，不发 JSON 模式参数、放宽输出上限，靠本地修复解析 JSON（它们很慢，战斗指导的 8 秒超时多半不够，不推荐）。账户余额不足（HTTP 402）和 key 无效一样不自动重试，面板上直接写明原因。

### 常用开关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PETARENA_COACH` | 有 key 为 `live`，否则 `off` | `off` 整个关闭 AI（连「请配置 API」的提示也不显示）；`live` 调用真实模型。另有 `fake` 只给自动化测试用，必须显式设置，不会自动启用 |
| `COACH_TIMEOUT` | `20` | 选将、复盘、培养建议单次请求超时（秒） |
| `COACH_FAST_TIMEOUT` | `8` | 战斗指导单次请求超时（秒） |
| `COACH_WORKERS` | CPU 核数 − 1，最多 16 | 后台推演（选将现场推演、对局结束后的复盘）用的进程数；设为 `0` 不开进程池，全部在服务进程里算 |
| `COACH_FAST_WORKERS` | CPU 核数 − 1，最多 64 | 战斗指导专用的进程数（启动时就拉起来，玩家等建议时不用排队）；机器配置低或和别人共用机器时调小，例如 `8` |
| `COACH_ADVICE_ROLLOUTS` | 按 `COACH_FAST_WORKERS` 自动取 128～1024 | 战斗指导时每个合法动作推演几局；嫌建议慢可以调小，例如 `128` |
| `COACH_REFLECT` | `background` | 对局结束后的复盘分析：`background` 后台 / `sync` 当场 / `off` 打开复盘页时才分析 |

其余可选项见 `.env.example` 中的注释。

> AI 只给建议，不替玩家操作；请求失败会自动重试 2 次，仍失败时可手动重试，任何情况下都不影响对局进行。

## 4. 怎么玩

打开首页会自动弹出「玩法速览」，第一次进入选将、战斗时有界面导览，右上角可以随时重看。推荐路线：PVP 对战 → 选「智能对手」→ 照 AI 军师的高亮选将、照 AI 指导的高亮出招 → 输了看「AI 复盘」和培养建议 → 去 PVE 历练后再战。
