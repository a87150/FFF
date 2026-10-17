# FFF

一个基于 Flask + FFmpeg 的网页版视频剪辑 / 转码工具。支持本机与局域网访问，可对 NAS 上的视频文件直接操作，无需上传。

**特点：**
- 🖥️ 网页操作，跨平台（Windows / Linux）
- 📂 直接浏览服务器/NAS 文件，不占用浏览器内存
- 🗂️ 文件夹选择器（逐级浏览选择、支持切换盘符，也可以直接输入路径跳转）
- 🔍 目录探查（递归找出目录下所有视频，悬停文件可看相对路径）
- ☑️ 批量多选（按后缀筛选、全选 / 反选、Shift 连选，一键加入批量队列）
- ✂️ 精确剪辑（带视频预览、拖动进度条选起止点）
- 🚀 硬件加速（NVIDIA NVENC / Intel QSV / AMD AMF / VAAPI）
- 🧠 智能推荐参数（根据源码率、编码自动推荐 CRF / preset / GPU）
- 📦 批量处理（批量去片头片尾、批量转码）
- 💾 输出目录收藏、源目录收藏
- ⏱️ 实时进度、日志、中止任务

---

## 📸 界面预览

```
┌───────────────┬──────────┬────────────────────────────┐
│  文件浏览     │ 批量队列 │  剪辑 / 转码参数 / 输入输出 │
│  - 路径跳转   │          │                            │
│  - 选择文件夹 │          │  - 视频预览                │
│  - 探查视频   │          │  - 起始/结束时间           │
│  - 收藏夹     │          │  - 批量片头/片尾裁剪        │
│  - 文件列表   │          │  - 转码方式（含智能）       │
│    + 加入队列 │          │  - 编码格式 / 质量 / preset │
│               │          │  - 输出目录收藏             │
└───────────────┴──────────┴────────────────────────────┘
```

---

## 📁 目录结构

```
ffmpeg-web/
├── app.py                 # 启动入口（Flask app 工厂）
├── config.py              # 全局配置（路径 / 容器兼容表 / 参数白名单）
├── ffmpeg_utils.py        # FFmpeg 工具层（探测 / 命令构建 / 兼容性检查 / 进度换算）
├── tasks.py               # 任务管理（并发控制 / 子进程 / 进度 / 中止）
├── routes.py              # HTTP 路由
├── requirements.txt       # 运行依赖
├── requirements-dev.txt   # 开发依赖（pytest）
├── tests/                 # 单元测试（pytest）
├── run.ps1                # Windows 一键启动脚本
├── run.sh                 # Linux/macOS 一键启动脚本
├── templates/
│   └── index.html         # 页面结构
├── static/
│   ├── style.css          # 样式
│   └── app.js             # 前端逻辑
├── ffmpeg.exe  (Windows)  # FFmpeg 可执行文件（需自行下载）
├── ffprobe.exe (Windows)
├── ffmpeg      (Linux)
└── ffprobe     (Linux)
```

---

## 🚀 部署步骤

### ⚡ 一键启动（推荐）

脚本会自动完成：查找 Python → 创建 `.venv` 虚拟环境 → 安装依赖（首次需联网）→ 检查 ffmpeg → 启动服务。

**Windows（PowerShell）**

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1

# 可选参数：指定端口 / 默认目录
powershell -ExecutionPolicy Bypass -File .\run.ps1 -Port 8080 -MediaDir "D:\Videos"
```

**Linux / macOS**

```bash
chmod +x run.sh    # 首次
./run.sh

# 或指定端口
./run.sh 8080
```

---

### 手动部署（以下为手动步骤）

### 前置：准备 FFmpeg

程序**不包含** FFmpeg，需要你单独下载并放到项目根目录。

**Windows**

1. 打开 https://github.com/BtbN/FFmpeg-Builds/releases
2. 下载 `ffmpeg-master-latest-win64-gpl.zip`
3. 解压后，把 `bin` 目录里的 `ffmpeg.exe`、`ffprobe.exe` 复制到项目根目录

**Linux**

```bash
# 下载静态编译版
cd /tmp
wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar -xf ffmpeg-release-amd64-static.tar.xz

# 复制到项目目录（无后缀）
cp ffmpeg-*-static/ffmpeg  /path/to/ffmpeg-web/
cp ffmpeg-*-static/ffprobe /path/to/ffmpeg-web/
chmod +x /path/to/ffmpeg-web/ffmpeg /path/to/ffmpeg-web/ffprobe
```

程序会自动优先使用同目录下的 ffmpeg，找不到时回退到系统 `PATH`。

---

### 1. 安装 Python 依赖

需要 Python 3.8+。

```bash
cd ffmpeg-web

# 建议用虚拟环境
python3 -m venv venv

# Linux/macOS
source venv/bin/activate

# Windows
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

国内加速：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

### 2. 启动服务

```bash
python app.py
```

看到类似输出说明启动成功：

```
[FFmpeg Web] 系统    = Linux 6.x
[FFmpeg Web] FFMPEG  = /path/to/ffmpeg-web/ffmpeg
[FFmpeg Web] FFPROBE = /path/to/ffmpeg-web/ffprobe
 * Running on http://0.0.0.0:5000
```

浏览器打开 `http://localhost:5000`，或局域网内 `http://你的IP:5000`。

---

### 3. （可选）修改端口和默认目录

用环境变量控制：

```bash
# Linux
export PORT=8080
export MEDIA_DIR=/vol1/1000/Media
export VAAPI_DEVICE=/dev/dri/renderD128   # 仅 VAAPI 需要
export USE_WAITRESS=1                     # 可选：生产服务器（pip install waitress）
python app.py

# Windows PowerShell
$env:PORT="8080"
$env:MEDIA_DIR="D:\Videos"
python app.py
```

