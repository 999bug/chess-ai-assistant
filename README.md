# 象棋 AI 助手

对着桌面上的象棋程序截图，识别当前棋局，用象棋引擎算出下一手，再用置顶浮窗把走法摆在你眼前。

只做「看屏幕 + 给建议」：**不注入游戏进程、不修改客户端、不自动落子**。

最初针对 **JJ象棋**（跑在腾讯应用宝里）开发。识别链路没有依赖特定程序的私有实现，
只要能稳定截到棋盘，别的桌面象棋程序同样能接。

## 特性

- **整盘识别**：棋盘四点定位 → 透视拉正 → 一次判出 10×9×16 的完整局面，
  结构上保证 90 格全有输出，不会出现「漏认一个子整个局面就错」。
- **守门员**：静态校验（将帅照面、子力超编、士象位置）+ 相邻帧着法差分。
  认不准时明确告警，不硬凑一个看起来合理的答案。
- **引擎常驻**：Pikafish（UCI 协议）长驻进程，你的每步棋只触发一次搜索，不重启引擎。
- **中文记谱**：引擎输出的坐标走法翻译成「炮二平五」这类中文记法。
- **独立浮窗**：识别进程与 UI 进程分离，浮窗可拖动、可置顶，Esc 退出即整体收尾。

## 识别准确率

对照 `tests/gt/recognition_gt.json`（人工逐格核对过的真值）跑 `python tools/eval_recognition.py`：

| 方案 | 逐格准确率 | 子力召回 | 幻影子 | 整盘全对 | 单帧耗时 |
|---|---|---|---|---|---|
| 旧：逐格模板匹配 | 93.9% | 85.2% (52/61) | 2 | 0/2 | 114ms |
| 新：整板 ONNX 分类 | **100%** | **100%** (61/61) | **0** | **2/2** | 628ms（含首次加载，热身后约 250ms） |

逐格准确率天然偏高（90 格里大半是空位），**要看子力召回和整盘全对率**——
引擎要的是整盘正确，不是 98% 正确，错一个子就可能给出荒唐建议。

## 快速开始

### 环境要求

- Windows 10 / 11：截图走 `mss`，窗口枚举与还原走 Win32 API
- Python 3.10 或更新
- 目标象棋程序正常显示在桌面上（不要求前台，但窗口不能最小化）

### 1. 安装依赖

```bash
python -m pip install opencv-python numpy mss onnxruntime
```

需要跑界面文字 OCR（`tools/ocrutil.py` / `tools/notation.py`）时再加 `rapidocr_onnxruntime`。

### 2. 下载引擎（不入库）

```bash
gh release download -R official-pikafish/Pikafish -D engine
```

解压下载到的 `.7z`，把 Windows 版可执行文件和 `pikafish.nnue` 放进 `engine/`，
可执行文件改名为 `pikafish.exe`（`app/coach.py` 按这个名字找）。

### 3. 下载识别模型（不入库）

```bash
python tools/download_models.py
```

约 31MB，来自 HuggingFace Space；直连不通时脚本会自动换 hf-mirror 镜像。
模型是 TheOne1006/chinese-chess-recognition 与 cheese2020/Chinese-Chess-Recognition
共用的预训练权重（Apache-2.0）。

### 4. 标定棋盘坐标

把目标程序摆到屏幕上，然后：

```bash
python tools/calibrate.py            # 默认找标题含「JJ象棋」的窗口
python tools/calibrate.py --show     # 顺带输出交叉点可视化图，方便肉眼验收
python tools/calibrate.py --title 你的窗口标题
```

结果写进 `config/layout.json`，坐标全部相对窗口左上角，窗口缩放时按比例换算。
仓库里带了一份 326×606 窗口下的标定值，**窗口尺寸不同必须重新标定**。

### 5. 跑起来

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

`start.ps1` 会依次：清理上次残留进程 → 检查引擎与模型是否就位 →
自动挑出装了识别依赖的 Python 和带 tkinter 的 Python → 启动识别进程 + 置顶浮窗。
关掉浮窗窗口即自动停止全部进程。（Windows PowerShell 5.1 和 PowerShell 7 都支持。）

