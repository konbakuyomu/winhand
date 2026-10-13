# OCR helper for winhand's `ocr` tool: Windows' built-in Windows.Media.Ocr. Needs Windows
# PowerShell 5.1, which can call WinRT (PowerShell 7 cannot).
# -Images: picture files separated by '|'; -Languages: language tags separated by ','
# (default: the user's OCR language, plus en-US when installed). Each picture is read once per
# language. Output is one JSON document on stdout; word rectangles are in the picture's pixels.
param(
    [string] $Images = '',
    [string] $Languages = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($operation, [Type] $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    $task.Result
}
function Engine([string] $tag) {
    [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($tag))
}

$available = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
$fromProfile = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$out = [ordered]@{
    available = $available
    user = if ($fromProfile) { $fromProfile.RecognizerLanguage.LanguageTag } else { $null }
    passes = @()
}
$engines = @()
if ($Languages) {
    foreach ($tag in $Languages.Split(',')) {
        $tag = $tag.Trim()
        $engine = if ($available -contains $tag) { Engine $tag } else { $null }
        if (-not $engine) {
            $out.error = 'language'
            $out.language = $tag
            $out | ConvertTo-Json -Depth 3 -Compress
            exit 0
        }
        $engines += $engine
    }
} else {
    if ($fromProfile) { $engines += $fromProfile }
    elseif ($available.Count) { $engines += Engine $available[0] }
    if ($engines.Count -and $available -contains 'en-US' -and $engines[0].RecognizerLanguage.LanguageTag -ne 'en-US') {
        $engines += Engine 'en-US'
    }
}
if (-not $engines.Count) {
    $out.error = 'no_language'
    $out | ConvertTo-Json -Depth 3 -Compress
    exit 0
}

$index = 0
foreach ($path in ($Images.Split('|') | Where-Object { $_ })) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        foreach ($engine in $engines) {
            $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @(foreach ($line in $result.Lines) {
                    , @(foreach ($word in $line.Words) {
                            $r = $word.BoundingRect
                            [ordered]@{ t = $word.Text; r = @([math]::Round($r.X, 1), [math]::Round($r.Y, 1), [math]::Round($r.Width, 1), [math]::Round($r.Height, 1)) }
                        })
                })
            $out.passes += [ordered]@{ image = $index; language = $engine.RecognizerLanguage.LanguageTag; lines = $lines }
        }
    } finally {
        $stream.Dispose()
    }
    $index++
}
$out | ConvertTo-Json -Depth 8 -Compress
