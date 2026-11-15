# Builds the app icon from the real Meridian logo.
#
# Input:  packaging/assets/meridian_logo.png (copied from meridian/static/ in the core repo).
# Output: packaging/assets/meridian-desktop.ico (16, 32, 48 and 256 px, embedded in the exe
#         by packaging/build.py) and packaging/assets/meridian-desktop-preview.png.
#
# The logo's PNG has an opaque white background. The logo's ring is a circle, so every pixel
# outside that circle is made transparent. The white inside the ring is kept.
#
# Run from the repo root after updating the logo:
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging/make_icon.ps1
# Windows PowerShell 5.1 with System.Drawing only. No extra packages.

Add-Type -AssemblyName System.Drawing

$assets = Join-Path $PSScriptRoot 'assets'
$logoPath = Join-Path $assets 'meridian_logo.png'
$icoPath = Join-Path $assets 'meridian-desktop.ico'
$pngPath = Join-Path $assets 'meridian-desktop-preview.png'
$sizes = @(16, 32, 48, 256)

$source = [System.Drawing.Bitmap]::new((Resolve-Path $logoPath).Path)

# Bounding box of the ring: every pixel that is not near-white. Sampled every 4 pixels.
$minX = $source.Width; $minY = $source.Height; $maxX = -1; $maxY = -1
for ($y = 0; $y -lt $source.Height; $y += 4) {
    for ($x = 0; $x -lt $source.Width; $x += 4) {
        $c = $source.GetPixel($x, $y)
        if ($c.R -lt 240 -or $c.G -lt 240 -or $c.B -lt 240) {
            if ($x -lt $minX) { $minX = $x }
            if ($x -gt $maxX) { $maxX = $x }
            if ($y -lt $minY) { $minY = $y }
            if ($y -gt $maxY) { $maxY = $y }
        }
    }
}
if ($maxX -lt 0) { throw "no logo pixels found in $logoPath" }

$diameter = [Math]::Max($maxX - $minX, $maxY - $minY)
$centreX = ($minX + $maxX) / 2.0
$centreY = ($minY + $maxY) / 2.0
$side = [int][Math]::Ceiling($diameter * 1.04)
$cropX = [int]($centreX - $side / 2.0)
$cropY = [int]($centreY - $side / 2.0)

function New-LogoBitmap([int]$size) {
    $square = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($square)
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $src = New-Object System.Drawing.Rectangle $cropX, $cropY, $side, $side
    $dst = New-Object System.Drawing.Rectangle 0, 0, $size, $size
    $g.DrawImage($source, $dst, $src, [System.Drawing.GraphicsUnit]::Pixel)
    $g.Dispose()

    # Clip to the ring's outer circle with a one-pixel soft edge.
    $radius = ($diameter / 2.0) * ($size / [double]$side)
    $mid = ($size - 1) / 2.0
    for ($y = 0; $y -lt $size; $y++) {
        for ($x = 0; $x -lt $size; $x++) {
            $dist = [Math]::Sqrt([Math]::Pow($x - $mid, 2) + [Math]::Pow($y - $mid, 2))
            $coverage = [Math]::Min(1.0, [Math]::Max(0.0, $radius + 0.5 - $dist))
            $c = $square.GetPixel($x, $y)
            $alpha = [int]($c.A * $coverage)
            $square.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($alpha, $c.R, $c.G, $c.B))
        }
    }
    return $square
}

# PNG payloads for each size, then the ICO container around them (PNG-compressed entries).
$payloads = @()
foreach ($size in $sizes) {
    $bmp = New-LogoBitmap $size
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $payloads += , @{ Size = $size; Bytes = $ms.ToArray() }
    if ($size -eq 256) { $bmp.Save($pngPath, [System.Drawing.Imaging.ImageFormat]::Png) }
    $bmp.Dispose()
    $ms.Dispose()
}
$source.Dispose()

$stream = [System.IO.File]::Create($icoPath)
$writer = New-Object System.IO.BinaryWriter $stream
$writer.Write([UInt16]0)                 # reserved
$writer.Write([UInt16]1)                 # type 1 = icon
$writer.Write([UInt16]$payloads.Count)   # image count
$offset = 6 + 16 * $payloads.Count
foreach ($p in $payloads) {
    $dim = if ($p.Size -ge 256) { 0 } else { $p.Size }   # 0 means 256 in the ICO header
    $writer.Write([Byte]$dim)
    $writer.Write([Byte]$dim)
    $writer.Write([Byte]0)               # colour count (0 for 32 bpp)
    $writer.Write([Byte]0)               # reserved
    $writer.Write([UInt16]1)             # colour planes
    $writer.Write([UInt16]32)            # bits per pixel
    $writer.Write([UInt32]$p.Bytes.Length)
    $writer.Write([UInt32]$offset)
    $offset += $p.Bytes.Length
}
foreach ($p in $payloads) { $writer.Write($p.Bytes) }
$writer.Close()
$stream.Close()

Write-Output "wrote $icoPath (sizes: $($sizes -join ', '))"
Write-Output "wrote $pngPath"
