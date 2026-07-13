# Curio 画布实现方案（RN Skia）

> 版本：v1.0.0
> 最后更新：2026-07-10
> 状态：工程实现 spec
> 设计文档：`docs/v1/02-curio.md` §7

---

## 1. 渲染架构

### 1.1 三层画布

```
渲染管线（运行时）：

┌─────────────────────────────────────────┐
│  Overlay Canvas（透明）                   │
│  ← 用户当前正在画的笔划                   │
│  ← 笔尖抬起 → 转入 Persistent Canvas     │
├─────────────────────────────────────────┤
│  Persistent Canvas（已提交笔划）           │
│  ← 所有 ink 笔划已渲染（source-over）     │
│  ← 所有 eraser 笔划已应用（destination-out）│
│  ← 增量更新：只重绘有改动的区域            │
├─────────────────────────────────────────┤
│  Image Layer（照片/PDF/图片元素）          │
│  ← 所有 image 元素渲染在此                 │
│  ← 笔划可画在 image 之上                  │
└─────────────────────────────────────────┘
```

**冷启动时：**
- 首先在 Persistent Canvas 位置显示缩略图（WebP base64）
- Overlay Canvas 立即激活——用户可立刻书写
- 后台从 strokes[0] 开始重建 Persistent Canvas
- 重建完成后，缩略图替换为 Persistent Canvas
- Overlay Canvas 叠在上面（用户新笔画始终可见）

### 1.2 坐标转换

```typescript
// 文档坐标系 ↔ 屏幕坐标系
// 文档坐标 = Int32（锚定在创建时的设备分辨率 × 4）
// 屏幕坐标 = Float32（缩放和平移后的当前视口）

interface Viewport {
  offsetX: number;    // 平移偏移（文档坐标）
  offsetY: number;
  scale: number;      // 0.25 ~ 4.0
}

function docToScreen(docX: number, docY: number, vp: Viewport): { x: number, y: number } {
  return {
    x: (docX - vp.offsetX) * vp.scale,
    y: (docY - vp.offsetY) * vp.scale,
  };
}

function screenToDoc(screenX: number, screenY: number, vp: Viewport): { x: number, y: number } {
  return {
    x: screenX / vp.scale + vp.offsetX,
    y: screenY / vp.scale + vp.offsetY,
  };
}
```

---

## 2. Stroke 渲染

### 2.1 单笔渲染流程

```
输入：stroke（ink 或 eraser）
   ↓
对每条 curve：
  ① de Casteljau 等距细分（控制点间距 > 2px 时细分）
  ② 沿法线方向偏移顶点（parallel transport frame）
     effective_width = style.width × (0.1 + 0.9 × sample.pressure)
  ③ 偏移后的左右顶点连接成多边形
  ④ 如果 endpoint：cap = tapered（起/收笔渐变到 minWidth=10%）
  ⑤ 自相交检测 → 改用 fill("evenodd")
  ⑥ fill 到 Canvas
```

### 2.2 代码骨架

```typescript
interface Renderer {
  // 增量添加一笔（不遍历历史）
  addStroke(stroke: Stroke): void;

  // 删除一笔（撤销）
  removeStroke(strokeId: string): void;

  // 设置当前显示范围
  setViewport(vp: Viewport): void;

  // 冷启动
  initFromStrokes(strokes: Stroke[], thumbnail: string): Promise<void>;

  // 导出
  exportSVG(strokes: Stroke[]): string;
}
```

### 2.3 RN Skia 集成