默认：
- `PORT=5000`
- `MEDIA_DIR`：Windows 为用户主目录，Linux 为 `/`
- `VAAPI_DEVICE=/dev/dri/renderD128`
- `MAX_CONCURRENT_TASKS=1`：同时运行的转码任务数上限，超出的任务自动排队
- `USE_WAITRESS=1`：用 [waitress](https://github.com/Pylons/waitress) 替代内置开发服务器（需先 `pip install waitress`；未安装时自动回退）

---

### 4. （可选）注册为系统服务

**Linux（systemd）**

```bash
sudo nano /etc/systemd/system/ffmpeg-web.service
```

```ini
[Unit]
Description=FFmpeg Web UI
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/home/your_user/ffmpeg-web
ExecStart=/home/your_user/ffmpeg-web/venv/bin/python /home/your_user/ffmpeg-web/app.py
Restart=on-failure
Environment="MEDIA_DIR=/vol1/1000/Media"
Environment="PORT=5000"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ffmpeg-web
sudo systemctl status ffmpeg-web
```

**Windows（NSSM）**

下载 [NSSM](https://nssm.cc/download)，以管理员运行：

```cmd
nssm install FFmpegWeb "C:\path\to\python.exe" "C:\path\to\app.py"
nssm set FFmpegWeb AppDirectory "C:\path\to\ffmpeg-web"
nssm start FFmpegWeb
```

---

### 5. （可选）运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

覆盖内容：时间/速度解析、容器兼容与音频自动切换、各平台编码参数生成、进度/剩余时间换算、
任务生命周期（完成 / 失败 / 排队 / 中止）。

---

## 📖 使用说明

### 文件浏览与探查

左侧顶部提供三个入口：

- **跳转**：直接输入路径，回车跳到该目录（支持 `D:` 这样的盘符简写）
- **选择**：弹出文件夹选择器，点击文件夹逐级进入（「⬆ 上级」返回上一层）；Windows 下在盘符根目录（如 `C:\`）再点「上级」会进入「此电脑」，可切换到其他盘符；选定后点「选择此文件夹」
- **探查**：递归扫描当前目录下**所有**视频文件（自动跳过隐藏目录/文件），结果直接列出；鼠标悬停在文件上可以看到它的相对路径，便于定位；点顶部「返回浏览」回到普通视图

普通视图和探查视图下都可以：点文件预览、悬停点 `+` 加入批量队列、收藏当前目录（⭐ 收藏夹）。探查结果超过 2000 个文件时会截断显示；鼠标悬停在文件上会显示其相对路径，便于区分同名文件。

列表上方的两个工具区用于**批量挑选文件**：

- **筛选**：按文件后缀只看某类视频（下拉选项随当前列表内容自动生成）
- **多选**：每个文件前的勾选框，配合「全选 / 反选 / 清空」；先勾一个再按住 **Shift** 点另一个可整段连选；点「加入所选 (N)」把勾选的视频一次性加入批量队列

> 提示：筛选只影响显示与全选/反选范围，右侧批量队列里的文件不受影响。

### 单文件转码 / 剪辑

1. **选文件**：左侧浏览，点击视频文件 → 右侧显示信息与预览
2. **选区间**：拖动播放器进度条，在起点处点「⏱ 当前」填起始；在终点处点「⏱ 当前」填结束
3. **选参数**：
   - **不转码（copy）**：只做剪切，速度极快，画质无损
   - **智能选择**：自动分析源文件推荐参数（含 GPU 优先）
   - **手动选平台**：软件编码 / NVENC / QSV / AMF / VAAPI
4. **输出路径**：自动生成。例如
   - copy 剪辑：`video_cut.mp4`
   - H.265 转码：`video_h265.mp4`
   - 剪辑+转码：`video_cut_h265.mp4`
5. **点开始转码**：实时进度（含速度与剩余时间估计）、日志、中止按钮；完成后自动刷新文件列表，可点「📂 输出目录」定位新文件

### 批量处理（推荐场景：批量去片头片尾）

1. **加队列**：左侧文件悬停点 `+`，加入中间「批量队列」
2. **设裁剪**：在「📦 批量裁剪」区域
   - 片头裁剪：如 `00:00:30`
   - 片尾裁剪：如 `00:01:00`
3. **选参数**：转码方式推荐用 **🧠 智能选择**
4. **点开始转码**：队列里的文件会**顺序处理**，每个文件独立计算起止时间（失败/中止的项可点「重试失败」重置后重跑）

### 智能选择的工作方式

每个文件单独分析：

| 源文件情况 | 智能选择的参数 |
| :--- | :--- |
| 源是老编码（mpeg4/wmv/mpeg2 等） | CPU + H.265 + 推荐 CRF |
| 源是现代编码（hevc/av1/vp9） | 目标码率模式（避免重编码膨胀） |
| 有 GPU 可用 | 优先 GPU（CQ/ICQP） |
| 无 GPU | 回退 CPU |

日志会明确显示每个文件选了什么参数。

---

## 🎛️ 参数说明

### 转码方式

| 选项 | 说明 |
| :--- | :--- |
| **copy** | 不重新编码，只剪切或改容器，最快 |
| **智能选择** | 按每个源自动分析 |
| **软件编码** | CPU (libx264 / libx265 / libsvtav1)，画质最佳但慢 |
| **NVIDIA NVENC** | RTX/GTX 显卡硬件编码，快 5-15 倍 |
| **Intel QSV** | Intel 核显/Arc 编码，低功耗 |
| **AMD AMF** | AMD 显卡硬件编码（Windows 支持较好） |
| **VAAPI** | Linux 通用硬件加速接口 |

### 质量参数

| 编码器 | 参数 | 推荐值 |
| :--- | :--- | :--- |
| libx264 / libx265 | CRF | 18-28（越小越好） |
| NVENC | CQ | 20-28 |
| QSV | ICQ | 22-28 |
| AMF / VAAPI | QP | 22-26 |

### Preset

- **软件编码**：`ultrafast` ~ `veryslow`（越慢压缩率越高）
- **NVENC**：`p1` ~ `p7`（数字越大质量越好）
- **QSV**：`veryfast` ~ `slower`
- **AMF**：`speed` / `balanced` / `quality`
- **VAAPI**：无 preset 概念（界面中不可选）

### 音频

| 容器 | 兼容的音频编码 |
| :--- | :--- |
| MP4 | AAC / MP3 / AC3 / EAC3 / ALAC / Opus |
| WebM | Opus / Vorbis |
| MKV | 几乎全部 |

**注意**：源音频不兼容目标容器时（如 WMA → MP4），程序会**自动改为 AAC 重编码**，日志会提示。

---

## 🧠 推荐参数速查

| 源码率 | H.265 CRF 建议 |
| :--- | :--- |
| < 800 kbps | 30 |
| 800 - 1500 | 29 |
| 1500 - 2500 | 28 |
| 2500 - 4000 | 27 |
| 4000 - 7000 | 25 |
| 7000 - 12000 | 23 |
| > 12000 | 21 |

**为什么有时 CRF 高文件还变大？**

CRF 是"目标质量"，不是"目标大小"。如果源文件本身用低效编码压出来的（如老 mpeg4 / wmv），你用现代编码器去"忠实还原"它，反而需要更多码率。**要变小，就要用目标码率模式**，直接锁死输出码率。

---

## ❓ 常见问题

### Q1：启动后显示「未找到 FFmpeg」

检查项目根目录有没有 `ffmpeg.exe` / `ffmpeg`（Linux 无后缀），并确认有执行权限：

```bash
chmod +x ffmpeg ffprobe
```

### Q2：转码失败，日志显示 "codec not currently supported in container"

音频/视频编码不兼容目标容器。程序已处理常见音频情况，视频不兼容时需要重新编码（不要选 copy）。

### Q3：GPU 加速不可用

- **NVIDIA**：需要驱动 + FFmpeg 编译时带 `--enable-nvenc`
- **QSV**：需要 Intel 核显 + FFmpeg 带 `--enable-libmfx`
- **VAAPI**：需要 `/dev/dri/renderD128` 设备 + 用户加入 `render` 组：
  ```bash
  sudo usermod -aG render $USER
  # 重新登录生效
  ```

可用以下命令检查：

```bash
ffmpeg -hide_banner -encoders | grep -E "nvenc|qsv|amf|vaapi"
```

### Q4：浏览器预览某些视频无画面

浏览器不支持所有编码格式（如 HEVC、ProRes）。**不影响转码**，只是预览看不了。可以：

- 改用 Chrome / Edge 最新版（部分支持 HEVC 硬件解码）
- 或将源文件转成 H.264 后再操作

### Q5：输出文件没变化 / 提示"输出路径不能和输入相同"

检查输出路径是否和源文件在同一位置且同名。程序已做保护，避免覆盖源文件。如果输出文件已存在，会弹窗让你确认是否覆盖。

### Q6：批量处理能并行吗？

前端批量任务是**顺序处理**（一个完成再下一个），避免同时占用过多 CPU / GPU。服务端同时运行的任务数由 `MAX_CONCURRENT_TASKS` 环境变量控制（默认 1，多个来源的提交会自动排队，任务状态显示为「排队中」）。

### Q7：CRF 22 转出来很小，正常吗？

正常。说明源文件冗余度高（如蓝光原盘、H.264 高码率）。压缩到源的 30-50% 是常见现象。如果担心画质，改 CRF 到 18-20 再压一遍对比。

---

## 🔒 安全提示

本工具可以**浏览服务器任意目录**、**读写任意文件**。请务必：

- 仅在**内网/局域网**使用
- **不要暴露到公网**（如需远程访问，用 VPN 或 SSH 隧道）
- 如果必须公网访问，请在反向代理（Nginx/Caddy）前加 HTTP Basic Auth：
  ```nginx
  location / {
      auth_basic "Restricted";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:5000;
  }
  ```

---

## 🛠️ 技术栈

- **后端**：Python 3.8+ / Flask
- **前端**：原生 HTML / CSS / JavaScript
- **核心**：FFmpeg / FFprobe

---

## 📜 许可

仅供个人学习与使用。FFmpeg 遵循 LGPL/GPL，本项目仅调用其命令行，请遵守对应许可。

---

## 🙏 致谢

- [FFmpeg](https://ffmpeg.org/)
- [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)
- [johnvansickle.com](https://johnvansickle.com/ffmpeg/)（Linux 静态版）