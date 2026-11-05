# 生成用电总览应用图标（深色圆角底 + 白色闪电）
Add-Type -AssemblyName System.Drawing

function New-Icon {
    param([int]$Size, [string]$Path)
    $bmp = New-Object System.Drawing.Bitmap($Size, $Size)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    $rect = New-Object System.Drawing.Rectangle(0, 0, $Size, $Size)
    $radius = [int]($Size * 0.22)
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = [int]($radius * 2)
    $path.AddArc($rect.X, $rect.Y, $d, $d, 180, 90)
    $path.AddArc($rect.Right - $d, $rect.Y, $d, $d, 270, 90)
    $path.AddArc($rect.Right - $d, $rect.Bottom - $d, $d, $d, 0, 90)
    $path.AddArc($rect.X, $rect.Bottom - $d, $d, $d, 90, 90)
    $path.CloseFigure()

    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        $rect,
        [System.Drawing.Color]::FromArgb(255, 13, 36, 64),
        [System.Drawing.Color]::FromArgb(255, 26, 94, 168),
        45.0)
    $g.FillPath($brush, $path)

    # 顶部高光
    $glow = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        $rect,
        [System.Drawing.Color]::FromArgb(120, 255, 255, 255),
        [System.Drawing.Color]::FromArgb(0, 255, 255, 255),
        90.0)
    $topRect = New-Object System.Drawing.Rectangle(0, 0, $Size, [int]($Size * 0.42))
    $g.FillRectangle($glow, $topRect)

    # 闪电（先外发光，再实心）
    $pts = @(
        (New-Object System.Drawing.PointF($Size * 0.60, $Size * 0.06)),
        (New-Object System.Drawing.PointF($Size * 0.32, $Size * 0.54)),
        (New-Object System.Drawing.PointF($Size * 0.49, $Size * 0.54)),
        (New-Object System.Drawing.PointF($Size * 0.37, $Size * 0.94)),
        (New-Object System.Drawing.PointF($Size * 0.68, $Size * 0.46)),
        (New-Object System.Drawing.PointF($Size * 0.51, $Size * 0.46)),
        (New-Object System.Drawing.PointF($Size * 0.68, $Size * 0.06))
    )
    $glowBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(90, 255, 255, 255))
    $glowPts = @()
    $cx = $Size * 0.5; $cy = $Size * 0.52
    foreach ($p in $pts) { $glowPts += (New-Object System.Drawing.PointF(($p.X - $cx) * 1.08 + $cx, ($p.Y - $cy) * 1.08 + $cy)) }
    $g.FillPolygon($glowBrush, $glowPts)
    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    $g.FillPolygon($white, $pts)

    $g.Dispose()
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
}

$out = "C:\Users\Administrator\Doubao\chats\2026-10-24\new-chat\electricity-dashboard-fpk\fpk"
New-Icon 64  "$out\ICON.PNG"
New-Icon 256 "$out\ICON_256.PNG"
New-Icon 64  "$out\app\ui\images\icon_64.png"
New-Icon 256 "$out\app\ui\images\icon_256.png"
"ICONS_DONE"
