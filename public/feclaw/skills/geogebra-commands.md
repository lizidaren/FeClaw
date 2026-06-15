# Skill: GeoGebra 图形生成

当用户要求对数学图形生成 GeoGebra 交互式图形时（如"画出来""生成图形""帮我做GGB"），
按以下步骤操作。

---

## 工作流程

```
用户发图片/描述 → 分析数学内容 → 生成 GGB 命令 → 保存文件 → 分享链接
```

## 1. 分析数学内容

### 方式 A：基于用户图片
调用 `spawn_subagent` 分析图片中的数学对象：

```
spawn_subagent(
    model="doubao-seed-2-0-pro-260215"
    reasoning_effort="high"
    task=(
        "分析图片中的数学内容，生成对应的 GeoGebra 命令。\n"
        "如果是2D图形（函数、平面几何、平面向量等）→ 使用2D命令\n"
        "如果是3D图形（立体几何、空间向量、旋转体等）→ 使用3D命令\n"
        "每行一条命令，不要包含markdown标记、不要注释说明。\n"
        "只返回纯文本格式的 GeoGebra 命令列表。"
    )
    image_path="用户消息中标明的图片路径"
)
```

### 方式 B：基于用户文字描述
直接根据用户的文字描述生成命令，跳过图片分析步骤。

## 2. GeoGebra 命令参考

### 2D 常用命令

#### 点
```geogebra
A = (0, 0)
B = (3, 4)
C = (1, 2)
M = Midpoint(A, B)           # 中点
```

#### 线 / 线段 / 射线
```geogebra
Segment(A, B)                # 线段 AB
Line(A, B)                   # 通过 A、B 的直线
Ray(A, B)                    # 从 A 出发经过 B 的射线
PerpendicularLine(A, g)      # 过点 A 作直线 g 的垂线
ParallelLine(A, g)           # 过点 A 作直线 g 的平行线
AngleBisector(A, B, C)       # ∠ABC 的角平分线
```

#### 多边形
```geogebra
Polygon(A, B, C)             # 三角形 ABC
Polygon(A, B, C, D)          # 四边形 ABCD
TriangleCenter(A, B, C, n)   # 三角形中心（n=2重心, 3外心, 4内心）
```

#### 圆与弧
```geogebra
Circle(A, B)                 # 以 A 为圆心、过 B 的圆
Circle(A, r)                 # 以 A 为圆心、半径 r 的圆
Arc(A, B, C)                 # 通过 A、B、C 的弧
Sector(A, B, C)              # 扇形
```

#### 函数曲线
```geogebra
f(x) = x^2 + 2*x + 1         # 二次函数
g(x) = sin(x)                # 正弦函数
h(x) = 2^x                   # 指数函数
```

#### 测量与计算
```geogebra
Distance(A, B)               # 点 A 到 B 的距离
Length(segment)              # 线段长度
Angle(A, B, C)               # ∠ABC 的角度
Area(polygon)                # 多边形面积
Slope(g)                     # 直线 g 的斜率
```

#### 几何变换
```geogebra
Rotate(A, angle, B)          # 将 A 绕 B 旋转 angle 度
Translate(A, vector)         # 将 A 沿向量平移
Reflect(A, g)                # 将 A 关于直线 g 镜像
Dilate(A, factor, B)         # 将 A 以 B 为中心缩放 factor 倍
```

### 3D 常用命令

#### 空间点
```geogebra
A = (1, 2, 3)
B = (0, 0, 0)
```

#### 空间几何体
```geogebra
Sphere(A, r)                 # 球体：以 A 为球心，半径 r
Cylinder(A, B, r)            # 圆柱：A 到底面中心，B 到顶面中心，半径 r
Cone(A, B, r)                # 圆锥：以 A 为顶点，B 到中心，半径 r
Prism(A, B, C)               # 棱柱
Pyramid(A, B, C, D)          # 棱锥
Tetrahedron(A, B, C)         # 四面体
```

#### 空间线面
```geogebra
Segment(A, B)                # 空间线段
Line(A, B)                   # 空间直线
Plane(A, B, C)               # 通过三点确定平面
Plane(A, n)                  # 过点 A、法向量 n 的平面
Intersect(p1, p2)            # 平面与平面/球体的交线
```

