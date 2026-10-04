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

python tsakaseq.py --selftest
python -m PyInstaller --noconfirm --onefile --noconsole --name TsakasEQ --icon icon.ico `
    --add-data "ui.html;." --add-data "$apo;vendor" tsakaseq.py
