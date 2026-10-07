# Capture the whole virtual screen to a PNG, used to check where the
# automation's clicks actually land.
param([string]$Out = "shot.png")
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose(); $bmp.Dispose()
Write-Output "$Out $($bounds.Width)x$($bounds.Height) at ($($bounds.X),$($bounds.Y))"
