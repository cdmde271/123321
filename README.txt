strike.py - portable API quota strike harness
=============================================

纯标准库，Python 3.8+，零安装。整个目录拷到 U 盘即可，写入的 results.jsonl /
summary.json 都落在脚本旁边（会在 U 盘上生成文件）。

U 盘目录结构
------------
  E:\api-strike\
    strike.py          <- 主程序（唯一必需文件）
    config.json        <- 参数（key / workers / rpm / duration）
    run.bat            <- 双击入口，自动找解释器
    results.jsonl      <- 运行时生成：每请求一行
    summary.json       <- 运行时生成：汇总
    py\                <- 可选：便携 Python（见下）
    README.txt

目标机没有 Python 时
--------------------
  1. 下载 python-3.12.11-embed-amd64.zip（python.org 的 embeddable 包，约 11 MB）
  2. 解压内容到 U 盘的 py\ 目录，确保存在 py\python.exe
  3. run.bat 会自动优先用 .\py\python.exe
  embeddable 包自带标准库，本程序不依赖 pip 包，直接可跑。

用法
----
  run.bat probe                          只读探测：key 是否有效、可见模型数
  run.bat map                            每个模型打一发，标出 200 / 429 分界
  run.bat strike                         按 config.json 跑（默认 12 并发 / 600 rpm / 60s）
  run.bat strike --duration 30           覆盖时长
  run.bat strike --workers 24 --rpm 0    24 并发，不限速
  run.bat strike --budget 2000           总量封顶 2000 发
  run.bat selftest                       本地假服务器自检，不联网

等价直接调用：
  python strike.py strike --duration 60 --workers 16 --rpm 600 --budget 5000

参数说明（config.json）
-----------------------
  key          目标 API key。仓库里留空，运行前用 --key 传入或在 config.json 填。
  models       [] 表示自动从 /models 拉全量；也可写死 ["gemini-2.5-flash"]。
  prompt       请求体里的文本，越短越省配额、越快。
  workers      并发线程数。
  rpm          每分钟请求上限，0 = 不限。真实配额下 600~1200 比较"安静"。
  duration     持续秒数。
  max_requests 总请求上限，0 = 由 duration 控制。
  retry_5xx    5xx 重试次数。

输出怎么读
----------
  results.jsonl  每行 {ts, t_rel, model, status, bucket, latency_ms, err}
  summary.json   sent / rps / buckets / http_codes / first_quota_at_s /
                 first_quota_after_requests / latency_ms / key_still_valid

  bucket 取值：ok(200) quota(429) key_revoked(403 API_KEY_INVALID)
               forbidden(403 其他) no_model(404) bad_request(400)
               server_error(5xx) transport(连不上) other

三个关键判读
------------
  1. first_quota_after_requests 很小（比如 < 50）  -> 配额本来就快见底，或用的是免费层
     很大（几千）                                  -> 打得动，需要更久才封顶
  2. quota 占比接近 100% 且持续                  -> 已封顶，继续打只是维持压力
  3. 出现 key_revoked                            -> 作者已吊销 key，程序自动停；换 key 才有意义

注意事项（工程层面）
--------------------
  * 程序只写自己目录下的两个文件，不装服务、不写注册表、不需要管理员权限。
  * 每 20 条 flush + fsync，拔盘最多丢 20 条。
  * 所有异常（DNS/TLS/超时/重置）都归类为 transport，不会中断整轮。
  * 断点续跑：results.jsonl 是追加模式，重跑不会覆盖历史。
  * 目标机若走会记录 URL 的代理（校园网/企业 TLS 检查），key 在 query 里是明文，
    这一点与程序无关，是目标 API 的设计。自建 key 可完全避开。
