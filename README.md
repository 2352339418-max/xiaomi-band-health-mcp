# 小米手环健康数据 MCP 服务

把小米手环（通过 Gadgetbridge 记录）的步数、心率、睡眠、血氧数据，接给任意支持 MCP 的 AI 助手。

纯 Python 标准库实现，**零第三方依赖**，不需要 root，不需要电脑。

```
小米手环 → Gadgetbridge → 自动导出 SQLite → health_mcp.py → MCP → AI 助手
```

## 为什么做这个

Gadgetbridge 会把数据存在应用私有目录（`/data/data/nodomain.freeyourgadget.gadgetbridge/databases/`），普通应用读不到，非 root 也读不到。

但它自带**自动导出**功能，可以把数据库定期复制到 `/sdcard/Download/` 这种公共目录。本项目的思路就是：读那份导出文件，解析成结构化数据，用 MCP 协议暴露给 AI。

这样 AI 就能回答「我今天睡了几个小时」「这周平均心率多少」这类问题，而不用你手动截图。

## 环境要求

| 项目 | 要求 |
|---|---|
| 手机 | Android 8.0+（本项目在魅族 21 / Android 16 上测试） |
| 手环 | 小米手环（本项目在 Smart Band 9 上测试） |
| Gadgetbridge | **2026.11.1 或更新**（旧版自动导出有 bug，见下文） |
| Python | 3.8+（Termux、QPython、DSH Go 等自带） |

## 步骤一：配置 Gadgetbridge 自动导出

1. 打开 Gadgetbridge，连上手环，同步一次数据
2. 进 **设置 → 开发者选项 → 意图接口**，打开：
   - 允许数据库导出
   - 数据库导出时广播
3. 回到设置页，找到 **自动导出**，开启并设置导出路径为 `/sdcard/Download/Gadgetbridge`
4. 点 **立刻运行** 验证

导出成功后，用文件管理器确认 `/sdcard/Download/Gadgetbridge` 存在且**不是 0 字节**。

> **踩坑记录**：Gadgetbridge 0.91.1 及更早版本的自动导出会静默失败——文件能创建出来，但大小是 0 字节，日志里也没有报错。原因是后台的 WorkManager 任务没有真正执行。升级到 2026.11.1 后恢复正常。

## 步骤二：启动服务

把 `health_mcp.py` 放到任意目录，运行：

```bash
python3 health_mcp.py
```

看到 `listening on 0.0.0.0:8787` 就成了。

后台常驻：

```bash
nohup python3 health_mcp.py >/tmp/health_mcp.log 2>&1 &
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GB_DB` | `/sdcard/Download/Gadgetbridge` | 导出的数据库路径 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8787` | 监听端口 |
| `TOKEN` | 空 | 设置后要求 `Authorization: Bearer <TOKEN>` |

```bash
GB_DB=/sdcard/Download/Gadgetbridge PORT=8787 TOKEN=your_secret python3 health_mcp.py
```

## 步骤三：接入 AI 助手

服务走 **MCP Streamable HTTP**，端点是 `http://127.0.0.1:8787/mcp`。

支持 MCP 的客户端配置示例（JSON）：

```json
{
  "mcpServers": {
    "health": {
      "type": "streamable_http",
      "url": "http://127.0.0.1:8787/mcp"
    }
  }
}
```

> 注意：部分客户端（如 RikkaHub）只支持 SSE 和 Streamable HTTP，**不支持 stdio**，所以必须用 HTTP 方式而不是命令行方式接入。

## 提供的工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `get_today_summary` | 无 | 当天总览：步数、距离、卡路里、心率（平均/最低/最高/静息）、血氧、压力、昨晚睡眠 |
| `get_steps` | `days`（默认 7） | 每日步数、距离、卡路里，含总计与平均 |
| `get_heart_rate` | `days`（默认 7） | 每日心率统计，以及最近一天的小时级曲线 |
| `get_sleep` | `days`（默认 7） | 每次睡眠的起止时间、总时长、深睡/浅睡/REM/清醒分钟数 |
| `get_spo2` | `days`（默认 7） | 每日血氧平均/最低/最高，以及低于 90% 的次数 |

## 数据来源说明

Gadgetbridge 的数据库是标准 SQLite，本项目读取以下表：

| 表名 | 内容 |
|---|---|
| `XIAOMI_ACTIVITY_SAMPLE` | 分钟级步数、心率、血氧 |
| `XIAOMI_DAILY_SUMMARY_SAMPLE` | 每日汇总（步数、距离、卡路里） |
| `XIAOMI_SLEEP_STAGE_SAMPLE` | 睡眠分期 |
| `XIAOMI_SLEEP_TIME_SAMPLE` | 睡眠起止时间 |
| `HUAMI_HEART_RATE_RESTING_SAMPLE` | 静息心率 |

所有时间戳都是**毫秒**级 Unix 时间。

## 常见问题

**Q：服务起来了，但读不到数据？**

先确认 `GB_DB` 指向的文件存在且不是 0 字节：

```bash
ls -lh /sdcard/Download/Gadgetbridge
```

如果是 0 字节，说明 Gadgetbridge 的自动导出没工作，升级 Gadgetbridge 试试。

**Q：能不能直接读 Gadgetbridge 的私有数据库？**

不能。`/data/data/nodomain.freeyourgadget.gadgetbridge/databases/` 只有 root 能读。Shizuku 给的是 shell 权限（uid 2000），也不行。`run-as` 同样不行。所以只能走自动导出这条路。

**Q：数据有延迟吗？**

取决于自动导出的频率。Gadgetbridge 默认每 3 小时导出一次，所以最多滞后 3 小时。想更新鲜可以在 Gadgetbridge 里手动点「立刻运行」。

**Q：手环刚连上，睡眠数据是空的？**

正常。睡眠、血氧这些需要手环同步过完整的一夜数据才会有。刚配对时只有当天的实时数据。

## 已知限制

- 只测试了小米手环 9，其他型号的表结构可能不同
- 心率的小时级曲线依赖分钟级采样，如果手环没开连续心率测量，数据会很稀疏
- 距离字段在某些固件版本下会返回 0，此时以步数为准

## License

MIT
