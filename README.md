# Usage Monitor for Windows

A local Codex token dashboard and installable Codex skill. This repository contains the complete Windows x64 application, including portable Python, timezone data, the verified native scanner and all license notices.

## Install in Codex from GitHub

Give Codex your repository's actual URL and ask:

```text
Install the usage-monitor skill from https://github.com/XillerrelliX/Usage-Monitor/tree/main/usage-monitor
```

For a fork, replace `XillerrelliX/Usage-Monitor` with your repository. Codex's skill installer copies the complete `usage-monitor` subfolder, including its runtime. Then invoke `$usage-monitor` on the next turn. It asks the scope and period before a usage report and runs the batched local check after both answers.

## Install with the included Windows installer

1. On GitHub, choose **Code → Download ZIP**.
2. Extract the repository ZIP.
3. Double-click **Install-Usage-Monitor.cmd**.

The installer uses the adjacent complete application and installs it into `%CODEX_HOME%\skills\usage-monitor`, or `%USERPROFILE%\.codex\skills\usage-monitor` when CODEX_HOME is unset. It verifies the pinned bundle, every declared file and its bundled runtime, and checks launcher help before publishing the installation. It does not generate a usage report or change your Codex data/settings. An identical verified installation is reused; a different existing skill is left in place with an error.

No administrator, Git, system Python or Rust installation is needed for this installer. Codex is needed to invoke the skill; the application can also be opened directly with its installed `Start-Usage-Monitor.cmd`.

For a direct public-GitHub download using a saved copy of the installer:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Repository XillerrelliX/Usage-Monitor -Ref main
```

Use a tag or full commit SHA for a fixed version. The script downloads a repository archive over HTTPS and validates its contents before running the packaged help command. It does not evaluate a downloaded script. Its pinned application manifest must agree with the repository payload. Private repositories/authentication and unattended upgrades are outside this installer's scope.

A ZIP already downloaded from GitHub can also be installed with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -ArchivePath .\repository-main.zip
```

## Upload this repository

Create your GitHub repository, then commit the **contents of this folder**, including the complete `usage-monitor/` directory and its `runtime/` files. Git or GitHub Desktop is convenient for this folder's file count. Preserve `.gitattributes`, which keeps exact packaged bytes on Windows checkouts. Uploading this repository ZIP as a single tracked file does not expose the installable skill folder.

The ready folder is independent of any particular GitHub owner/repository name. No account credentials or publishing configuration are embedded. After publishing, share the real `.../tree/main/usage-monitor` link above.

Layout:

```text
README.md
install.ps1
Install-Usage-Monitor.cmd
repository-manifest.json
.gitattributes
.gitignore
LICENSE
usage-monitor/
  SKILL.md
  Start-Usage-Monitor.cmd
  bundle-manifest.json
  scripts/ assets/ references/ tests/ native/ licenses/ runtime/
```

## Data and maintenance

The application reads the local Codex files available on the destination machine. Private history, settings, caches, reports and credentials are excluded from this repository. Reports and the optional query meter remain local; their results do not establish account allowance or which device/person originally produced synced files.

The Windows package is version **1.4.0**. Runtime, scanner, accounting and the existing dashboard are unchanged from that verified release. macOS/Linux do not have a ready-to-use package here. This project is an independent MIT-licensed wrapper; third-party licenses remain under `usage-monitor/licenses/` and the native source.

For a future application release, rebuild its verified complete package and update the installer's pinned bundle identity together. Preserve the public manifests and tests; do not edit their hashes to bypass integrity failures. Repository archive compression can change, so the installer validates the extracted, pinned application contents rather than relying on a generated GitHub archive's outer ZIP hash.

GitHub references: [download repository archives](https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives), [upload files](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository).
