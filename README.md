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
python run.py                      # 打开 http://127.0.0.1:8000
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 想让局域网 / 公网上的人访问，改为 `0.0.0.0` |
| `--port` | `8000` | 端口 |
| `--data-dir` | `data/` | 存档目录（对局记录、PVE 成长、AI 经验库）；删掉即重置进度 |
| `--seed` | 无 | 固定随机种子，用于复现对局 |

### 部署到服务器给别人玩

```bash
python run.py --host 0.0.0.0 --port 8000
```

- 记得在服务器防火墙 / 安全组里放行该端口，别人用 `http://<服务器IP>:8000` 访问。
- 在远程机器上只想自己看：`ssh -L 8000:127.0.0.1:8000 <主机>`，然后本地浏览器打开 `http://127.0.0.1:8000`。
- 后台常驻：`nohup python run.py --host 0.0.0.0 > petarena.log 2>&1 &`。
- 存档是单份的：所有访问者共用同一个进度和同一局对战，适合演示，不是多人联机服务。

## 3. LLM API 的使用方式

AI Coach 通过 **OpenAI 兼容的 `/chat/completions` 接口** 调用大模型，默认接 DeepSeek。**不配置 key 也能完整地玩**，只是没有 AI 面板。

1. 复制配置文件：

   ```bash
   cp .env.example .env
   ```

2. 在 `.env` 里填入自己的 key（`.env` 已在 `.gitignore` 中，不会被提交）：

   ```bash
   DEEPSEEK_API_KEY=sk-xxxxxxxx
   ```

3. 重新运行 `python run.py`，页面上出现 AI 军师 / AI 指导面板即为启用成功。

也可以不写文件，直接用环境变量：`DEEPSEEK_API_KEY=sk-xxx python run.py`（环境变量优先于 `.env`）。

### 换用其他模型 / 服务商

只要接口兼容 OpenAI 的 `/chat/completions`，改下面三项即可（key 仍然填在 `DEEPSEEK_API_KEY` 里）：

```bash
DEEPSEEK_API_KEY=<你的 key>
COACH_BASE_URL=https://api.deepseek.com     # 服务地址
COACH_MODEL=deepseek-flash                  # 模型名
```

### 常用开关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PETARENA_COACH` | 有 key 为 `live`，否则 `off` | `off` 关闭 AI；`fake` 不联网的假模型（数字照常来自引擎，讲解是套话，适合没有 key 时预览界面）；`live` 调用真实模型 |
| `COACH_TIMEOUT` | `20` | 选将、复盘、培养建议单次请求超时（秒） |
| `COACH_FAST_TIMEOUT` | `8` | 战斗指导单次请求超时（秒） |
| `COACH_WORKERS` | CPU 核数 − 1，最多 16 | 推演用的进程数；机器配置低时调小 |
| `COACH_REFLECT` | `background` | 对局结束后的复盘分析：`background` 后台 / `sync` 当场 / `off` 打开复盘页时才分析 |

其余可选项见 `.env.example` 中的注释。

> AI 只给建议，不替玩家操作；请求失败会自动重试 2 次，仍失败时可手动重试，任何情况下都不影响对局进行。

## 4. 怎么玩

打开首页会自动弹出「玩法速览」，第一次进入选将、战斗时有界面导览，右上角可以随时重看。推荐路线：PVP 对战 → 选「智能对手」→ 照 AI 军师的高亮选将、照 AI 指导的高亮出招 → 输了看「AI 复盘」和培养建议 → 去 PVE 历练后再战。