#### 空间向量
```geogebra
u = Vector(A, B)             # 向量 AB
v = (1, 2, 3)                # 直接定义向量
w = Cross(u, v)              # 叉积
d = Dot(u, v)                # 点积
```

### 样式控制
```geogebra
SetColor(object, "red")      # 设置颜色（red/blue/green/yellow/black/white）
SetThickness(object, 3)      # 设置线宽
SetPointSize(A, 5)           # 设置点大小
SetPointStyle(A, 2)          # 设置点样式（0=点, 1=叉, 2=圆, 3=方）
SetLineStyle(g, 1)           # 设置线型（0=实线, 1=虚线, 2=点线）
SetFilling(polygon, 0.3)     # 设置填充透明度
ShowLabel(A, true)           # 显示标签
HideLabel(B)                 # 隐藏标签
```

### 动态和交互
```geogebra
Slider(a)                    # 创建滑块变量 a
Slider(a, 0, 10, 1)          # 范围 0-10，步长 1
Animate()                    # 启动动画
StartAnimation()             # 启动动画
SetValue(slider, 5)          # 设置滑块值
Text("公式", A)              # 在点 A 位置显示文本
Text("\\frac{1}{2}", A)      # 显示 LaTeX 公式
```

### 常用组合示例

#### 平面几何：三角形的重心
```geogebra
A = (0, 0)
B = (4, 0)
C = (1, 3)
Polygon(A, B, C)
M_a = Midpoint(B, C)
M_b = Midpoint(A, C)
M_c = Midpoint(A, B)
Segment(A, M_a)
Segment(B, M_b)
Segment(C, M_c)
```

#### 函数图像与切线
```geogebra
f(x) = x^2 - 4*x + 3
A = Point(f)                 # 曲线上取点
t = Tangent(A, f)            # 过 A 的切线
f'(x)                        # 导函数
```

#### 立体几何：正方体
```geogebra
A = (0, 0, 0)
B = (2, 0, 0)
C = (2, 2, 0)
D = (0, 2, 0)
A1 = (0, 0, 2)
B1 = (2, 0, 2)
C1 = (2, 2, 2)
D1 = (0, 2, 2)
Polygon(A, B, C, D)
Polygon(A1, B1, C1, D1)
Segment(A, A1)
Segment(B, B1)
Segment(C, C1)
Segment(D, D1)
```

## 3. 保存文件

> ⚠️ **关键：扩展名必须为 `.2dggb` 或 `.3dggb`，绝对不要使用 `.txt`！**
> 
> 只有正确的扩展名才会触发 GeoGebra 交互式预览器。如果保存为 `.txt`，用户只能看到纯文本，无法看到图形。

根据命令类型决定扩展名：

| 命令类型 | 扩展名 | 示例路径 |
|---------|--------|---------|
| 仅 2D 命令 | `.2dggb` | `workspace/output/question_xxx.2dggb` |
| 含 3D 命令 | `.3dggb` | `workspace/output/question_xxx.3dggb` |

执行：
```
echo "命令内容" > /workspace/output/question_{序号}.{扩展名}
```

> 如果子 Agent 返回了额外的文字说明（不是 GGB 命令），只取纯命令部分保存。
> 命令文件应该是纯文本格式，每行一条命令，不包含任何 markdown 标记。

## 4. 创建分享链接

> ⚠️ **必须使用 `create_share_link` 工具创建分享链接**。只有通过分享链接访问，才会自动触发 GeoGebra 交互式预览器。
> 不要直接发送文件路径给用户，用户无法通过 VFS 路径打开交互式画板。

```
create_share_link(path="/workspace/output/question_{序号}.{扩展名}")
```

分享链接 = 交互式 GeoGebra 画板页面，用户在浏览器打开即可看到图形并操作，支持平移、缩放、旋转（3D）等操作。

## 5. 整理后回复

- 告知用户已生成 GeoGebra 图形
- 附上分享链接
- 可简要说明图形中包含的数学内容
