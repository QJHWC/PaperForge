[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputFile
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$WindowsPlatform = [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
    [System.Runtime.InteropServices.OSPlatform]::Windows
)
if (-not $WindowsPlatform) {
    throw "The v3 Windows gate must run on a real Windows host."
}

$Repository = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TemporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "paperforge-windows-gate-" + [System.Guid]::NewGuid().ToString("N")
)
$CvprCommit = "291758547e923160eb4d37079b7b9f0dfce82355"
$PythonVersions = @("3.10", "3.11", "3.12")
$Results = [ordered]@{}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Program,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    $Command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $Command) {
        throw "Required command is unavailable: $Name"
    }
    return $Command.Source
}

New-Item -ItemType Directory -Path $TemporaryRoot | Out-Null
try {
    $RequiredTools = [ordered]@{}
    foreach ($Name in @(
        "git", "node", "pdflatex", "bibtex", "latexmk", "pdftoppm"
    )) {
        $RequiredTools[$Name] = Require-Command $Name
    }

    Push-Location $Repository
    try {
        $BuildVenv = Join-Path $TemporaryRoot "build-venv"
        Invoke-Checked py "-3.11" "-m" "venv" $BuildVenv
        $BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
        Invoke-Checked $BuildPython "-m" "pip" "install" "--upgrade" "pip"
        Invoke-Checked $BuildPython "-m" "pip" "install" `
            "build==1.2.2.post1"
        $BuildRoot = Join-Path $TemporaryRoot "dist"
        Invoke-Checked $BuildPython "-m" "build" "--outdir" $BuildRoot
        $Wheel = Get-ChildItem -Path $BuildRoot `
            -Filter "paperforge_research_os-3.0.0-py3-none-any.whl" |
            Select-Object -First 1
        if ($null -eq $Wheel) {
            throw "The v3 wheel was not produced."
        }
        $WheelSha256 = (
            Get-FileHash $Wheel.FullName -Algorithm SHA256
        ).Hash.ToLowerInvariant()

        foreach ($Version in $PythonVersions) {
            $VersionRoot = Join-Path $TemporaryRoot ("python-" + $Version)
            $Venv = Join-Path $VersionRoot "venv"
            $BaseTemp = Join-Path $VersionRoot "pytest"
            New-Item -ItemType Directory -Path $VersionRoot | Out-Null

            Invoke-Checked py "-$Version" "-m" "venv" $Venv
            $Python = Join-Path $Venv "Scripts\python.exe"
            Invoke-Checked $Python "-m" "pip" "install" "--upgrade" "pip"
            Invoke-Checked $Python "-m" "pip" "install" $Wheel.FullName
            Invoke-Checked $Python "-m" "pip" "install" "-r" "requirements-dev.txt"
            Invoke-Checked $Python "-m" "pip" "check"
            Push-Location $TemporaryRoot
            try {
                Invoke-Checked $Python "-I" "-c" `
                    "import paperforge; from paperforge.api import PaperForgeService"
            }
            finally {
                Pop-Location
            }
            Invoke-Checked $Python "-m" "pytest" "-q" "-p" "no:cacheprovider" `
                "--basetemp" $BaseTemp
            Invoke-Checked $Python "-m" "ruff" "check" "."
            Invoke-Checked $Python "-m" "mypy" "paperforge"
            if ($Version -eq "3.11") {
                Invoke-Checked $Python "tools/verify_wheel.py" $Wheel.FullName
            }

            $Results[$Version] = [ordered]@{
                pytest = "passed"
                ruff = "passed"
                mypy = "passed"
                pip_check = "passed"
                installed_wheel_smoke = "passed"
                wheel_sha256 = $WheelSha256
            }
        }

        Invoke-Checked node "--check" "frontend/app.js"
        $CvprRoot = Join-Path $TemporaryRoot "cvpr-author-kit"
        Invoke-Checked git "clone" "--no-checkout" `
            "https://github.com/cvpr-org/author-kit.git" $CvprRoot
        Invoke-Checked git "-C" $CvprRoot "checkout" "--detach" $CvprCommit

        $Python311 = Join-Path $TemporaryRoot "python-3.11\venv\Scripts\python.exe"
        $PublicationEvidence = Join-Path $TemporaryRoot "publication-profiles.json"
        Invoke-Checked $Python311 "-m" "tools.validate_publication_profiles" `
            "--cvpr-author-kit" $CvprRoot "--output" $PublicationEvidence
        $PublicationResult = Get-Content $PublicationEvidence -Raw |
            ConvertFrom-Json
        if (-not $PublicationResult.passed) {
            throw "The four-profile publication gate failed."
        }

        $Evidence = [ordered]@{
            schema = "paperforge.windows-gate/v1"
            passed = $true
            generated_at = [DateTimeOffset]::UtcNow.ToString("o")
            os = [System.Environment]::OSVersion.VersionString
            architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
            python = $Results
            tools = $RequiredTools
            node_syntax = "passed"
            cvpr_commit = $CvprCommit
            publication_profiles = $PublicationResult
        }
        $ResolvedOutput = [System.IO.Path]::GetFullPath($OutputFile)
        $OutputParent = Split-Path -Parent $ResolvedOutput
        if ($OutputParent) {
            New-Item -ItemType Directory -Force -Path $OutputParent | Out-Null
        }
        $Evidence | ConvertTo-Json -Depth 100 |
            Set-Content -Path $ResolvedOutput -Encoding utf8
        Write-Host "Windows v3 gate passed: $ResolvedOutput"
    }
    finally {
        Pop-Location
    }
}
finally {
    if (Test-Path $TemporaryRoot) {
        Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force
    }
}
