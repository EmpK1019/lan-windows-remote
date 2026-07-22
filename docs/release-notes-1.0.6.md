# LAN Remote 1.0.6

- 实测确认普通 `SendInput` 在 Windows `Winlogon` 安全桌面会被系统拒绝，不再使用固定的 Enter/密码/Enter 模拟输入链路。
- 新增独立的 LAN Remote Credential Provider：仅在收到 30 秒有效的一次性远程解锁请求时枚举，不替换或过滤 Windows 自带密码、PIN、Windows Hello 登录方式。
- 锁屏密码由 LocalSystem 使用机器级 DPAPI 加密，待处理注册表项仅允许 SYSTEM 与管理员读取，Provider 读取后立即删除并清零明文缓冲区。
- LockApp 到 LogonUI 的唤醒阶段允许输入桌面切换竞态；只有确认进入凭据界面后才触发 Provider 自动提交。
- 保持 1.0.5 的原生 H.264 正常桌面/Winlogon 无黑屏切源、30/60/120 FPS、原生窗口与工具栏行为不变。
