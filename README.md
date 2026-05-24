# Canvas → Apple Calendar Sync

自动从 [SJTU Canvas](https://oc.sjtu.edu.cn) 同步作业、习题课 (RC)、答疑时间 (OH) 到 macOS 日历，支持中文/英文混合识别。

## 功能

- **作业同步**：自动抓取所有课程有截止日期的作业，创建为日历事件
- **RC/OH 智能识别**：使用 DeepSeek 大模型理解公告、大纲、页面中的时间地点信息
- **时间变更检测**：当 RC/OH 时间被调整（如"周一调至周三"），自动删除旧安排、添加新安排
- **定时自动同步**：每 2 天在后台自动运行一次，无需手动操作
- **中文地点识别**：支持东中院、DZY、ZY103 等 SJTU 常见教室命名

## 环境准备

整个设置大约需要 5 分钟，你需要准备两样东西：

- **Canvas API Token**（从 Canvas 网页获取）
- **DeepSeek API Key**（从 DeepSeek 官网获取，用于智能识别 RC/OH）

---

## 第一步：获取 Canvas Token

1. 打开浏览器，登录 [oc.sjtu.edu.cn](https://oc.sjtu.edu.cn)
2. 点击左上角头像 → **Account**（账户）→ **Settings**（设置）
3. 往下滚动，找到 **Approved Integrations** → 点击 **+ New Access Token**
4. Purpose 填写 `Canvas Calendar Sync`，过期时间选 **No Expiration**
5. 点击生成，**复制那一长串 token 并保存好**（关闭页面后就看不到了）

---

## 第二步：获取 DeepSeek API Key

DeepSeek 是国产大模型，用来读懂公告里的人类语言（比如"RC 从周一调到了周三"）。

1. 打开 [platform.deepseek.com](https://platform.deepseek.com)
2. 注册账号（手机号即可）
3. 进控制台 → **API Keys** → **创建 API Key**
4. 复制 key（以 `sk-` 开头）
5. **充值 1 块钱就够了**，每次同步花费约 0.2 分钱，1 块钱能用一年多

---

## 第三步：下载项目

打开 Mac 上的 **终端**（Terminal，在启动台搜索"终端"），逐行复制粘贴以下命令：

```bash
# 下载项目
git clone https://github.com/ReeseNoctis/canvas-cal-sync.git
cd canvas-cal-sync
```

---

## 第四步：写入密钥

在终端中继续执行（**把引号里的内容替换成你自己的 key**）：

```bash
# 写入 Canvas token（第一步获取的那个）
echo "你的Canvas_Token粘贴到这里" > data/api_token.txt

# 写入 DeepSeek API key（第二步获取的那个，以 sk- 开头）
echo "你的DeepSeek_Key粘贴到这里" > data/api_key_llm.txt
```

⚠️ 这两个文件包含你的个人密钥，不会被上传到 GitHub（已写入 `.gitignore`）。

---

## 第五步：安装运行

```bash
# 安装依赖
pip3 install --quiet requests openai

# 试运行一次
python3 sync.py
```

打开 Mac 上的 **日历 App**（Calendar），你应该能看到一个名为 **"SJTU Canvas"** 的新日历，里面已经有你的作业和 RC/OH 安排了。

---

## 第六步：设置定时自动同步（可选）

```bash
# 安装定时任务（每 2 天自动跑一次）
./setup.sh
```

此后无需任何操作，日历会自动更新。

如果想关掉定时同步：

```bash
launchctl unload ~/Library/LaunchAgents/com.sjtu.canvassync.plist
```

---

## 配置说明

编辑 `config.json` 可以自定义行为：

### 课程筛选

```json
"course_filter": {
  "mode": "include",
  "list": [
    "ECE2160JSU2026",
    "GER1100JSU2026-1",
    "形势与政策"
  ]
}
```

- `"mode": "include"` → 只同步列表中匹配的课程
- `"mode": "exclude"` → 排除列表中匹配的课程
- 匹配不区分大小写，部分匹配即可（如 `"ECE2160"` 可以匹配 `"ECE2160JSU2026"`）

### 大模型设置

```json
"llm": {
  "provider": "deepseek",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat"
}
```

默认使用 DeepSeek，如果你想换成其他兼容 OpenAI 接口的模型，改 `base_url` 和 `model` 即可。

### OH/RC 关键词

```json
"oh_keywords": ["Office Hour", "答疑", "OH:", "OH：", "Office Hours:"],
"rc_keywords": ["习题课", "Recitation", "RC:", "RC：", "习题", "RC"]
```

脚本先用这些关键词快速筛选包含 OH/RC 信息的文本，再交给大模型精准提取。你可以按需增减。

### 日历名称

```json
"sync": {
  "calendar_name": "SJTU Canvas",
  "lookahead_days": 60
}
```

改 `calendar_name` 可以自定义日历名称。

---

## 常见问题

**Q: 运行报错 `API token not found`？**
检查 `data/api_token.txt` 是否存在，里面的 token 是否正确。

**Q: 运行报错 `DeepSeek API key not found`？**
检查 `data/api_key_llm.txt` 是否存在，key 是否以 `sk-` 开头。

**Q: 日历里没有出现？**
1. 确认你当前筛选的课程里有 Canvas 内容
2. 查看日志：`cat data/sync.log`
3. 确认日历 App 里 "SJTU Canvas" 日历没有被隐藏

**Q: DeepSeek 扣费太多？**
每次同步约 0.002 元（0.2 分钱），每月约 3 分钱。1 块钱够你用两三年。

**Q: 想手动运行？**
```bash
cd canvas-cal-sync
python3 sync.py
```

---

## 工作原理

```
Canvas API                        DeepSeek LLM                Apple Calendar
    │                                  │                          │
    ├─ 获取课程列表                     │                          │
    ├─ 获取作业 (assignments)           │                          │
    ├─ 获取公告 (announcements) ──────→ 智能提取 RC/OH           │
    ├─ 获取大纲 (syllabus)    ──────→   时间、地点、             │
    ├─ 获取页面 (pages)       ──────→   新增/取消/改期  ───────→ 写入日历事件
    │                                  │                          │
    │                           理解语义:                         │
    │                           "RC shifted from Mon to Wed"      │
    │                           → 删除周一，添加周三               │
    │                           "8:20 PM" → 20:20                │
    │                           过滤噪音: 问卷、链接、群聊         │
```

如果大模型调用失败（如网络问题），脚本会自动降级为正则表达式提取，不会中断同步。

---

## 许可

SJTU Global College
