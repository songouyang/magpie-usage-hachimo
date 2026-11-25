# Magpie 独立只读采集器 · 1.0.3

macOS，Python 3.9+，仅依赖标准库。本机 Magpie → 本采集器 → `data/magpie-usage.txt` → Pet Manager Python 插件 → USB → 设备。采集器不操作 USB，不写客户端私有存档，不绑定组件哈希或客户端版本。

## 启动和授权

保持本机 Magpie 运行，在本目录执行：

```sh
python3 -m magpie_usage
```

也可双击 `start-collector.command`；若 macOS 限制双击，使用上面的终端命令。默认生成本目录 `data/magpie-usage.txt`。每次采集完成约 15 秒后再次采集。停止用 Control-C；关闭前台终端会停止采集。不新增开机自启任务。已有实例时不要重复启动，进程锁或数据文件锁会拒绝第二个写入者。

在支持 `petui-python-v1` 的 Pet Manager「组件中心 → 我的组件」刷新「Magpie 用量」同一卡片；也可导入 1.0.3 的 `.hachiapp`。在详情「Python 数据插件」选择 `data/magpie-usage.txt` 并授权，等待 telemetry 可用，点击「保存并同步」。升级组件需要重新授权。运行中保持 Pet Manager 和采集器打开，插件每 30 秒读取文件。本地发布不覆盖设备内容。

## 参数与诊断

```sh
python3 -m magpie_usage --magpie-url http://127.0.0.1:3425
python3 -m magpie_usage --feed-file /absolute/path/magpie-usage.txt
python3 -m magpie_usage --once
python3 -m magpie_usage --local-only --once
python3 -m magpie_usage --output ./snapshot.json
```

`--once` 采集一轮后退出；成功退出码 0，失败为 1。`--local-only` 不更新插件数据文件，但仍可更新成功缓存和诊断。`--output` 输出可读的本机 JSON 诊断，父目录必须存在；不能选组件库或客户端私有目录。数据文件、缓存与诊断输出须使用不同路径。`feedWrite.written=true` 表示文件落盘成功，设备接收仍依赖客户端授权、连接及服务。

默认缓存 `.last-success.json`，可用 `--cache-file` 指定，父目录须已存在。缓存保留最近一次成功聚合，重启后按原成功时间判断过期；采集失败保留成功数据。未取得成功数据时显示「数据更新：—」，成功时间超过 60 秒显示「数据过期」，恢复后更新时间推进。缓存和诊断可能包含个人用量，不要随包分享。

## 文件协议与故障处理

文件固定为 8192 字节，包含两个各 4096 字节的 ASCII 数据槽。每槽包含序号、JSON payload 及覆盖序号和 payload 的 FNV-1a 校验。单写入者交替原位更新并刷新落盘，文件身份保持不变。插件读最新完整槽，忽略半写入或校验不通过的槽；两槽完全无效时使用占位。超限拒绝写入，不截断数据。

不要在编辑器中重新保存、移动或复制覆盖已授权文件。文件被替换后采集器停止，须重启并重新选择授权文件。符号链接、硬链接、损坏槽文件和异常长度不会被自动覆盖；停掉采集器，改用新的 `--feed-file` 路径再授权即可。锁随进程退出释放，不必删除锁文件。两个不同解压目录写同一数据文件也会被文件锁阻止。

## 指标与连接

复用 MIT 原项目 `/v1/magpie/quotas`、`/v1/magpie/usage`、`/v1/magpie/usage/requests?limit=1`，先检查健康接口。今日为 usage series 最后一个桶，近七天为最后七个桶，会话和缓存采用 API 汇总；Token 沿用 Input／Output／Cache Read／Reasoning。没有调用时成功率未知，缺失显示「—」，观察到的零保留，无限额窗口不参与最低额度。

仅 loopback HTTP，不接受 URL 凭据和重定向。三项指标并发读取，任一失败不产生新成功时间；超时 10 秒，单项响应最多 2 MiB。不保存请求正文或账号凭据。时区 Asia/Shanghai，模型名按 UTF-8 和显示宽度限制，耗时达到一秒后以秒显示。四页 UI 与绑定沿用原组件，静音。

当前实测 Pet Manager 0.1.260 和 Pico USB 安装；其他设备仍需通过原生兼容检查。此链路要求 PC 运行，断开 USB 后使用现有固件连接不可用界面，不承诺 LCD 保留最后快照。实体按键、触点、LCD 观感和物理拔插恢复未测。
