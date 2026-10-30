---
name: weather
description: 查询全球城市的实时天气和未来预报，基于 wttr.in / Open-Meteo 免费接口，无需 API Key
tags: weather, curl, api
---

# 天气查询技能

通过 bash 工具调用免费的天气接口获取数据。**优先使用 wttr.in**，失败时回退到 Open-Meteo。两者都不需要 API Key。

## 方式一：wttr.in（首选，一行搞定）

```bash
# 人类可读的实时天气摘要（自动识别中文城市名）
curl -s "wttr.in/北京?format=%l:+%c+%t+湿度%h+风速%w&lang=zh"

# 未来 3 天预报：窄版报告（约 1-2 KB，可接受；不要用去掉 n 的完整彩色报告，输出大十倍）
curl -s "wttr.in/Shanghai?n&lang=zh"
```

要点：
- 城市名用拼音或英文更稳定（如 `Beijing`），中文名也支持但需确保终端 UTF-8 编码正常。
- Windows Git Bash 下 `%` 无需转义，直接写在 URL 里即可。
- ⚠️ **不要用 `format=j1`**：它返回几万字符的完整 JSON，会瞬间撑爆上下文、触发压缩循环。
  只有在用户明确要求原始字段时才用，并立即挑出需要的字段（`current_condition[0].temp_C` /
  `humidity` / `windspeedKmph` / `weatherDesc[0].value`），绝不要把整段 JSON 留在对话里。

## 方式二：Open-Meteo（wttr.in 不可达时的回退）

Open-Meteo 需要经纬度，先用其地理编码接口查坐标，再查天气：

```bash
# 1. 城市名 -> 经纬度（取 results[0] 的 latitude / longitude）
curl -s "https://geocoding-api.open-meteo.com/v1/search?name=Beijing&count=1&language=zh"

# 2. 经纬度 -> 实时天气 + 7 天预报
curl -s "https://api.open-meteo.com/v1/forecast?latitude=39.9&longitude=116.4&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min,weather_code&timezone=auto"
```

`weather_code` 是 WMO 数字代码，常用值：0=晴，1-3=多云，45/48=雾，51-55=毛毛雨，61-65=雨，71-77=雪，80-82=阵雨，95-99=雷暴。回答用户时翻译成自然语言。

## 回答用户的规范

1. 用户没给城市时，先问清城市，不要擅自假设。
2. 默认回答：当前温度、天气状况、湿度、风速，以及今天/明天的最高最低温；用户只问"天气如何"时不要堆砌全部字段。
3. 单位统一用摄氏度和 km/h，除非用户要求其他单位。
4. 接口请求失败（超时、非 200、JSON 解析失败）时：换一种方式重试一次；仍失败则如实告知用户，**不要编造天气数据**。

## 示例对话

用户：北京今天天气怎么样？

```bash
curl -s "wttr.in/Beijing?format=%l:+%c+%t+湿度%h+风速%w&lang=zh"
```

→ 输出类似 `Beijing: ☀️ +26°C 湿度35% 风速12km/h`，直接转述即可。
不要为了"保险"额外跑一次 JSON 接口——一行格式已经包含全部所需信息。