```typescript
import { Canvas, Path, Skia, useCanvasRef } from "@shopify/react-native-skia";

// 每笔 stroke 渲染为一个 Skia Path 对象
function strokeToSkPath(stroke: Stroke): SkPath {
  const path = Skia.Path.Make();
  let first = true;
  for (const curve of stroke.curves) {
    const [x0, y0] = curve.p0;
    const [x1, y1] = curve.p1;
    const [x2, y2] = curve.p2;
    const [x3, y3] = curve.p3;
    if (first) {
      path.moveTo(x0, y0);
      first = false;
    }
    path.cubicTo(x1, y1, x2, y2, x3, y3);
  }
  return path;
}

function renderStrokeToCanvas(canvas: SkCanvas, stroke: Stroke, vp: Viewport) {
  const paint = Skia.Paint();
  if (stroke.type === "ink") {
    paint.setColor(Skia.Color(stroke.style.color));
    paint.setStrokeWidth(stroke.style.width * vp.scale);
    paint.setStyle(PaintStyle.Stroke);
    paint.setAntiAlias(true);
    paint.setStrokeCap(StrokeCap.Round);
    paint.setStrokeJoin(StrokeJoin.Round);

    // 轮廓填充：
    // 1. 细分曲线 → 分段
    // 2. 每段计算法线偏移（pressure 驱动宽度）
    // 3. 连接成闭合多边形
    // 4. fill("evenodd")
    const contourPath = buildContourPath(stroke, vp);
    canvas.drawPath(contourPath, paint);

  } else if (stroke.type === "eraser") {
    // destination-out
    canvas.save();
    const eraserPath = strokeToSkPath(stroke);
    paint.setBlendMode(BlendMode.DstOut);
    paint.setStrokeWidth(stroke.eraser_width * vp.scale);
    paint.setStyle(PaintStyle.Stroke);
    canvas.drawPath(eraserPath, paint);
    canvas.restore();
  }
}
```

---

## 3. 冷启动

### 3.1 缩略图生成

```typescript
// 每次保存画布时（5s 防抖）
async function generateThumbnail(canvas: SkCanvas): Promise<string> {
  // 1. 创建一个小尺寸离屏画布（如 256×256）
  // 2. 缩放到缩略图大小
  // 3. 导出为 WebP base64
  // 4. 存入 page.metadata.thumbnail
  return "data:image/webp;base64,...";
}

// 注意：缩略图只包含 Persistent Canvas 的内容
// Overlay 上的新笔画在保存时已转入 Persistent，不会丢失
```

### 3.2 双缓冲冷启动

```typescript
class CanvasEngine {
  private persistent: SkCanvas;
  private overlay: SkCanvas;
  private thumbnail: SkImage | null;
  private strokes: Stroke[];
  private dirty: boolean;

  async init(strokes: Stroke[], thumbnailBase64: string) {
    this.strokes = strokes;

    // 1. 显示缩略图
    if (thumbnailBase64) {
      const data = Skia.Data.fromBase64(thumbnailBase64);
      this.thumbnail = Skia.Image.MakeFromEncoded(data);
    }

    // 2. 重建 Persistent Canvas（后台异步）
    this.rebuildPersistent();

    // 3. Overlay 立即可用
  }

  private async rebuildPersistent() {
    // 从 strokes[0] 开始，按 ts 顺序回放
    for (const stroke of this.strokes) {
      if (stroke.ts <= this.lastRebuiltTs) continue; // 跳过已重建的
      renderStrokeToCanvas(this.persistent, stroke, { scale: 1, offsetX: 0, offsetY: 0 });
    }
    this.dirty = false;

    // 回放完成后，替换缩略图
    this.thumbnail = null;
  }

  // 用户画了新笔画
  onNewStroke(stroke: Stroke) {
    // 画到 Overlay 上
    renderStrokeToCanvas(this.overlay, stroke, this.currentViewport);

    // 同时追加到 strokes[]
    this.strokes.push(stroke);
  }

  // 合成显示
  render() {
    if (this.thumbnail) {
      // 显示缩略图
      drawImage(this.thumbnail);
    } else {
      // 合成 Persistent + Overlay
      drawCanvas(this.persistent);
      // 在 persistent 上以 destination-out 应用 eraser
      // 然后叠加 overlay 上的新笔画
      drawCanvasComposited(this.persistent, this.overlay);
    }
  }
}
```

---

## 4. SVG 导出（evenodd）