**两个 Python 可能不是同一个。** 识别依赖（`cv2` / `numpy` / `mss` / `onnxruntime`）
常装在独立虚拟环境里，而这类环境多为 python-build-standalone 构建，**不带 tcl/tk**，
所以浮窗得退到系统里自带的 CPython（如 `Python310`）。`start.ps1` 会自动探测，
找不到时会在报错里逐个列出解释器缺什么；也能手工硬指定：

```powershell
$env:JJCHESS_ID_PY = "D:\py-venv\Scripts\python.exe"   # 识别 + 引擎
$env:JJCHESS_UI_PY = "C:\Python310\python.exe"         # 浮窗（必须带 tkinter）
```

设了就以它为准，不达标会直接报错，不会偷偷换一个解释器。

也可以手动开两个终端：

```bash
python app/auto_coach.py --side red     # 识别 + 引擎
python app/hud.py                       # 浮窗
```

## 命令行

`app/` 下是运行时核心，`tools/` 下是辅助与一次性脚本。

| 脚本 | 说明 |
|---|---|
| `app/auto_coach.py` | 主循环：截图 → 识别 → 校验 → 出招，结果写 `out/suggestion.json` |
| `app/hud.py` | 置顶浮窗，读 `out/suggestion.json` |
| `app/coach.py` | 单次抓图出建议（调试用），也提供引擎封装 |
| `tools/eval_recognition.py` | 识别准确率评测 |
| `tools/calibrate.py` | 棋盘坐标标定 |
| `tools/download_models.py` | 拉取 ONNX 权重 |
| `tools/probe.py` | 窗口枚举与截图方式的探测工具 |
| `tools/watch_board.py` | 早期实现：实时整盘监测（霍夫圆，已被 `auto_coach` 取代） |
| `tools/board_read.py` | 早期实现：霍夫圆 + OCR 读盘 |

常用参数：

```bash
python app/auto_coach.py --side black --movetime 1500     # 引擎想更强
python app/auto_coach.py --turn black                     # 中途接手，进场时轮黑方走
python app/auto_coach.py --backend template               # 回退到旧的模板匹配后端
python tools/eval_recognition.py --backend both --verbose
python tools/download_models.py --force
```

**中途接手也能用**：对局打到一半才把引擎打开时，"现在轮到谁走"是它必须知道
的第一件事（JJ象棋 界面上没有这个提示，原来只能假定「轮我方」）。现在进场会先推断：
被将军的局面判得出来（轮到我走时对方不可能正被将军，反之亦然），平静局面判不出来
就先按「我方走」算**并在浮窗提示**——这种时候用 `--turn red|black` 直接告诉它最准。
就算当时猜错了也会自己纠正：下一帧只要看出某一步是谁走的，轮次对不上就翻过来重排。

**运行期参数在 `config/tune.json`**——改了保存即生效，不用重启进程
（引擎的 Hash/Threads 走 `setoption` 热改，思考时间本来就是每步 `go` 的参数）。
字段含义、取值范围和推荐值见 [docs/CONFIG.md](docs/CONFIG.md)。
`start.ps1` 的同名参数只是启动时覆盖一次，日常调整直接改文件更省事。

## 目录结构

```
start.ps1             一键启动（识别进程 + 置顶浮窗）

app/                  运行时核心
  auto_coach.py         主循环：抓图 → 识别 → 校验 → 出招
  coach.py              引擎封装（UCI）+ FEN + 中文记谱
  applog.py             日志（写根目录 log/，按天切分）
  rules.py              规则校验：静态局面 + 相邻帧着法差分
  board_onnx.py         整板识别适配层（四点拉正 → ONNX 推理）
  grid_classify.py      旧的逐格模板匹配后端（--backend template 回退用）
  hud.py                置顶浮窗

tools/                辅助与一次性脚本
  calibrate.py          棋盘坐标标定（产出 config/layout.json）
  probe.py              窗口枚举与截图方式探测
  eval_recognition.py   识别准确率评测
  download_models.py    拉取 ONNX 权重
  board_read.py         早期实现：霍夫圆 + OCR 读盘
  grid_read.py          早期实现：逐格模板分类
  watch_board.py        早期实现：实时整盘监测（霍夫圆）
  watch_notation.py     棋谱区实时检测（OCR）
  notation.py           中文记谱解析
  ocrutil.py            RapidOCR 封装

config/
  layout.json           棋盘坐标（标定产物，入库）
  tune.json             运行期参数（入库，说明见 docs/CONFIG.md）

docs/
  PROGRESS.md           开发进度、实测数据与设计取舍
  CONFIG.md             tune.json 字段说明
  index.html            执红开局武器库（单页展示）

tests/
  run_all.py            一次跑完全部测试
  gt/                   人工核对过的识别真值

engine/               引擎二进制与 NNUE（自行下载，不入库）
models/               ONNX 权重（自行下载，不入库）
out/                  运行产物：截图、建议（不入库）
log/                  运行日志（不入库，只留一份 README 说明）
```

