# api-strike

Portable quota / rate-limit pressure harness for the Google Generative Language API.
Pure Python standard library, no dependencies, no install, runs from a USB stick.

> 一个可移植的 API 配额压力测试器：纯标准库、零依赖、可放 U 盘直接跑。
> 用于验证一个 API key 的配额边界、429 触发阈值与限速行为。

## Quick start

```bash
python strike.py probe                     # 只读：key 是否有效 + 可见模型数
python strike.py map                       # 每个模型打一发，画出 200 / 429 分界
python strike.py strike                    # 按 config.json 持续压测
python strike.py strike --budget 20        # 小剂量试打
python strike.py selftest                  # 本地假服务器自检，不联网
```

Windows 双击入口：

```bat
run.bat probe
run.bat strike --duration 30 --workers 12 --rpm 600
```

## What it does

| 子命令 | 行为 | 联网 |
|---|---|---|
| `probe` | `GET /models`，输出 HTTP 状态、模型数量与名称 | 1 请求 |
| `map` | 对每个模型发一次最小 `generateContent`，标出 200 / 429 / 404 | N 请求 |
| `strike` | N 线程持续发请求，按 `rpm` 限速，写 `results.jsonl` + `summary.json` | 持续 |
| `selftest` | 起本地 stub 服务器（前 25 发 200，之后 429），验证引擎与计数 | 不联网 |

## Config

`config.json`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `key` | `""` | API key。**仓库里留空**，本地填自己的，或用 `--key` 传入 |
| `base` | `https://generativelanguage.googleapis.com/v1beta` | 端点，可指向自建 mock |
| `models` | `[]` | 空 = 自动从 `/models` 拉全量 |
| `prompt` | `ping` | 请求体文本，越短越省配额 |
| `workers` | `12` | 并发线程数 |
| `rpm` | `600` | 每分钟请求上限，`0` = 不限 |
| `duration` | `60` | 持续秒数 |
| `max_requests` | `0` | 总请求上限，`0` = 由 duration 控制 |
| `proxy` | `""` | `http://host:port`；空则读 `HTTPS_PROXY` / 系统代理 |

## Output

`results.jsonl` — 每请求一行：

```json
{"ts": 1793812345.12, "t_rel": 1.31, "model": "gemini-2.5-flash",
 "status": 429, "bucket": "quota", "latency_ms": 88.4, "err": "..."}
```

`summary.json`：

```json
{
  "sent": 24, "rps": 0.31,
  "buckets": {"ok": 4, "quota": 4},
  "http_codes": {"200": 4, "429": 4},
  "first_quota_at_s": 1.31,
  "first_quota_after_requests": 3,
  "latency_ms": {"p50": 14.0, "p95": 16.0, "max": 516.0},
  "key_still_valid": true
}
```

`bucket` 取值：`ok` `quota` `key_revoked` `forbidden` `no_model` `bad_request` `server_error` `transport` `other`。

三个判读要点：

1. `first_quota_after_requests` 小 → 配额本就接近上限；大（数千）→ 还打得动。
2. `quota` 占比接近 100% 且持续 → 已封顶，继续只是维持压力。
3. 出现 `key_revoked`（403 `API_KEY_INVALID`）→ key 已吊销，引擎自动停。

## Portable use (USB)

```
E:\api-strike\
  strike.py  config.json  run.bat  README.txt
  py\python.exe        <- optional portable interpreter
```

目标机没有 Python 时：把 `python-3.12.x-embed-amd64.zip` 解压到 `py\`，`run.bat` 会优先使用它。
无管理员权限、无注册表写入、无后台服务；只在本目录生成 `results.jsonl` / `summary.json`。

## Requirements

- Python 3.8+（3.8 可跑 Win7；3.9+ 需 Win8.1+）
- 出站 443 到 `generativelanguage.googleapis.com`
- 系统时间正确（TLS 校验）

不需要：Google 账号、GPU、浏览器扩展、pip、管理员权限。

## Notes

- `results.jsonl` 每 20 条 flush + fsync，拔盘最多丢 20 条；追加模式，重跑不覆盖历史。
- 所有传输层异常归类为 `transport`，不中断整轮。
- 请求 URL 里 key 位于 query string（与上游 API 设计一致）；若出口有记录完整 URL 的代理，key 会出现在其日志中。使用自建 key 可完全避免这一暴露面。
- 本仓库不包含任何 key。`config.json` 的 `key` 字段留空是刻意的：请用 `--key` 或本地 `config.local.json`（已 gitignore）。