### 4.1 算法

```typescript
function exportSVG(strokes: Stroke[]): string {
  // 1. 按 ts 排序
  const sorted = [...strokes].sort((a, b) => a.ts - b.ts);

  // 2. 构建一个 path d 字符串
  const inkPaths: string[] = [];   // 外路径
  const eraserPaths: string[] = []; // 内路径（子路径）

  for (const stroke of sorted) {
    const pathData = curvesToPathData(stroke.curves);
    if (stroke.type === "ink") {
      inkPaths.push(pathData);
    } else {
      eraserPaths.push(pathData);
    }
  }

  // 3. 合并为一个 path：ink写外路径，eraser写内路径
  const allPaths = [...inkPaths, ...eraserPaths];

  return `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">
    <path d="${allPaths.join(" ")}" fill-rule="evenodd" fill="${inkColor}" />
  </svg>`;
}

function curvesToPathData(curves: BezierCurve[]): string {
  let d = "";
  for (const c of curves) {
    if (d === "") {
      d += `M ${c.p0[0]} ${c.p0[1]} `;
    }
    d += `C ${c.p1[0]} ${c.p1[1]}, ${c.p2[0]} ${c.p2[1]}, ${c.p3[0]} ${c.p3[1]} `;
  }
  return d;
}
```

**注意：** 导出时压力信息已丢失（分叉为扁平化轮廓 polygon）。SVG 仅作为视觉参考，不是可编辑数据格式。完整数据始终保留在 `strokes.json` 中。

---

## 5. 撤销

### 5.1 Command Pattern

```typescript
interface Command {
  type: "add_stroke";
  stroke: Stroke;
  undo(): void;   // 从 strokes[] 移除 + overlay/persistent 重绘受影响部分
  redo(): void;   // 重新添加
}

class UndoManager {
  private stack: Command[] = [];
  private maxSize = 100;

  execute(cmd: Command) {
    cmd.redo();
    this.stack.push(cmd);
    if (this.stack.length > this.maxSize) this.stack.shift();
  }

  undo() {
    const cmd = this.stack.pop();
    if (cmd) cmd.undo();
  }
}

// undo 实现：
// 对于 ink stroke：从 persistent canvas 上删除这一笔（需重绘该区域）
// 对于 eraser stroke：删掉这一笔 = 被挖掉的 ink 重新可见
//    → 不需要重放时间线，ink 像素还在画布上
```

---

## 6. 图片处理

### 6.1 照片/PDF 插入

```typescript
interface CanvasImage {
  id: string;
  imageKey: string;       // COS 或本地路径
  x: number; y: number;    // 文档坐标
  width: number;
  height: number;
  rotation: number;        // 度，默认 0
  zIndex: number;          // 叠放次序，默认 0（笔划 > 图片）
}
```

**默认叠放次序：** 笔划在图片之上（zIndex: 1），图片在最底层（zIndex: 0）。用户可通过长按选中后调整位置。

**选中交互：** 长按图片 → 加载圈动画 → 选中 → 可拖拽移动 / 拽角缩放 / 弹出菜单（删除）。

---

## 7. 性能约束

| 项 | MVP 限制 |
|:---|:---------|
| 缩放范围 | 25% ~ 400% |
| 笔划数 | < 5000/页 |
| 冷启动缩略图 | ~20-50KB WebP |
| 撤销栈 | 100 步 |
| 定时保存 | 每 5 秒增量（防抖）|

---

## 8. 组件接口

```typescript
// RN 侧暴露的 CurioCanvas 组件
interface CurioCanvasProps {
  pageId: string;
  strokes: Stroke[];
  images: CanvasImage[];
  metadata: PageMetadata;
  viewport: Viewport;

  onStrokeAdd: (stroke: Stroke) => void;
  onSave: (page: PageData) => void;
  onPhotoCapture: () => void;
  onFileInsert: () => void;
  onUndo: () => void;
  onToggleDraft: () => void;
  onOpenMenu: () => void;
}
```
