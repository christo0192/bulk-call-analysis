param([switch]$Check)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Stop-WithMessage([string]$Message) {
    Write-Error $Message
    exit 1
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Stop-WithMessage "Install uv and put it on PATH." }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Stop-WithMessage "Install Node.js 20 or newer and put node on PATH." }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Stop-WithMessage "Install npm and put it on PATH." }
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { Stop-WithMessage "Install Rust and put cargo on PATH." }

$nodeVersion = (& node --version).Trim()
if ($nodeVersion -notmatch '^v(\d+)') { Stop-WithMessage "Install Node.js 20 or newer and put node on PATH." }
if ([int]$Matches[1] -lt 20) { Stop-WithMessage "Install Node.js 20 or newer." }

# The linker depends on the Rust toolchain: MSVC hosts need the Visual Studio C++ build tools,
# GNU and GNU-LLVM hosts need a MinGW clang (for example llvm-mingw) on PATH.
$rustHost = ((& rustc -vV) | Where-Object { $_ -like "host:*" }) -replace "^host:\s*", ""
if ($rustHost -like "*-msvc") {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere)) { Stop-WithMessage "Install the Visual Studio C++ build tools (your Rust toolchain is $rustHost)." }
    $link = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find "VC\Tools\MSVC\*\bin\Hostx64\x64\link.exe" 2>$null
    if (-not $link) { Stop-WithMessage "Install the Visual Studio C++ build tools (your Rust toolchain is $rustHost)." }
    $linker = "MSVC build tools"
} else {
    $clang = Get-Command "x86_64-w64-mingw32-clang", "clang" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $clang) { Stop-WithMessage "Put a MinGW clang (for example the llvm-mingw bin folder) on PATH (your Rust toolchain is $rustHost)." }
    $linker = $clang.Name
}

Write-Output "uv: found"
Write-Output "node: $nodeVersion"
Write-Output "npm: found"
Write-Output "cargo: found"
Write-Output "rust host: $rustHost, linker: $linker"

Push-Location (Join-Path $repo "engine")
try {
    # spaCy is the optional nlp extra; --inexact keeps the name model, which is installed outside the lock.
    & uv sync --extra nlp --inexact
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "uv sync failed; install uv and check the engine dependencies." }
    # Masking needs spaCy's small English model to find names; install the pinned wheel once.
    & uv run python -c "from bca.setup.spacy_model import ensure_model; ensure_model()"
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Installing the name-masking model failed; check the internet connection and run this again." }
}
finally { Pop-Location }

$ui = Join-Path $repo "shell\ui"
$lock = Join-Path $ui "package-lock.json"
$modules = Join-Path $ui "node_modules"
if (-not (Test-Path -LiteralPath $modules) -or (Get-Item -LiteralPath $lock).LastWriteTimeUtc -gt (Get-Item -LiteralPath $modules).LastWriteTimeUtc) {
    Push-Location $ui
    try { & npm ci; if ($LASTEXITCODE -ne 0) { Stop-WithMessage "npm ci failed; install Node.js and npm dependencies." } }
    finally { Pop-Location }
}
Push-Location $ui
try { & npm run build; if ($LASTEXITCODE -ne 0) { Stop-WithMessage "npm run build failed; fix the UI build before starting the app." } }
finally { Pop-Location }
if ($Check) { Write-Output "Checks complete; the app was not started."; exit 0 }

$venvPython = Join-Path $repo "engine\.venv\Scripts\python.exe"
$env:BCA_ENGINE_PROGRAM = $venvPython
$env:BCA_ENGINE_ARGS = '["-m","bca.api.stdio"]'
Push-Location (Join-Path $repo "shell\src-tauri")
try { & cargo run --release --features custom-protocol }
finally { Pop-Location }
