# Builds dist\TsakasEQ.exe (Equalizer APO installer bundled inside).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$apo = "vendor\EqualizerAPO-x64-1.4.2.exe"
$sha = "7403be7427bbe1936a40dded082829b6e217fc4f5990fee5cba501f0ae055afa"
if (-not (Test-Path $apo)) {
    New-Item -ItemType Directory -Force vendor | Out-Null
    Invoke-WebRequest "https://sourceforge.net/projects/equalizerapo/files/1.4.2/EqualizerAPO-x64-1.4.2.exe/download" -OutFile $apo -UserAgent "Wget"
}
if ((Get-FileHash $apo -Algorithm SHA256).Hash -ne $sha) { throw "Equalizer APO installer checksum mismatch" }

# Loud mode's safety clipper: Airwindows ClipOnly2 (MIT), from the official 64-bit Windows VST2 bundle
$clip = "vendor\ClipOnly264.dll"
if (-not (Test-Path $clip)) {
    $zip = "$env:TEMP\WinVST64s.zip"
    Invoke-WebRequest "https://www.airwindows.com/wp-content/uploads/WinVST64s.zip" -OutFile $zip
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    [IO.Compression.ZipFileExtensions]::ExtractToFile($z.GetEntry("WinVST64s/ClipOnly264.dll"), "$PWD\$clip", $true)
    $z.Dispose(); Remove-Item $zip
}
if ((Get-FileHash $clip -Algorithm SHA256).Hash -ne "0b18ba1e0e0d53a640126b317d7abb2cc308c08af69b7d4a54345e604c18fb22") { throw "ClipOnly2 checksum mismatch" }

python tsakaseq.py --selftest
if ($LASTEXITCODE) { throw "Self-test failed" }
python -m PyInstaller --noconfirm --onefile --noconsole --name TsakasEQ --icon icon.ico `
    --add-data "ui.html;." --add-data "$apo;vendor" --add-data "$clip;vendor" tsakaseq.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }
