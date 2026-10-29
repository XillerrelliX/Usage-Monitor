# Verify the complete shipped payload before executing its private Python.
# This launcher has no download, installation, compiler or system-Python path.
$ErrorActionPreference = 'Stop'
$usageArguments = @($args)
$packageRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

function Get-PayloadHash([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}

function Assert-RegularPayload([string]$Path) {
    $item = Get-Item -Force -LiteralPath $Path
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are not allowed in the public payload.' }
    if ($item.PSIsContainer) { throw ('Expected a regular payload file: ' + $Path) }
}

function Get-PayloadPath([string]$Name) {
    if ([string]::IsNullOrEmpty($Name) -or $Name.Contains('\') -or $Name.StartsWith('/') -or $Name -match '[\x00-\x1f]') {
        throw 'Invalid public payload path.'
    }
    foreach ($part in $Name.Split('/')) {
        if ($part -eq '' -or $part -eq '.' -or $part -eq '..' -or $part.Contains(':') -or $part -match '[ .]$' -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
            throw 'Invalid public payload path.'
        }
    }
    $target = [IO.Path]::GetFullPath((Join-Path $packageRoot $Name))
    if (-not $target.StartsWith($packageRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Public payload path leaves its package directory.'
    }
    $parent = [IO.Path]::GetDirectoryName($target)
    while ($parent.Length -ge $packageRoot.Length) {
        $item = Get-Item -Force -LiteralPath $parent
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are not allowed in the public payload.' }
        if ($parent -eq $packageRoot) { break }
        $parent = [IO.Path]::GetDirectoryName($parent)
    }
    Assert-RegularPayload $target
    return $target
}

function Assert-PublicTree([string]$Directory, $Expected) {
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push((Join-Path $packageRoot $Directory))
    while ($pending.Count -gt 0) {
        $folder = $pending.Pop()
        $folderItem = Get-Item -Force -LiteralPath $folder
        if (($folderItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are not allowed in the public payload.' }
        foreach ($item in Get-ChildItem -Force -LiteralPath $folder) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are not allowed in the public payload.' }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            else {
                $relative = $item.FullName.Substring($packageRoot.Length + 1).Replace('\','/')
                if (-not $Expected.Contains($relative)) { throw ('Unexpected public payload file: ' + $relative) }
            }
        }
    }
}

try {
    if ($packageRoot.StartsWith('\\')) { throw 'Extract Usage Monitor to a local folder on this computer instead of a network share.' }
    if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') {
        throw 'This Usage Monitor package requires Windows x64.'
    }
    $manifestPath = Get-PayloadPath 'bundle-manifest.json'
    $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1 -or $manifest.platform -ne 'windows-x86_64' -or
        $manifest.product.name -ne 'Usage Monitor' -or $manifest.privateUsageDataIncluded -ne $false -or
        $manifest.product.engineSha256 -ne 'fd6b9ab7f014e5e180c7bab14ee079763c6db07a7cccc48b59ff0da2b12adb37' -or
        $manifest.product.sourceCommit -ne '6bd0eed7b108497aa50051580387345340e3167f') {
        throw 'Invalid Usage Monitor public bundle manifest.'
    }
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $manifest.files.PSObject.Properties) {
        if (-not $names.Add($entry.Name) -or $entry.Value -notmatch '^[0-9a-f]{64}$') { throw 'Invalid or duplicate public payload SHA256 entry.' }
        $path = Get-PayloadPath $entry.Name
        if ((Get-PayloadHash $path) -ne $entry.Value) { throw ('Missing or damaged Usage Monitor payload: ' + $entry.Name + '. Extract the original ZIP again.') }
    }
    foreach ($required in @('Start-Usage-Monitor.cmd', 'scripts/windows_start.ps1', 'scripts/standalone_app.py',
                           'runtime/prebuilt-manifest.json', 'runtime/prebuilt-required',
                           'runtime/python/python.exe', 'runtime/bin/codex-usage-offline.exe')) {
        if (-not $names.Contains($required)) { throw ('Incomplete Usage Monitor package: ' + $required) }
    }
    foreach ($directory in @('scripts', 'assets', 'native', 'references', 'tests', 'licenses', 'runtime')) {
        Assert-PublicTree $directory $names
    }
    $python = Join-Path $packageRoot 'runtime\python\python.exe'
    $bridge = "import runpy,sys; p=sys.argv.pop(1); sys.path.insert(0,p); runpy.run_path(p+'/standalone_app.py',run_name='__main__')"
    & $python -B -X utf8 -c $bridge $PSScriptRoot @usageArguments
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('Usage Monitor failed: ' + $_.Exception.Message)
    exit 1
}