## 日志

运行时的问题都写在 `log/` 下，按天切分：

| 文件 | 内容 |
|---|---|
| `log/auto_coach-YYYY-MM-DD.log` | 主循环：状态变迁、校验没过的完整问题列表和 FEN、差分异常、引擎异常与重启 |
| `log/hud-YYYY-MM-DD.log` | 浮窗进程 |
| `log/coach.out.log` / `coach.err.log` | `start.ps1` 重定向的 stdout / stderr |

默认保留 14 天（下次打开日志文件时顺手清理）。要看更啰嗦的内容，
启动前设 `JJCHESS_LOG_LEVEL=debug`；日志目录也能用 `JJCHESS_LOG_DIR` 换掉。

排查「引擎没给着法」这类问题时，日志里的 `reason` 是分项的：
无着可走（绝杀/困毙）、引擎拒收局面、超时、进程已退出——四种的处理方式完全不同，
所以别只看界面上的那句话。

## 设计取舍

- **放弃了「以认字为核心」的路线**。18px 的 车/卒、士/仕 本来就难分，
  这条路走不通；改成整板一次分类后，代价是依赖一个 31MB 的模型，换来的是整盘正确率。
- **规则层只做必要条件校验，不做可达性搜索**。一个 22 子的残盘
  （红方缺 2 车 2 马 2 炮）物理上完全可达，静态校验**必须**放行。
  拦这类错误只能靠更好的识别器 + 开局帧严格比对，加规则硬拦会误杀真实局面。
- **识别预处理契约不要改**：拉正尺寸、裁剪、归一化参数、类别顺序，
  任何一项变了精度都会直接掉。参数表见 `docs/PROGRESS.md`。
- **首次稳定盘面时严格比对标准开局**，这是唯一能拦住「自信的错答案」的时机。
  但因为 app 存在非标准开局模式，这里只告警不硬拦。
- **引擎失联要按原因分开处理**（2026-09-29 实测）。pikafish 碰到「轮到我走、
  而我方已经能吃对方的将」这种局面时不是拒招，而是打印一行
  `CRITICAL ERROR: ... King can be captured` 之后**自己退出**——
  于是后面每一问都失败，日志上却只剩一串「引擎没给着法」。
  现在两条都堵上了：这种局面在提问前就被拦下；真失联了会区分
  「无着可走 / 拒收局面 / 超时 / 进程已退出」，只有后两种值得重试，
  而进程没了必须重启，否则管道再也回不来。

## 已知限制

- 棋盘坐标是一次性标定写死的，没有逐帧复核。实测格点与棋子圆心有
  dx +2~+7px 的系统偏差；整板分类对这个量级不敏感，但窗口缩放较大时会退化。
- 真值只有 2 帧，中局、残局、走子动画帧的覆盖不足。
- 棋谱区坐标尚未标定，`tools/notation.py` 的中文记谱解析暂时没有输入源。
- 只给建议，不自动落子。
- 目标窗口必须持续可见，最小化或容器失焦后取不到画面。

## 第三方组件

| 组件 | 用途 | 许可 |
|---|---|---|
| [Pikafish](https://github.com/official-pikafish/Pikafish) | 象棋引擎 | GPL-3.0，本项目不分发其二进制 |
| 整板识别 ONNX 权重 | 棋子分类 | Apache-2.0，出处见 `tools/download_models.py` |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | 界面文字 OCR | Apache-2.0 |

本项目仅用于学习与自我复盘。请自行确认目标程序的服务条款。
