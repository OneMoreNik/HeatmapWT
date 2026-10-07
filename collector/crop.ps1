# Capture a rectangle of the screen. Used to compare a small region before and
# after a click, to tell whether the click had any effect.
param([int]$X = 0, [int]$Y = 0, [int]$W = 60, [int]$H = 20, [string]$Out = "crop.png")
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap $W, $H
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($X, $Y, 0, 0, (New-Object System.Drawing.Size $W, $H))
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose(); $bmp.Dispose()
