# Skill: JSXGraph 交互式几何绘图

当用户要求绘制交互式几何图形时（如"画出来""帮我演示一下""做个图看看"），
按以下步骤生成 `.jsxgraph` 文件并创建分享链接。

---

## 工作流程

```
用户发图片/描述 → 分析数学内容 → 生成 JSXGraph 代码 → 保存 .jsxgraph 文件 → 分享链接
```

## 使用方式

### 保存文件

用 VFS `echo` 或 `cat` 命令将 JSXGraph 代码写入 `workspace/xxx.jsxgraph`。
**只写入 JSXGraph 的 JavaScript 代码，不含 HTML 标签和 script 标签。**

### 分享链接

保存后调用 `create_share_link` 工具生成分享链接发给用户。

---

## 画板初始化

每个 `.jsxgraph` 文件必须以 `JXG.JSXGraph.initBoard` 开始（JSXGraph 库已自托管在 FeClaw 静态服务中）：

```javascript
var board = JXG.JSXGraph.initBoard('jxgbox', {
    boundingbox: [-5, 5, 5, -5],  // [xmin, ymax, xmax, ymin]
    axis: true,                    // 显示坐标轴
    grid: false,                   // 可选：显示网格
    pan: {needTwoFingers: true},   // 双指平移
    zoom: {factor: 1.25}           // 滚轮缩放
});
```

### 推荐配置

```javascript
board.options.label.autoPosition = true;  // 标签自动避让
board.options.point.size = 1;             // 点大小
```

### boundingbox 选择原则

- 几何图形：留出 20% 边距，`[-4, 3, 4, -3]`
- 函数图像：根据函数值域调整，`[-10, 10, 10, -10]` 或更宽
- 三角函数：`[-7, 3, 7, -3]`
- 窄图精细：`[-3, 4, 3, -4]`

---

## 元素创建（核心 API）

所有元素用 `board.create(type, parents, attributes)` 创建。

### 点

```javascript
var A = board.create('point', [1, 2], {name: 'A', color: 'blue', size: 4});
var B = board.create('point', [-3, 1], {name: 'B', color: 'red', size: 3});
var C = board.create('point', [0, 0], {name: 'C', visible: false});   // 隐藏点
```

### 线 / 线段 / 射线

```javascript
var line1 = board.create('line', [A, B]);           // 直线
var seg1 = board.create('segment', [A, B]);          // 线段
var ray1 = board.create('ray', [A, B]);              // 射线
var tan = board.create('tangent', [A, curve]);       // 切线
```

### 多边形

```javascript
var tri = board.create('polygon', [A, B, C], {color: 'lightblue', borders: {strokeColor: 'blue'}});
var rect = board.create('polygon', [A, B, C, D]);     // 四边形
var regHex = board.create('regularpolygon', [A, B, 6]);  // 正六边形（A B 为相邻顶点）
```

### 圆 / 圆弧

```javascript
var circ = board.create('circle', [center, point]);       // 圆心+圆周一点
var circ2 = board.create('circle', [center, radius]);     // 圆心+半径值
var arc = board.create('arc', [A, B, C]);                // 以 A 为圆心，B 到 C 的弧
```

### 曲线 / 函数图像

```javascript
var f = board.create('functiongraph', [
    function(x) { return Math.sin(x); },
    -7, 7
], {strokeColor: 'red', strokeWidth: 2});

var f2 = board.create('functiongraph', [
    function(x) { return x * x; },
    -3, 3
]);

var curve = board.create('curve', [
    function(t) { return 2 * Math.cos(t); },   // x(t)
    function(t) { return 3 * Math.sin(t); },   // y(t)
    0, 2 * Math.PI
]);
```

### 特殊几何构造

```javascript
var M = board.create('midpoint', [A, B], {name: 'M'});          // 中点
var G = board.create('circumcenter', [A, B, C], {name: 'Q'});   // 外心
var I = board.create('incenter', [A, B, C], {name: 'I'});       // 内心
var H = board.create('orthocenter', [A, B, C], {name: 'H'});    // 垂心
var L = board.create('line', [M, N], {straightFirst: false, straightLast: false});  // 线段式连线
var cross = board.create('intersection', [line1, circ, 0]);     // 交点（0 或 1 选哪个交点）
```

### 角度 / 角度标记

```javascript
var angle = board.create('angle', [B, A, C], {    // ∠BAC
    radius: 0.5,
    color: 'red',
    fillColor: 'yellow',
    fillOpacity: 0.3,
    name: 'α'
});

var rightAngle = board.create('angle', [B, A, C], {
    radius: 0.4,
    orthoType: 'sect',    // 直角：正方形标记
    fillOpacity: 0,
    name: ''
});
```

