<#
    Puts an "SPCX Morning" icon on the Desktop pointing at the launcher.

    Run it once:

        powershell -ExecutionPolicy Bypass -File ".\Create Desktop Icon.ps1"

    It looks for an existing Market Scanner image and uses that. A Windows
    shortcut will not take a .png, so a .png or .jpg is converted to a .ico
    first -- the PNG is embedded whole, which Windows has understood since
    Vista, so nothing is redrawn or degraded.

    Point it somewhere specific with -Image:

        ... -File ".\Create Desktop Icon.ps1" -Image "C:\Dev\Market-Scanner\logo.png"
#>

[CmdletBinding()]
param(
    [string] $Image,
    [string] $Name = "SPCX Morning",
    [string[]] $SearchPaths = @(
        "C:\Dev\Market-Scanner",
        "C:\Dev\Scanner-Studio",
        "C:\Dev\Feed-Check"
    )
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $here "SPCX Morning.bat"

if (-not (Test-Path $target)) {
    throw "Cannot find '$target'. Keep this script beside the .bat it launches."
}

function Find-Image {
    param([string[]] $Roots)
    $patterns = @("*.ico", "*icon*.png", "*logo*.png", "*scanner*.png", "*.png")
    foreach ($pattern in $patterns) {
        foreach ($root in $Roots) {
            if (-not (Test-Path $root)) { continue }
            $hit = Get-ChildItem -Path $root -Filter $pattern -Recurse -File `
                       -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -notmatch '\\(\.git|\.venv|node_modules|__pycache__)\\' } |
                   Sort-Object Length -Descending |
                   Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    return $null
}

if (-not $Image) {
    Write-Host "Looking for a Market Scanner image..."
    $Image = Find-Image -Roots $SearchPaths
    if ($Image) {
        Write-Host "  found: $Image"
    } else {
        Write-Host "  none matched in:" -ForegroundColor Yellow
        $SearchPaths | ForEach-Object { Write-Host "    $_" }

        # Show what images DO exist, so the path can be copied rather than hunted.
        $candidates = foreach ($root in $SearchPaths) {
            if (Test-Path $root) {
                Get-ChildItem -Path $root -Include *.png,*.ico,*.jpg,*.jpeg `
                              -Recurse -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.FullName -notmatch '\\(\.git|\.venv|node_modules|__pycache__)\\' }
            }
        }
        if ($candidates) {
            Write-Host ""
            Write-Host "  Images found nearby - copy one of these paths:" -ForegroundColor Cyan
            $candidates | Select-Object -First 15 |
                ForEach-Object { Write-Host "    $($_.FullName)" }
        }
        Write-Host ""
        Write-Host "  Then re-run with:" -ForegroundColor Yellow
        Write-Host "    powershell -ExecutionPolicy Bypass -File "".\Create Desktop Icon.ps1"" -Image ""<path>"""
        Write-Host "  Carrying on without one; the shortcut gets the default icon."
    }
} elseif (-not (Test-Path $Image)) {
    throw "No such image: $Image"
}

function Convert-ToIcon {
    <#
        Wrap a PNG in an .ico container. An .ico is a 22-byte header
        followed by image data, and since Vista that data may be a PNG
        rather than a BMP -- so the picture is embedded as-is instead of
        being re-rendered into a palette.
    #>
    param([string] $Source, [string] $Destination)

    Add-Type -AssemblyName System.Drawing

    $bitmap = [System.Drawing.Bitmap]::FromFile($Source)
    try {
        # Windows reads 0 in the size byte as 256, which is the largest an
        # .ico entry can describe.
        $side = 256
        $square = New-Object System.Drawing.Bitmap $side, $side
        $graphics = [System.Drawing.Graphics]::FromImage($square)
        try {
            $graphics.InterpolationMode =
                [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.Clear([System.Drawing.Color]::Transparent)

            # Fit inside the square without stretching the picture.
            $scale = [Math]::Min($side / $bitmap.Width, $side / $bitmap.Height)
            $w = [int]($bitmap.Width * $scale)
            $h = [int]($bitmap.Height * $scale)
            $graphics.DrawImage($bitmap, [int](($side - $w) / 2),
                                [int](($side - $h) / 2), $w, $h)
        } finally {
            $graphics.Dispose()
        }

        $buffer = New-Object System.IO.MemoryStream
        $square.Save($buffer, [System.Drawing.Imaging.ImageFormat]::Png)
        $png = $buffer.ToArray()
        $buffer.Dispose()
        $square.Dispose()

        $out = [System.IO.File]::Create($Destination)
        $writer = New-Object System.IO.BinaryWriter($out)
        try {
            $writer.Write([uint16]0)              # reserved
            $writer.Write([uint16]1)              # 1 = icon
            $writer.Write([uint16]1)              # one image
            $writer.Write([byte]0)                # width  (0 means 256)
            $writer.Write([byte]0)                # height (0 means 256)
            $writer.Write([byte]0)                # palette size
            $writer.Write([byte]0)                # reserved
            $writer.Write([uint16]1)              # colour planes
            $writer.Write([uint16]32)             # bits per pixel
            $writer.Write([uint32]$png.Length)    # bytes of image data
            $writer.Write([uint32]22)             # offset to that data
            $writer.Write($png)
        } finally {
            $writer.Dispose()
            $out.Dispose()
        }
    } finally {
        $bitmap.Dispose()
    }
}

$iconPath = $null
if ($Image) {
    if ([IO.Path]::GetExtension($Image).ToLower() -eq ".ico") {
        $iconPath = (Resolve-Path $Image).Path
        Write-Host "Using the .ico as it is."
    } else {
        $iconPath = Join-Path $here "spcx.ico"
        Write-Host "Converting to $iconPath ..."
        try {
            Convert-ToIcon -Source (Resolve-Path $Image).Path -Destination $iconPath
            Write-Host "  done."
        } catch {
            Write-Host "  conversion failed: $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host "  The shortcut will use the default icon instead."
            $iconPath = $null
        }
    }
}

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $target
$link.WorkingDirectory = $here
$link.Description = "SPCX morning candles and MACD alerts"
if ($iconPath) { $link.IconLocation = "$iconPath,0" }
$link.Save()

Write-Host ""
Write-Host "Done. '$Name' is on your Desktop." -ForegroundColor Green
Write-Host "  launches : $target"
if ($iconPath) { Write-Host "  icon     : $iconPath" }
Write-Host ""
Write-Host "If the icon still looks generic, Windows has cached the old one."
Write-Host "Renaming the shortcut, or logging out and back in, clears it."
