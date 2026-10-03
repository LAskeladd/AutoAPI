param(
    [switch]$NoRepl,
    [string]$Config = 'config.yaml'
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# Windows PowerShell 5.1 otherwise decodes native UTF-8 output as the OEM code page.
$utf8 = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = $utf8
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$exitCode = 1
try {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw '未找到项目虚拟环境 .venv\Scripts\python.exe。请先创建环境并安装 requirements.txt。'
    }
    $main = Join-Path $PSScriptRoot 'main.py'
    if (-not (Test-Path -LiteralPath $main -PathType Leaf)) {
        throw "未找到启动文件：$main"
    }
    $configPath = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $PSScriptRoot $Config }
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "未找到配置文件：$configPath。请根据 config.example 创建配置。"
    }
    $arguments = @($main, '-c', $configPath)
    if ($NoRepl) { $arguments += '--no-repl' }
    & $python @arguments
    $exitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine(('启动失败：' + $_.Exception.Message))
    $exitCode = 1
}
exit $exitCode
