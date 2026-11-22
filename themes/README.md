# 主题

IrisAI 自带一个默认主题（写在 `hub/static/css/app.css` 里）。别的主题都放在这个文件夹，每个主题一个子文件夹，放进来刷新页面，就能在“设置 → 外观”里选。

主题文件夹的位置可以改：环境变量 `HUB_THEMES` 优先，其次是 `config/hub.json` 的 `themes_dir`（相对路径按项目根目录算），都没有就用这里。

这个文件夹除了本说明都不进代码仓库：主题里的立绘、桌宠常用别人的素材，许可多半不允许再分发。分享主题时，请先看清素材的条款。

## 最少要有什么

```
themes/
  my-theme/            文件夹名就是主题 id，只能用字母、数字、下划线、减号
    skin.json
    skin.css
```

`skin.json`：

```json
{ "name": "我的主题", "accent": "#6fb488", "persona": "某个人格" }
```

| 字段 | 作用 |
|---|---|
| `name` | 设置里显示的名字 |
| `accent` | 选这个主题时建议的强调色（用户可以再改） |
| `persona` | 可选。桌宠台词用哪个人格：读 `knowledge/人格/<persona>/桌宠台词.json`，没有就用通用台词 |

`skin.css`：只在选了这个主题时生效，所有规则都写在 `:root[data-skin=<主题 id>]` 下面，例如

```css
:root[data-skin=my-theme] { --deco: #6fb488; }
:root[data-skin=my-theme] .p-hero { background: ... }
```

可以覆盖 `app.css` 里的颜色、圆角等 CSS 变量，也可以改任何部件的样式。`--deco` 是装饰色，桌面桌宠戳她冒出的爱心也用这个颜色。

## 可选的文件

| 文件 | 用在哪里 |
|---|---|
| `icon.png` | 浏览器标签页图标、登录页图标 |
| `icon.ico` | 桌面程序的窗口和托盘图标（换主题后重开窗口生效） |
| `素材/立绘.*` | 入口页右栏的大立绘。透明底，高 2000 像素左右 |
| `素材/横幅.*` | 入口页顶部横幅的背景，宽图，主体放右半边 |
| `素材/看板.*` | 工作台侧栏下方、预览区空白处的看板娘；有它才会出现桌宠 |
| `素材/看板_思考.*`、`_出错`、`_完成`、`_空闲` | 看板娘按 Agent 状态换的表情（配合 `skin.css` 里的 `data-mood`） |
| `素材/背景.*` | 整个页面的空白处，很淡地铺一层 |

图片格式可以是 png、jpg、webp、gif、avif，同一个位置只认第一张。主题文件挂在 `/s/skins/<主题 id>/` 下，局域网里不登录也能打开。

### 立绘换表情

`素材/立绘差分/<表情>.webp` 加一份 `清单.json`：

```json
{ "size": [1211, 2000], "default": "微笑", "faces": ["微笑", "笑脸", "惊讶"],
  "marks": [{ "name": "心形", "left": 17.1, "top": -0.9, "width": 60.2 }] }
```

每张差分和 `立绘.*` 同样大小、同一个裁剪框，换表情时盖在上面淡入淡出。`marks` 是情绪符号（`立绘差分/符号/<名字>.webp`），位置按占立绘宽高的百分比。什么情况用哪个表情，在入口页“调整布局 → 表情…”里改。

`素材/立绘部件/部件.json` 可以让立绘按部件一层层叠出来（眼、口、眉各选一个），格式同下面的桌宠部件。

### 桌宠

`素材/桌宠.json` 是帧清单，帧都是同样大小、同一个裁切框的透明底 png：

```json
{ "size": 512, "frames": { "idle": "桌宠/idle.png", "blink1": "桌宠/blink1.png", "talk1": "桌宠/talk1.png" } }
```

`idle` 必须有，缺的帧都用 `idle`。会用到的帧名：idle、blink1、blink2、talk1、talk2、think、done、error、sleep1、sleep2、yawn、wave、surprise、held、dizzy、pout、wink、idea、hum、shy、cheer、hope。什么情况用哪一帧，在“调整布局 → 表情… → 桌宠”里改。

`素材/桌宠部件/部件.json` 加上按图层编号的 png，就能在“编辑表情”里按部件拼新的帧：

```json
{
  "size": 512, "outline": 0,
  "parts": { "体/身体1": { "file": "002.png", "x": 140, "y": 192, "z": 2 } },
  "fixed": ["后发/后发"],
  "cats": { "body": { "name": "身体", "multi": false, "options": [{ "id": "体/身体1", "label": "身体 1" }] } },
  "presets": { "idle": { "body": "体/身体1", "eye": "目/普通", "mouth": "口/微笑", "brow": "眉/平常", "cheek": [] } },
  "links": {}
}
```

`parts` 每个部件一张图（`x`、`y` 是在帧里的位置，`z` 越大越靠前）；`fixed` 总是画的部件；`cats` 是可选的类：
body、eye、mouth、brow 单选，eyeopt、mouthopt、cheek、opt 多选；`presets` 是每一帧原本的配方。
眨眼、说话的帧跟着 idle 走，sleep2 跟着 sleep1 走：只改平常的脸，眨眼和说话也会换成新的脸。
一份 PSD 的拆法可以参考项目里 `hub/app/petparts.py` 的说明。
