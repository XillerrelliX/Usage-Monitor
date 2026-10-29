# Existing installations take the interpreter check and local refresh path only.
# No admin rights, global PATH edits, account calls, or model calls.
$ErrorActionPreference = 'Stop'
$usageArguments = @($args)
$packageRoot = Split-Path -Parent $PSScriptRoot
$pythonCommand = $null
$pythonPrefix = @()

function Test-UsagePython([string]$Executable, [string[]]$Prefix) {
    try {
        & $Executable @Prefix -B -X utf8 -c 'import sys,sqlite3,ssl; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Get-UsageHash([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}

try {
    # A previously installed private interpreter is found after config resolution.
    $runtimeDirectory = $null
    $explicitConfig = $null
    $explicitRuntime = $null
    for ($index=0; $index -lt $usageArguments.Count; $index++) {
        if ($usageArguments[$index] -eq '--config' -and $index+1 -lt $usageArguments.Count) {
            $explicitConfig = $usageArguments[$index+1]
        } elseif ($usageArguments[$index].StartsWith('--config=')) {
            $explicitConfig = $usageArguments[$index].Substring(9)
        } elseif ($usageArguments[$index] -eq '--runtime-dir' -and $index+1 -lt $usageArguments.Count) {
            $explicitRuntime = [IO.Path]::GetFullPath($usageArguments[$index+1])
        } elseif ($usageArguments[$index].StartsWith('--runtime-dir=')) {
            $explicitRuntime = [IO.Path]::GetFullPath($usageArguments[$index].Substring(14))
        }
    }
    $settingsFile = $null
    if ($null -ne $explicitConfig) {
        $settingsFile = [IO.Path]::GetFullPath($explicitConfig)
    } elseif (Test-Path -LiteralPath (Join-Path $packageRoot '.usage-setup.json')) {
        $pointer = Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $packageRoot '.usage-setup.json') | ConvertFrom-Json
        if ($pointer.schemaVersion -ne 1 -or -not [IO.Path]::IsPathRooted($pointer.settingsFile)) {
            throw 'Invalid local setup pointer; original bytes preserved.'
        }
        $settingsFile = $pointer.settingsFile
    }
    if ($null -ne $settingsFile) {
        $runtimeDirectory = Join-Path (Split-Path -Parent $settingsFile) 'runtime'
        if (Test-Path -LiteralPath $settingsFile -PathType Leaf) {
            $saved = Get-Content -Encoding UTF8 -Raw -LiteralPath $settingsFile | ConvertFrom-Json
            if ($null -ne $saved.offline.runtimeDirectory) {
                if (-not [IO.Path]::IsPathRooted([string]$saved.offline.runtimeDirectory)) {
                    throw 'Saved runtime path must be absolute; original settings preserved.'
                }
                $runtimeDirectory = [string]$saved.offline.runtimeDirectory
            }
        }
    }
    if ($null -ne $explicitRuntime) { $runtimeDirectory = $explicitRuntime }
    # Special folders may be empty in a new/redirected profile. Resolve CLI and
    # saved paths first, then match local_setup's per-user fallback convention.
    $defaultConfigDirectory = $null
    if ($null -eq $runtimeDirectory) {
        $localBase = $env:LOCALAPPDATA
        if ([string]::IsNullOrWhiteSpace($localBase)) { $localBase = [Environment]::GetFolderPath('LocalApplicationData') }
        if ([string]::IsNullOrWhiteSpace($localBase)) {
            $profileBase = $env:USERPROFILE
            if ([string]::IsNullOrWhiteSpace($profileBase)) { $profileBase = [Environment]::GetFolderPath('UserProfile') }
            if ([string]::IsNullOrWhiteSpace($profileBase)) { throw 'Cannot resolve this user profile; select --config or --runtime-dir.' }
            $localBase = Join-Path $profileBase 'AppData\Local'
        }
        $defaultConfigDirectory = Join-Path $localBase 'codex-local-usage'
        if ($null -eq $runtimeDirectory) { $runtimeDirectory = Join-Path $defaultConfigDirectory 'runtime' }
    }
    # A copied dashboard keeps its original prebuilt runtime in saved settings.
    # Delegate before probing Python so a damaged runtime cannot trigger downloads.
    if ((Test-Path -LiteralPath (Join-Path $runtimeDirectory 'prebuilt-manifest.json')) -or
        (Test-Path -LiteralPath (Join-Path $runtimeDirectory 'prebuilt-required')) -or
        (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $runtimeDirectory) 'bundle-manifest.json') -PathType Leaf)) {
        $prebuiltRoot = Split-Path -Parent $runtimeDirectory
        $prebuiltLauncher = Join-Path $prebuiltRoot 'scripts\windows_start.ps1'
        if (-not (Test-Path -LiteralPath (Join-Path $prebuiltRoot 'bundle-manifest.json') -PathType Leaf) -or
            -not (Test-Path -LiteralPath $prebuiltLauncher -PathType Leaf)) {
            throw 'Usage Monitor prebuilt package is incomplete. Extract the original Windows ZIP again.'
        }
        $prebuiltArguments = @($usageArguments | Where-Object { $_ -notin @('--setup','--offline') })
        $savedSelection = Join-Path $packageRoot '.usage-selection.json'
        if (Test-Path -LiteralPath $savedSelection -PathType Leaf) {
            $prebuiltArguments = @('--selection-file', $savedSelection) + $prebuiltArguments
        }
        if ($null -eq $explicitConfig -and $null -ne $settingsFile) {
            $prebuiltArguments = @('--config', $settingsFile) + $prebuiltArguments
        }
        & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoLogo -NoProfile -ExecutionPolicy Bypass -File $prebuiltLauncher @prebuiltArguments
        exit $LASTEXITCODE
    }
    $hintFiles = @((Join-Path $packageRoot '.usage-python'))
    if ($null -ne $settingsFile) {
        $hintFiles += Join-Path (Split-Path -Parent $settingsFile) '.usage-python'
    } elseif ($null -ne $defaultConfigDirectory) {
        $hintFiles += Join-Path $defaultConfigDirectory '.usage-python'
    }
    $portablePython = Join-Path $runtimeDirectory 'python\python.exe'
    if (Test-Path -LiteralPath $portablePython) {
        if (Test-UsagePython $portablePython @()) { $pythonCommand = $portablePython }
    }
    foreach ($hint in $hintFiles) {
        if ($null -ne $pythonCommand) { break }
        if (-not (Test-Path -LiteralPath $hint -PathType Leaf)) { continue }
        $hintPython = [IO.File]::ReadAllText($hint, [Text.Encoding]::UTF8).TrimEnd("`r","`n")
        if ([string]::IsNullOrWhiteSpace($hintPython) -or $hintPython.Contains("`r") -or $hintPython.Contains("`n") -or -not [IO.Path]::IsPathRooted($hintPython)) {
            throw 'Invalid local interpreter hint; expected one absolute filename. Original bytes preserved.'
        }
        if (Test-Path -LiteralPath $hintPython -PathType Leaf) {
            if (Test-UsagePython $hintPython @()) { $pythonCommand = $hintPython }
        }
    }
    foreach ($name in @('py','python3','python')) {
        if ($null -ne $pythonCommand) { break }
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -eq $command -or $command.Source -like '*\WindowsApps\*') { continue }
        $prefix = @()
        if ($name -eq 'py') { $prefix = @('-3') }
        if (Test-UsagePython $command.Source $prefix) {
            $pythonCommand = $command.Source
            $pythonPrefix = $prefix
        }
    }
    if ($null -eq $pythonCommand) {
        if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') {
            throw 'Automatic Windows Python setup currently requires x64.'
        }
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $packageRoot 'assets\runtime-downloads.json') | ConvertFrom-Json
        $entry = $manifest.platforms.'windows-x86_64'.python
        $uri = [Uri]$entry.url
        if ($uri.Scheme -ne 'https' -or $uri.Host -ne 'www.python.org' -or $entry.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw 'Untrusted portable Python manifest entry.'
        }
        $downloads = Join-Path $runtimeDirectory 'downloads'
        New-Item -ItemType Directory -Path $downloads -Force | Out-Null
        $archive = Join-Path $downloads ([IO.Path]::GetFileName($uri.AbsolutePath))
        if (-not (Test-Path -LiteralPath $archive) -or (Get-UsageHash $archive) -ne $entry.sha256) {
            $partial = $archive + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
            [Console]::Error.WriteLine('Setup: downloading verified portable Python from python.org')
            $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
            $downloaded = $false
            if (Test-Path -LiteralPath $curl -PathType Leaf) {
                & $curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' --output $partial $entry.url
                $downloaded = ($LASTEXITCODE -eq 0)
            }
            if (-not $downloaded) {
                # Retain any failed curl bytes. The independent fallback gets a
                # fresh file and uses the platform's normal TLS verification.
                $partial = $archive + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -UseBasicParsing -Uri $entry.url -OutFile $partial
            }
            if ((Get-UsageHash $partial) -ne $entry.sha256) {
                throw ('Python SHA256 mismatch; unexecuted download preserved: ' + $partial)
            }
            if (Test-Path -LiteralPath $archive) { Move-Item -LiteralPath $archive -Destination ($archive + '.previous-' + [Guid]::NewGuid().ToString('N')) }
            Move-Item -LiteralPath $partial -Destination $archive
        }
        $staging = Join-Path $runtimeDirectory ('python.staging-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $staging | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            foreach ($item in $zip.Entries) {
                $name = $item.FullName.Replace('\','/')
                if ($name.StartsWith('/') -or $name -match '(^|/)\.\.(/|$)|:|\x00' -or (($item.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) {
                    throw 'Unsafe portable Python archive member.'
                }
                $target = [IO.Path]::GetFullPath((Join-Path $staging $name))
                if (-not $target.StartsWith($staging + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Python archive leaves installation folder.' }
            }
        } finally { $zip.Dispose() }
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $staging)
        if (-not (Test-UsagePython (Join-Path $staging 'python.exe') @())) { throw 'Verified portable Python failed its standard library check.' }
        @{sha256=$entry.sha256;url=$entry.url} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $staging '.installed.json')
        $destination = Join-Path $runtimeDirectory 'python'
        if (Test-Path -LiteralPath $destination) { Move-Item -LiteralPath $destination -Destination ($destination + '.previous-' + [Guid]::NewGuid().ToString('N')) }
        Move-Item -LiteralPath $staging -Destination $destination
        $pythonCommand = $portablePython
    }
    # The -c bridge also supports CPython's isolated embeddable distribution,
    # whose default sys.path does not otherwise contain this source directory.
    $bridge = "import runpy,sys; p=sys.argv.pop(1); sys.path.insert(0,p); runpy.run_path(p+'/setup_runtime.py',run_name='__main__')"
    & $pythonCommand @pythonPrefix -B -X utf8 -c $bridge $PSScriptRoot @usageArguments
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('Setup failed: ' + $_.Exception.Message)
    exit 1
}
