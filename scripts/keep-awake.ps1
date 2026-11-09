# keep-awake.ps1 — 让系统在运行期间不进入睡眠/新型待机（允许息屏）。
#
# 为什么需要：
#   2026-11-09 实测，笔记本 10 分钟息屏后 Windows 进入「新型待机（Modern Standby）」，
#   AstrBot / NapCat / Shell 三个进程被整体冻结 56 分钟（16:17 与 18:18 各一次），
#   期间 QQ 消息完全收不到，表现为「发两三次才回」。
#   powercfg 的「睡眠超时」设为 0 也挡不住——新型待机是由「息屏」触发的。
#
# 做法：调用 SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)，
#   向系统声明「本进程需要系统保持运行」。这是媒体播放器息屏不断播的同一机制。
#   只锁系统、不锁屏幕：屏幕仍按设置息屏，但不再挂起。
#
# 停止：关闭窗口或结束 powershell 进程即可，注册的请求随之释放。
param(
    [int]$HeartbeatSeconds = 60
)

$ErrorActionPreference = 'Stop'

Add-Type -Namespace Win32Native -Name Power -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@

$ES_CONTINUOUS       = 0x80000000
$ES_SYSTEM_REQUIRED  = 0x00000001
$flags = $ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED

[void][Win32Native.Power]::SetThreadExecutionState($flags)
Write-Host "[keep-awake] $(Get-Date -Format 'HH:mm:ss') 已申请系统常驻：允许息屏，禁止休眠/新型待机"
Write-Host '[keep-awake] 关闭本窗口即解除。'

while ($true) {
    Start-Sleep -Seconds ([Math]::Max(10, $HeartbeatSeconds))
    [void][Win32Native.Power]::SetThreadExecutionState($flags)
}