### 文本 / 标签

```javascript
board.create('text', [1, 2, 'Hello']);                   // 静态文本
board.create('text', [-3, -3, function() {               // 动态文本（值实时更新）
    return '角度 = ' + (angle.Value() * 180 / Math.PI).toFixed(1) + '°';
}]);
```

---

## 常用属性

### 颜色
- `'orange'`, `'red'`, `'blue'`, `'green'`, `'purple'`, `'pink'`, `'gray'`

### 点属性
```javascript
{name: 'A', color: 'blue', size: 4, face: 'circle', /* 或 'cross', 'x', 'diamond', 'square' */}
```

### 线属性
```javascript
{strokeColor: 'black', strokeWidth: 2, strokeDash: 0, /* 0=实线, 2=虚线, 4=点线 */}
```

### 面属性（多边形/圆）
```javascript
{color: 'lightblue', fillOpacity: 0.3}
```

### 标签属性
```javascript
{name: 'A', withLabel: true, label: {offset: [15, -10]}}
```

---

## 典型示例

### 示例 1：三角形 + 外接圆

```javascript
var board = JXG.JSXGraph.initBoard('jxgbox', {boundingbox: [-4, 4, 4, -4], axis: true});
board.options.label.autoPosition = true;
board.options.point.size = 2;

var A = board.create('point', [-2, -1], {name: 'A'});
var B = board.create('point', [3, -2], {name: 'B'});
var C = board.create('point', [1, 3], {name: 'C'});
var tri = board.create('polygon', [A, B, C]);
var O = board.create('circumcenter', [A, B, C], {name: 'O', color: 'red'});
var circum = board.create('circle', [O, A], {strokeColor: 'red', strokeWidth: 1});
```

### 示例 2：函数图像 + 切线

```javascript
var board = JXG.JSXGraph.initBoard('jxgbox', {boundingbox: [-5, 5, 5, -5], axis: true});
board.options.label.autoPosition = true;

var f = board.create('functiongraph', [function(x) { return 0.2 * x * x; }, -5, 5]);
var P = board.create('glider', [1, 0.2, f], {name: 'P', color: 'red', size: 4});
var tan = board.create('tangent', [P], {strokeColor: 'green', strokeWidth: 2});
```

### 示例 3：正六边形 + 角度（用户示例）

```javascript
var board = JXG.JSXGraph.initBoard('jxgbox', {boundingbox: [-4, 3, 4, -3]});
board.options.label.autoPosition = true;
board.options.point.size = 1;

var A = board.create('point', [-1.2, -2], {color: 'orange', size: 4});
var B = board.create('point', [0.25, -0.5], {color: 'orange', size: 4});
var hexagon = board.create('regularpolygon', [A, B, 6]);
var D = hexagon.vertices[3];
var Q = board.create('circumcenter', [A, B, D], {name: 'Q'});

var G = board.create('point', [3, -2], {name: 'G', color: 'orange', size: 4});
var rtr = board.create('regularpolygon', [B, G, 3]);
var H = rtr.vertices[2];
var R = board.create('circumcenter', [B, G, H], {name: 'R'});

var tr = board.create('polygon', [A, G, B], {color: 'pink'});
var P = board.create('midpoint', [A, G], {name: 'P'});
var q = board.create('line', [P, Q], {name: 'q', withLabel: true});
var r = board.create('line', [P, R], {name: 'r', withLabel: true});

var angle = board.create('angle', [R, P, Q], {
    radius: 0.4, color: 'red', fillOpacity: 0, name: 'φ'
});
board.create('text', [-3, -3, function() {
    return 'θ₁ = ' + (angle.Value() * 180 / Math.PI).toFixed(1) + '°';
}]);
```

---

## 注意事项

1. **只写 JS 代码**，不包含 HTML、`<script>` 标签、`<!DOCTYPE>` 等
2. **所有 board 变量名必须用 `var` 声明**
3. **始终以 `initBoard` 开头**，变量名必须为 `board`
4. **color 用英文名**（orange, red, blue, green, purple, pink, gray, black）
5. 数学运算用 JavaScript `Math.*`：`Math.sin()`, `Math.cos()`, `Math.PI`, `Math.sqrt()`
6. 角度值用弧度，转角度：`angle.Value() * 180 / Math.PI`
7. **交互性优先**：用 `glider`（滑动点）让学生拖拽观察变化
8. 避免创建大量元素（> 200 个）以免浏览器卡顿
9. 复杂图形分步骤画，用注释说明每一步
